"""Load Gate.io candles as RESEARCH history (NEXTRADE does not trade on Gate.io).

Gate serves at most 10,000 bars back per interval: about 14 months of 1h bars and
4.5 years of 4h bars. Resumable: each timeframe continues from the newest stored bar.
Backtest on it with Quidax costs:  python manage.py run_backtest --venue gate --timeframe 4h
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.gate.client import GateClient, earliest_allowed, instrument_from_pair, pair_of
from apps.market.models import Instrument
from apps.market.services import save_instrument
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

DEFAULT_SYMBOLS = "ETHUSDT,BTCUSDT,SOLUSDT"
now_utc = lambda: datetime.now(UTC)  # noqa: E731
make_gate = lambda: GateClient()  # noqa: E731


class Command(BaseCommand):
    help = "Download Gate.io 1h/4h/1d candles (research data; trading stays on Quidax)."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
        parser.add_argument("--timeframes", default="1h,4h,1d")

    def handle(self, *args, **o):
        symbols = [s.strip().upper() for s in o["symbols"].split(",") if s.strip()]
        tfs = [Timeframe(t.strip()) for t in o["timeframes"].split(",") if t.strip()]
        now = now_utc()
        client = make_gate()
        try:
            try:
                for s in symbols:
                    save_instrument(instrument_from_pair(client.currency_pair(pair_of(s))))
            except VenueError as exc:
                raise CommandError(f"Cannot load Gate.io pairs: {exc}") from exc
            store = CandleStore({i.symbol: i for i in Instrument.objects.filter(venue="gate", symbol__in=symbols)},
                                run_label="load_gate")
            for s in symbols:
                for tf in tfs:
                    last = store.last_open_time(s, tf)
                    start = last + tf.delta if last else earliest_allowed(now, tf)
                    try:
                        bars = client.candles(s, tf, start, tf.floor(now))
                    except VenueError as exc:
                        self.stdout.write(self.style.ERROR(f"  {s} {tf.value}: {exc}"))
                        continue
                    store.save_candles(bars, "rest")
                    span = f" ({bars[0].open_time:%Y-%m-%d} -> {bars[-1].open_time:%Y-%m-%d})" if bars else ""
                    self.stdout.write(f"  {s} {tf.value}: {len(bars)} new bars{span}")
        finally:
            client.close()
        self.stdout.write(self.style.SUCCESS(
            "Done. Next: python manage.py run_backtest --venue gate --timeframe 4h"))
