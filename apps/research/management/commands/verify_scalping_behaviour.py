"""Does the engine actually scalp? Mechanical checks of the behaviour, not of profitability.

    python manage.py verify_scalping_behaviour --venue bybit --days 14 --timeframe 5m
    python manage.py verify_scalping_behaviour --synthetic

Runs the real strategies through the real simulator (shared cash, NARA risk limits, costs, the
re-entry guard) over many pairs and checks: simultaneous positions on different instruments,
automatic profit-taking and closing, fresh signals only, re-entry after closing without stale
signals, new entries while others are open, continuous scanning. Also reports which parts of the
scalping design are wired into the simulator and trader loop and which are not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.market.models import Instrument
from apps.market.store import CandleStore
from nextrade_core.backtest.engine import Backtester
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, with_features
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits, scaled_limits
from nextrade_core.scalping import behaviour
from nextrade_core.scalping.net_exit import backtest_entry_gate, backtest_exit_policy
from nextrade_core.scalping.reentry import ReentryConfig, ReentryGuard
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, make
from services.trader.daemon import TradingDaemon

now_utc = lambda: datetime.now(UTC)  # noqa: E731


class Command(BaseCommand):
    help = "Check that the engine really scalps: many positions, auto exits, fresh re-entries."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default="bybit")
        parser.add_argument("--timeframe", default="5m")
        parser.add_argument("--days", type=float, default=14)
        parser.add_argument("--strategies", default=",".join(s for s in SCALP_PREREGISTERED if s != "scalp_short"))
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--synthetic", action="store_true", help="use generated random-walk data (mechanics only)")
        parser.add_argument("--capital", default="1000")

    def handle(self, *args, **o):
        tf = Timeframe(o["timeframe"])
        codes = [c.strip() for c in o["strategies"].split(",") if c.strip()]
        costs = cost_model_for(o["venue"] if o["venue"] in ("bybit", "gate", "binance") else "bybit")
        if o["synthetic"]:
            from tests.helpers import random_walk_frame
            from nextrade_core.domain.instrument import AssetClass, Instrument as Dom
            frames, domain = {}, {}
            for k in range(8):
                sym = f"SYN{k}USDT"
                frames[sym] = with_features(random_walk_frame(int(o["days"] * 1440 / tf.minutes), seed=10 + k,
                                                              minutes_per_bar=tf.minutes), tf)
                domain[sym] = Dom(venue="synthetic", symbol=sym, base=f"SYN{k}", quote="USDT", asset_class=AssetClass.CRYPTO,
                                  tick_size=Decimal("0.0001"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                                  min_notional=Decimal("1"))
        else:
            qs = Instrument.objects.filter(venue=o["venue"])
            if o["symbols"]:
                qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
            insts = {i.symbol: i for i in qs if i.candles.exists()}
            if not insts:
                raise CommandError(f"No stored {o['venue']} candles. Run load_{o['venue']}_history first.")
            store, end = CandleStore(insts), now_utc()
            frames = {}
            for sym in insts:
                raw = store.load_frame(sym, tf, end - timedelta(days=o["days"] + 3), end)
                if len(raw) > FeatureParams().warmup_bars + 100:
                    frames[sym] = with_features(raw, tf)
            domain = {s: insts[s].to_domain() for s in frames}
        if not frames:
            raise CommandError("Not enough bars for any symbol.")
        limits = scaled_limits(RiskLimits().risk_per_trade, o["venue"] if o["venue"] in ("bybit", "gate", "binance") else "bybit")
        cooldown = ReentryConfig().cooldown
        # All strategies run together in ONE simulation, like the trader loop: shared cash, one position
        # per instrument, one re-entry guard, NARA limits across everything.
        signals, all_sigs = {}, []
        for code in codes:
            strat = make(code)
            sigs = [s for sym, f in frames.items() for s in strat.generate(f, sym, tf)]
            signals.update({s.key: pd.Timestamp(s.decision_time) for s in sigs})
            all_sigs += sigs
        all_sigs.sort(key=lambda s: s.decision_time)
        n_sig_symbols = {s.symbol for s in all_sigs}
        res = Backtester(frames, domain, costs, RiskEngine(limits), Decimal(o["capital"]), reentry=ReentryGuard(),
                         entry_gate=backtest_entry_gate(costs), exit_policy=backtest_exit_policy(costs)).run(all_sigs)
        trades, rej, funnel = res.trades, dict(res.rejections), dict(res.funnel)
        span = max((f.index[-1] - f.index[0]).total_seconds() for f in frames.values()) / 86400
        checks = behaviour.evaluate(trades, signals, n_signal_symbols=len(n_sig_symbols), rejections=rej, funnel=funnel,
                                    cooldown=cooldown, days=span) + behaviour.wiring(Backtester, TradingDaemon)
        self.stdout.write(f"Mechanics check on {len(frames)} symbols, {tf.value}, {span:.0f} days, strategies: {', '.join(codes)}"
                          + (" (SYNTHETIC random data: shows mechanics only)" if o["synthetic"] else ""))
        self.stdout.write(f"  signals generated: {len(signals):,}; trades: {len(trades):,}; funnel {dict(funnel)}; skipped {dict(rej)}")
        self.stdout.write(behaviour.verdict(checks))
        self.stdout.write("These checks show the engine's mechanics. They say nothing about whether it makes money.")
        out = Path(settings.BASE_DIR) / "research" / "validation"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (out / f"behaviour-{stamp}.json").write_text(json.dumps(
            {"synthetic": o["synthetic"], "symbols": sorted(frames), "timeframe": tf.value, "strategies": codes,
             "checks": [{"check": n, "result": ("pass" if ok else "not_demonstrated" if ok is None else "fail"), "evidence": w}
                        for n, ok, w in checks]}, indent=2, default=str))
