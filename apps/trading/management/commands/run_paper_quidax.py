"""Paper-trade a pre-registered strategy on live Quidax prices (fake money only).

    python manage.py run_paper_quidax --new-run            # start: NGN 20,000, trend filter
    python manage.py run_paper_quidax                      # resume after a restart

Signals: Gate.io daily bars (refreshed hourly by this command). Fills, spreads and stops:
live Quidax NGN quotes (polled every minute by this command), converted to USDT at the live
USDT/NGN rate. Stops are held by the paper venue, which fills them on Quidax prices.
Only strategies judged on their raw record (ADR 0006) run here; T1 is the one that passed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from adapters.gate.client import GateClient
from adapters.paper.broker import PaperBroker
from apps.market.management.commands._quidax import make_quidax
from apps.market.models import Instrument
from apps.market.services import measured_spreads_bps, sync_quidax_markets
from apps.trading.bootstrap import LATCHED, last_state, make_controller, paper_balances
from apps.trading.models import TradingRun
from apps.trading.proxy_source import GateDailyRefresher, GateQuidaxSource, QuidaxQuotePoller
from apps.trading.store import ControlPoller, DjangoTradingStore, OffLoop
from nextrade_core.clock import WallClock
from nextrade_core.controller.state_machine import Actor, TradingState
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.naia.engine import Naia
from nextrade_core.naia.scoring import ScoringConfig
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import scaled_limits
from nextrade_core.strategies.registry import make
from services.trader.daemon import DaemonConfig, TradingDaemon

UNIVERSE = "BTCUSDT,ETHUSDT,SOLUSDT,LTCUSDT,XRPUSDT,DASHUSDT,TRXUSDT"   # ADR 0006
make_gate = lambda: GateClient()  # noqa: E731
RUN_TAG = "gate-signals/quidax-fills"


class Command(BaseCommand):
    help = "Paper trading: Gate.io daily signals, live Quidax prices, fake money."

    def add_arguments(self, parser):
        parser.add_argument("--new-run", action="store_true")
        parser.add_argument("--capital-ngn", default="20000")
        parser.add_argument("--strategies", default="trendfilter")
        parser.add_argument("--symbols", default=UNIVERSE)
        parser.add_argument("--risk-per-trade", default="0.01")
        parser.add_argument("--poll", type=float, default=60.0, help="seconds between quote polls")
        parser.add_argument("--max-steps", type=int, default=0, help="stop after N polls (testing)")

    def handle(self, *args, **o):
        if last_state() in LATCHED:
            raise CommandError(f"Trading is halted ({last_state()}). Review, then run: "
                               "python manage.py acknowledge_halt --reason \"...\"")
        strategies = {c.strip(): make(c.strip()) for c in o["strategies"].split(",") if c.strip()}
        not_raw = [c for c, s in strategies.items() if not getattr(s, "judged_raw", False)]
        if not_raw:
            raise CommandError(f"{', '.join(not_raw)}: only rule-based strategies (ADR 0006) run here")
        symbols = [s.strip().upper() for s in o["symbols"].split(",") if s.strip()]
        gate = {i.symbol: i for i in Instrument.objects.filter(venue="gate", symbol__in=symbols)}
        missing = sorted(set(symbols) - set(gate))
        if missing:
            raise CommandError(f"No Gate.io data for {', '.join(missing)}. Run: python manage.py load_gate_history "
                               f"--symbols {','.join(missing)} --timeframes 1d")

        qx_client, gate_client = make_quidax(), make_gate()
        try:
            qx = sync_quidax_markets(qx_client, [g.base + "NGN" for g in gate.values()] + ["USDTNGN"])
        except Exception as exc:
            qx_client.close()
            gate_client.close()
            raise CommandError(f"Cannot reach Quidax: {exc}") from exc
        quidax_by_symbol = {s: qx[g.base + "NGN"] for s, g in gate.items()}
        usdtngn = qx["USDTNGN"]

        poller = OffLoop(QuidaxQuotePoller(qx_client, quidax_by_symbol, usdtngn), "quotes")
        refresher = OffLoop(GateDailyRefresher(gate_client, gate), "gate")
        clock = WallClock()
        poller.poll(clock.now())
        added = refresher.refresh(clock.now())
        self.stdout.write(f"Gate.io daily bars added: {added}")
        source = OffLoop(GateQuidaxSource(gate, quidax_by_symbol, usdtngn), "source")
        latest = _usdt_quote(usdtngn)
        if latest is None:
            raise CommandError("No USDT/NGN quote from Quidax yet; check the connection and retry.")
        rate = ((latest[0] + latest[1]) / 2).quantize(Decimal("0.01"))
        min_usdt = (Decimal(1250) / rate).quantize(Decimal("0.01"))
        # Orders are Quidax orders: Quidax's quantity rules and its NGN 1,250 minimum (in USDT);
        # only the price tick stays Gate's, because prices here are in USDT.
        domain = {s: replace(i.to_domain(), step_size=quidax_by_symbol[s].step_size,
                             min_qty=quidax_by_symbol[s].min_qty, min_notional=min_usdt)
                  for s, i in gate.items()}

        if o["new_run"] or not TradingRun.objects.filter(mode="paper", ended_at=None,
                                                         config__tag=RUN_TAG).exists():
            capital = (Decimal(o["capital_ngn"]) / rate).quantize(Decimal("0.01"))
            run = TradingRun.objects.create(mode="paper", initial_equity=capital, config={
                "tag": RUN_TAG, "symbols": symbols, "timeframe": "1d", "strategies": sorted(strategies),
                "capital_ngn": o["capital_ngn"], "ngn_per_usdt_at_start": str(rate),
                "risk_per_trade": o["risk_per_trade"]})
            self.stdout.write(f"New paper run {run.pk}: NGN {o['capital_ngn']} = {capital} USDT at NGN {rate}/USDT")
        else:
            run = TradingRun.objects.filter(mode="paper", ended_at=None, config__tag=RUN_TAG).first()
            self.stdout.write(f"Resuming paper run {run.pk}")

        qx_spreads = measured_spreads_bps({s: quidax_by_symbol[s] for s in gate})
        costs = cost_model_for("quidax", qx_spreads)
        limits = scaled_limits(Decimal(o["risk_per_trade"]), "quidax")
        # NAIA has its own spread gate; it must use the venue's limit, like NARA (Quidax: 60 bps)
        naia = Naia(costs, ScoringConfig(max_spread_bps=float(limits.max_spread_bps)))
        naia.mark_rule_based(strategies)
        store = OffLoop(DjangoTradingStore(), "store")
        controller = make_controller(clock, threaded=True)
        controls = OffLoop(ControlPoller(clock.now()), "controls")
        daemon = TradingDaemon(
            DaemonConfig(run_id=run.pk, mode="paper", symbols=sorted(gate), timeframe=Timeframe.D1,
                         process_name="paper_quidax"),
            source=source, broker=PaperBroker(balances_=paper_balances(run)), store=store,
            strategies=strategies, naia=naia, risk=RiskEngine(limits), costs=costs, instruments=domain,
            controller=controller, clock=clock, controls=controls.poll)
        self.stdout.write(f"Strategies: {', '.join(s.version for s in strategies.values())}; "
                          f"risk {limits.risk_per_trade:.2%}/trade; min order {min_usdt} USDT; "
                          f"spreads {qx_spreads or 'default 40 bps'}")
        try:
            asyncio.run(self._main(daemon, controller, poller, refresher, o,
                                   OffLoop(_Status(run.pk, usdtngn), "status")))
        finally:
            for x in (store, source, controls, poller, refresher):
                x.close()
            qx_client.close()
            gate_client.close()

    async def _main(self, daemon, controller, poller, refresher, o, status) -> None:
        controller.transition(TradingState.DATA_INIT, Actor.OPERATOR, "run_paper_quidax started")
        controller.transition(TradingState.ANALYSIS, Actor.SYSTEM, "market data available")
        for sym in daemon.cfg.symbols:
            q = daemon.source.latest_quote(sym)
            if q:
                daemon.broker.update_quote(sym, q[0], q[1])
        report = await daemon.reconcile()
        self.stdout.write(f"Reconciliation: {report}")
        controller.transition(TradingState.PAPER, Actor.OPERATOR, "paper trading started")
        self.stdout.write("Paper trading on Quidax prices (fake money). Ctrl+C to stop; rerun to resume.")
        # One line per price check buried everything else; keep HTTP noise to warnings only.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        steps, last_refresh_hour = 0, None
        try:
            while True:
                now = daemon.clock.now()
                poller.poll(now)
                if now.replace(minute=0, second=0, microsecond=0) != last_refresh_hour:
                    refresher.refresh(now)
                    last_refresh_hour, new_hour = now.replace(minute=0, second=0, microsecond=0), True
                else:
                    new_hour = False
                result = await daemon.step()
                if result:
                    self.stdout.write(f"[{now:%Y-%m-%d %H:%M} UTC] new daily bar {daemon.last_bar:%Y-%m-%d}: {result}")
                if new_hour:
                    self.stdout.write(f"[{now:%Y-%m-%d %H:%M} UTC] {status.line()}")
                steps += 1
                if o["max_steps"] and steps >= o["max_steps"]:
                    break
                await asyncio.sleep(o["poll"])
        except KeyboardInterrupt:
            pass
        finally:
            if controller.state is TradingState.PAPER:
                controller.transition(TradingState.ANALYSIS, Actor.OPERATOR, "run_paper_quidax stopped")


def _usdt_quote(usdtngn: Instrument):
    from apps.market.models import QuoteSample
    return QuoteSample.objects.filter(instrument=usdtngn).order_by("-ts").values_list("bid", "ask").first()


class _Status:
    """The hourly status line: proof the trader is alive, and what it holds."""

    def __init__(self, run_id: int, usdtngn: Instrument) -> None:
        self.run_id, self.usdtngn = run_id, usdtngn

    def line(self) -> str:
        from apps.trading.models import Position
        q = _usdt_quote(self.usdtngn)
        rate = f"NGN {(q[0] + q[1]) / 2:,.2f}/USDT" if q else "no USDT/NGN quote"
        positions = Position.objects.filter(run_id=self.run_id)
        open_syms = sorted(positions.filter(status="open").values_list("symbol", flat=True))
        closed = positions.filter(status="closed")
        pnl = sum((p.realized_pnl or Decimal(0) for p in closed), Decimal(0))
        return (f"alive · {rate} · open: {', '.join(open_syms) or 'none'} · "
                f"closed trades: {closed.count()} (net {pnl:+.2f} USDT)")
