"""Run the trading daemon. Paper mode by default; live mode is enabled in Phase 14.

    python manage.py run_trader --new-run --capital 1000
    python manage.py run_trader                 # resume the latest paper run
Needs `record_live` running in another window (it supplies candles and quotes).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from adapters.paper.broker import PaperBroker
from apps.market.models import Instrument
from apps.market.store import CandleStore
from apps.trading.bootstrap import LATCHED, hit_rate_naia, last_state, make_controller, paper_balances
from apps.trading.models import TradingRun
from apps.trading.store import DjangoMarketSource, DjangoTradingStore, OffLoop, ControlPoller
from nextrade_core.clock import WallClock
from nextrade_core.controller.state_machine import Actor, TradingState
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.strategies.registry import TRADABLE, make
from services.trader.daemon import DaemonConfig, TradingDaemon


class Command(BaseCommand):
    help = "Run the NEXTRADE trading daemon (paper)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", default="paper", choices=["paper", "live"])
        parser.add_argument("--new-run", action="store_true")
        parser.add_argument("--capital", default="1000", help="paper starting cash in USDT (new runs)")
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
        tf = Timeframe(o["timeframe"])
        qs = Instrument.objects.filter(venue="binance", tradable=True)
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = {i.symbol: i for i in qs}
        if not instruments:
            raise CommandError("No instruments. Run sync_binance_instruments / load_binance_history first.")

        if o["new_run"] or not TradingRun.objects.filter(mode="paper", ended_at=None).exists():
            run = TradingRun.objects.create(mode="paper", initial_equity=Decimal(o["capital"]),
                                            config={"symbols": sorted(instruments), "timeframe": tf.value,
                                                    "strategies": o["strategies"]})
            self.stdout.write(f"New paper run {run.pk} with {run.initial_equity} USDT")
        else:
            run = TradingRun.objects.filter(mode="paper", ended_at=None).first()
            self.stdout.write(f"Resuming paper run {run.pk}")

        clock = WallClock()
        costs = CostModel()
        strategies = {c: make(c) for c in o["strategies"].split(",") if c}
        candle_store = CandleStore(instruments)
        from apps.research.registry import production_ensemble
        naia, sources = hit_rate_naia(candle_store, strategies, sorted(instruments), tf, costs,
                                      model_lookup=production_ensemble)
        for code, src in sources.items():
            self.stdout.write(f"  NAIA {code}: {src}")

        broker = PaperBroker(balances_=paper_balances(run))
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
            controls=controls.poll)
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
        steps = 0
        try:
            while True:
                result = await daemon.step()
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
