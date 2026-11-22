"""Run the trading daemon. Paper mode by default; live mode is enabled in Phase 14.

    python manage.py run_trader --new-run --capital 1000
    python manage.py run_trader                 # resume the latest paper run
Needs `record_live` running in another window (it supplies candles and quotes).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.bybit.client import is_pegged

from adapters.paper.broker import PaperBroker
from apps.market.models import Instrument
from apps.market.store import CandleStore
from apps.trading.bootstrap import LATCHED, hit_rate_naia, last_state, make_controller, paper_balances
from apps.trading.models import TradingRun
from apps.trading.store import DjangoMarketSource, DjangoTradingStore, OffLoop, ControlPoller
from nextrade_core.clock import WallClock
from nextrade_core.controller.state_machine import Actor, TradingState
from nextrade_core.costs.model import CostModel, cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits, scaled_limits
from nextrade_core.scalping import approvals, paper_safety
from nextrade_core.scalping.net_exit import NetExitConfig, NetProfitExit
from nextrade_core.scalping.reentry import ReentryConfig, ReentryGuard
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, TRADABLE, make
from services.trader.daemon import DaemonConfig, TradingDaemon


log = logging.getLogger("nextrade.run_trader")
CAMPAIGN = "paper-scalp-ngn"


def seed_reentry(guard: ReentryGuard, run: TradingRun, now, hours: int = 6) -> int:
    """After a restart the guard knows nothing: rebuild it from positions this run closed recently."""
    from apps.trading.models import Position
    n = 0
    for p in Position.objects.filter(run=run, status="closed", closed_at__gte=now - timedelta(hours=hours)).order_by("closed_at"):
        guard.record_close(p.symbol, p.strategy, 1, float(p.realized_pnl or 0), p.closed_at, p.opened_at)
        n += 1
    return n


def trader_limits(risk_per_trade: Decimal, venue: str, max_positions: int, max_exposure: Decimal,
                  max_market_exposure: Decimal) -> RiskLimits:
    from dataclasses import replace
    base = scaled_limits(risk_per_trade, venue)
    return replace(base, max_positions=max_positions, max_exposure=max_exposure, max_symbol_exposure=max_market_exposure)


class Command(BaseCommand):
    help = "Run the NEXTRADE trading daemon (paper)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", default="paper", choices=["paper", "live"])
        parser.add_argument("--venue", default="binance", choices=["binance", "bybit", "gate"],
                            help="whose recorded candles/quotes and cost model to use (bybit/gate: run record_rest)")
        parser.add_argument("--new-run", action="store_true")
        parser.add_argument("--capital", default=None, help="paper starting cash in USDT (new runs); default: --capital-ngn / --ngn-per-usdt")
        parser.add_argument("--capital-ngn", default="40000", help="simulated starting capital in naira")
        parser.add_argument("--ngn-per-usdt", default="1480", help="ASSUMED naira per USDT used only to size the paper account")
        parser.add_argument("--risk-per-trade", default="0.001", help="fraction of equity risked per trade (0.001 = 0.1%%)")
        parser.add_argument("--max-positions", type=int, default=5)
        parser.add_argument("--max-exposure", default="0.80", help="max open notional as a fraction of equity")
        parser.add_argument("--max-market-exposure", default="0.30", help="max notional in one market as a fraction of equity")
        parser.add_argument("--net-min-bps", type=float, default=8.0, help="net profit (bps) a scalp must clear")
        parser.add_argument("--min-planned-bps", type=float, default=15.0, help="a target must promise this much NET at entry")
        parser.add_argument("--stop-file", default=None, help="exit cleanly when this file appears")
        parser.add_argument("--campaign", default=CAMPAIGN)
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--strategies", default=",".join(TRADABLE))
        parser.add_argument("--timeframe", default="15m")
        parser.add_argument("--poll", type=float, default=5.0)
        parser.add_argument("--max-steps", type=int, default=0, help="stop after N polls (testing)")

    def handle(self, *args, **o):
        if o["mode"] == "live":
            raise CommandError("Live mode is started with run_live (Phase 14), after go_live_check passes.")
        if last_state() in LATCHED:
            raise CommandError(f"Trading is halted ({last_state()}). Review, then run: "
                               "python manage.py acknowledge_halt --reason \"...\"")
        logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per request would drown the log
        paper_safety.install_write_guard()
        tf = Timeframe(o["timeframe"])
        base = Path(settings.BASE_DIR)
        strategies_req = [c for c in o["strategies"].split(",") if c]
        scalp_req = [c for c in strategies_req if c in SCALP_PREREGISTERED]
        if scalp_req:
            try:
                allowed = set(approvals.require_mode(base, approvals.Mode.PAPER))
            except PermissionError as exc:
                raise CommandError(f"{exc}. Run: python manage.py authorise_paper_trial") from exc
            blocked = [c for c in scalp_req if c not in allowed]
            if blocked:
                raise CommandError(f"Not approved for paper trading: {blocked}. Run authorise_paper_trial.")
            wrong_tf = [c for c in scalp_req if SCALP_PREREGISTERED[c] != tf.value]
            for c in wrong_tf:
                self.stdout.write(self.style.WARNING(f"  {c} is designed for {SCALP_PREREGISTERED[c]} bars; this process runs {tf.value}: "
                                                     "NOT included (it needs its own process and account)."))
            strategies_req = [c for c in strategies_req if c not in wrong_tf]
        if not strategies_req:
            raise CommandError("No strategy left to run on this timeframe.")
        trial = [c for c in scalp_req if approvals.load(base).get(c) and approvals.load(base)[c].trial and c in strategies_req]
        if trial:
            self.stdout.write(self.style.WARNING("EXPLORATORY PAPER TRIAL: these strategies have NOT passed validation, results are unvalidated: "
                                                 + ", ".join(trial)))
        qs = Instrument.objects.filter(venue=o["venue"], tradable=True, candles__isnull=False).distinct()
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = {i.symbol: i for i in qs if not is_pegged(i.symbol[:-4] if i.symbol.endswith("USDT") else i.symbol)}
        if not instruments:
            raise CommandError("No instruments. Run load_bybit_history (or sync_binance_instruments / load_binance_history) first.")

        if o["capital"]:
            capital = Decimal(o["capital"])
        else:
            capital = (Decimal(o["capital_ngn"]) / Decimal(o["ngn_per_usdt"])).quantize(Decimal("0.01"))
        run = TradingRun.objects.filter(mode="paper", ended_at=None, config__campaign=o["campaign"]).order_by("-id").first()
        if o["new_run"] or run is None:
            run = TradingRun.objects.create(
                mode="paper", initial_equity=capital,
                config={"campaign": o["campaign"], "symbols": sorted(instruments), "timeframe": tf.value,
                        "strategies": ",".join(strategies_req), "capital_ngn": o["capital_ngn"],
                        "ngn_per_usdt_assumed": o["ngn_per_usdt"], "validated": False})
            self.stdout.write(f"New paper run {run.pk} with {run.initial_equity} USDT "
                              f"(= NGN {o['capital_ngn']} at an assumed {o['ngn_per_usdt']} NGN/USDT)")
        else:
            self.stdout.write(f"Resuming paper run {run.pk} (started with {run.initial_equity} USDT)")

        clock = WallClock()
        costs = cost_model_for(o["venue"]) if o["venue"] != "binance" else CostModel()
        strategies = {c: make(c) for c in strategies_req}
        candle_store = CandleStore(instruments)
        from apps.research.registry import production_ensemble
        rule_based = [c for c, st in strategies.items() if getattr(st, "judged_raw", False)]
        model_based = {c: st for c, st in strategies.items() if c not in rule_based}
        naia, sources = hit_rate_naia(candle_store, model_based, sorted(instruments), tf, costs,
                                      model_lookup=production_ensemble)
        naia.mark_rule_based(rule_based)       # rule-based strategies are judged on raw trades (ADR 0006)
        for code in rule_based:
            sources[code] = "rule-based (risk engine and cost checks still apply)"
        for code, src in sources.items():
            self.stdout.write(f"  NAIA {code}: {src}")

        broker = PaperBroker(balances_=paper_balances(run), fee_rate=Decimal(str(costs.fee_rate)),
                             slippage=Decimal(str(costs.slippage_bps / 10_000)))
        limits = trader_limits(Decimal(o["risk_per_trade"]), o["venue"], o["max_positions"],
                               Decimal(o["max_exposure"]), Decimal(o["max_market_exposure"]))
        guard = ReentryGuard(ReentryConfig())
        seeded = seed_reentry(guard, run, clock.now())
        net_cfg = NetExitConfig(min_net_bps=o["net_min_bps"], min_planned_bps=o["min_planned_bps"])
        self.stdout.write(f"Markets: {len(instruments)}; strategies: {', '.join(strategies)}; timeframe {tf.value}; costs "
                          f"fee {costs.fee_rate:.2%}/side, spread {costs.spread_bps:g} bps, slippage {costs.slippage_bps:g} bps/side")
        self.stdout.write(f"Risk: {limits.risk_per_trade:.2%}/trade, max {limits.max_positions} positions, exposure <= "
                          f"{limits.max_exposure:.0%} (one market <= {limits.max_symbol_exposure:.0%}), daily loss {limits.daily_loss:.0%}, "
                          f"drawdown stop {limits.max_drawdown:.0%}; re-entry guard seeded with {seeded} recent closes")
        # the daemon is async: route every database call through one worker thread
        db_store = OffLoop(DjangoTradingStore(), "store")
        db_source = OffLoop(DjangoMarketSource(instruments), "source")
        controller = make_controller(clock, threaded=True)
        controls = OffLoop(ControlPoller(clock.now()), "controls")
        daemon = TradingDaemon(
            DaemonConfig(run_id=run.pk, mode="paper", symbols=sorted(instruments), timeframe=tf),
            source=db_source, broker=broker, store=db_store,
            strategies=strategies, naia=naia, risk=RiskEngine(), costs=costs,
            instruments={s: i.to_domain() for s, i in instruments.items()}, controller=controller, clock=clock,
            controls=controls.poll, reentry=guard, net_exit=NetProfitExit(costs, net_cfg), net_cfg=net_cfg,
            can_short=False)
        paper_safety_report = paper_safety.safety_checks(base, broker=broker)
        for name, ok, detail in paper_safety_report:
            self.stdout.write(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")
        if not paper_safety.all_ok(paper_safety_report):
            raise CommandError("Paper-mode safety check failed; refusing to start.")
        try:
            asyncio.run(self._main(daemon, controller, o))
        finally:
            db_store.close()
            db_source.close()
            controls.close()

    async def _main(self, daemon: TradingDaemon, controller, o) -> None:
        controller.transition(TradingState.DATA_INIT, Actor.OPERATOR, "run_trader started")
        controller.transition(TradingState.ANALYSIS, Actor.SYSTEM, "market data available")
        for sym in daemon.cfg.symbols:   # paper venue needs a price before re-placing stops
            q = daemon.source.latest_quote(sym)
            if q:
                daemon.broker.update_quote(sym, q[0], q[1])
        report = await daemon.reconcile()
        self.stdout.write(f"Reconciliation: {report}")
        if not report["ok"]:
            daemon.store.alert("warning", "reconcile", f"reconciliation found differences: {report}")
        controller.transition(TradingState.PAPER, Actor.OPERATOR, "paper trading started")
        self.stdout.write("Paper trading. Press Ctrl+C to stop.")
        steps = bad = 0
        stop_file = Path(o["stop_file"]) if o["stop_file"] else None
        try:
            while True:
                if stop_file is not None and stop_file.exists():
                    self.stdout.write("Stop file found: stopping cleanly.")
                    break
                try:
                    result = await daemon.step()
                    bad = 0
                except VenueError as exc:                      # network or exchange trouble: wait and carry on
                    bad += 1
                    self.stdout.write(f"[{daemon.clock.now():%H:%M:%S}] data/venue error ({bad}): {str(exc)[:120]}")
                    await asyncio.sleep(min(60, 5 * bad))
                    continue
                except Exception as exc:                       # noqa: BLE001  a bug: report, retry a few times, then let the supervisor restart
                    bad += 1
                    log.exception("trader step failed")
                    daemon.store.alert("warning", "trader_error", f"{type(exc).__name__}: {str(exc)[:200]}")
                    if bad >= 5:
                        raise
                    await asyncio.sleep(5)
                    continue
                if result:
                    self.stdout.write(f"[{daemon.clock.now():%Y-%m-%d %H:%M} UTC] bar {daemon.last_bar}: {result}")
                steps += 1
                if o["max_steps"] and steps >= o["max_steps"]:
                    break
                await asyncio.sleep(o["poll"])
        except KeyboardInterrupt:
            pass
        finally:
            if controller.state is TradingState.PAPER:
                controller.transition(TradingState.ANALYSIS, Actor.OPERATOR, "run_trader stopped")
