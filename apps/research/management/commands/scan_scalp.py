"""Research mode: scan stored candles with the scalp strategies and show what they would do.

    python manage.py scan_scalp --venue gate --timeframe 5m --capital 27

Places NO orders. Lists accepted and rejected candidates with reasons, instruments the capital
cannot trade, and stale feeds. An empty accepted list is a normal, correct outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.market.models import Instrument
from apps.market.services import measured_spreads_bps
from apps.market.store import CandleStore
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, with_features
from nextrade_core.scalping.scanner import scan
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, make


class Command(BaseCommand):
    help = "Scan stored candles with the scalp strategies (research mode, no orders)."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default="gate", choices=["gate", "quidax", "binance", "mt5", "bybit"])
        parser.add_argument("--timeframe", default="5m")
        parser.add_argument("--strategies", default=",".join(SCALP_PREREGISTERED))
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--capital", default="27", help="capital in the venue's quote currency (N40,000 is about $27: set it)")
        parser.add_argument("--risk-per-trade", default="0.0025")
        parser.add_argument("--days", type=int, default=10, help="history loaded for indicator warm-up")
        parser.add_argument("--can-short", action="store_true", help="the venue/account allows shorting")

    def handle(self, *args, **o):
        tf, venue = Timeframe(o["timeframe"]), o["venue"]
        qs = Instrument.objects.filter(venue=venue)
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = {i.symbol: i for i in qs if i.candles.exists()}
        if not instruments:
            raise CommandError(f"No stored {venue} candles. Run load_{venue}_history first.")
        store = CandleStore(instruments)
        now = datetime.now(UTC)
        frames, no_data = {}, []
        for sym in instruments:
            raw = store.load_frame(sym, tf, now - timedelta(days=o["days"]), now)
            if len(raw) > FeatureParams().warmup_bars + 5:
                frames[sym] = with_features(raw, tf)
            else:
                no_data.append(sym)
        strategies = {c.strip(): make(c.strip()) for c in o["strategies"].split(",") if c.strip()}
        costs = cost_model_for(venue, measured_spreads_bps(instruments) if venue == "mt5" else None)
        res = scan(frames, {s: instruments[s].to_domain() for s in frames}, strategies, timeframe=tf, costs=costs,
                   now=now, capital=Decimal(o["capital"]), risk_fraction=Decimal(o["risk_per_trade"]),
                   can_short=o["can_short"])
        self.stdout.write(f"Scanned {len(frames)} symbols, {len(strategies)} strategies at {tf.value} ({now:%H:%M} UTC).")
        if no_data:
            self.stdout.write(f"  stale or missing data (too few recent {tf.value} bars; keep the recorder running "
                              f"or reload history), skipped: {', '.join(sorted(no_data))}")
        if res.stale:
            self.stdout.write(f"  stale or missing data, skipped: {', '.join(sorted(res.stale))}")
        if not res.accepted:
            self.stdout.write("  Nothing qualifies right now. Waiting is the correct decision.")
        for c in res.accepted:
            s = c.signal
            self.stdout.write(f"  WOULD ENTER {s.symbol} {s.side.value} via {s.strategy}: expected net {c.expected_net_r:+.2f}R "
                              f"after {c.cost_r:.2f}R costs, regime {c.regime}  (research: no order placed)")
        for c in res.rejected:
            self.stdout.write(f"  rejected {c.signal.symbol} {c.signal.strategy}: {'; '.join(c.reasons)}")
        for sym, why in sorted(res.unavailable.items()):
            self.stdout.write(f"  unavailable {sym}: {'; '.join(why)}")
        self.stdout.write("These strategies are unproven until they pass run_backtest and are approved.")
