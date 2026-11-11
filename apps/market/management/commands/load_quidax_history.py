"""Load the recent Quidax k-lines Quidax still serves (verified 2026-11-11 from Nigeria):
only the newest ~300 bars per period, `timestamp` is ignored, and the 4h and 1d periods
return nothing. So this keeps the last ~300 1h bars (12 days) and ~300 1m bars; deeper
Quidax history only exists if `record_quidax` runs continuously. For research history
use load_gate_history. 4h Quidax bars are built from stored 1h bars.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.quidax.markets import PERIOD_MINUTES, market_of, parse_klines
from apps.market.services import sync_quidax_markets
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

from ._common import parse_day
from ._quidax import make_quidax, quidax_symbols

MAX_PAGES = 200
now_utc = lambda: datetime.now(UTC)  # noqa: E731


class Command(BaseCommand):
    help = "Download the recent Quidax candles still served (1h and 1m)."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None, help="e.g. ETHNGN,USDTNGN")
        parser.add_argument("--timeframes", default="1h,1m")
        parser.add_argument("--start", default="2023-01-01", help="YYYY-MM-DD, used when nothing is stored yet")

    def handle(self, *args, **o):
        symbols = quidax_symbols(o["symbols"])
        tfs = [Timeframe(t.strip()) for t in o["timeframes"].split(",") if t.strip()]
        start = parse_day(o["start"])
        now = now_utc()
        client = make_quidax()
        try:
            try:
                instruments = sync_quidax_markets(client, symbols)
            except VenueError as exc:
                raise CommandError(f"Cannot reach Quidax: {exc}") from exc
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            store = CandleStore(instruments, run_label="load_quidax")
            for symbol in symbols:
                for tf in tfs:
                    self._load(client, store, symbol, tf, start, now)
        finally:
            client.close()
        self.stdout.write(self.style.SUCCESS("Done. Next: python manage.py run_backtest --timeframe 1h"))

    def _load(self, client, store, symbol, tf, start, now) -> None:
        last = store.last_open_time(symbol, tf)
        cursor = last + tf.delta if last else start
        total, first_seen = 0, None
        for _ in range(MAX_PAGES):
            rows = client.klines(market_of(symbol), PERIOD_MINUTES[tf], since_s=int(cursor.timestamp()))
            candles = [c for c in parse_klines(rows, symbol, tf) if c.open_time >= cursor and c.is_closed(now)]
            if not candles:
                break
            first_seen = first_seen or candles[0].open_time
            store.save_candles(candles, "rest")
            total += len(candles)
            nxt = candles[-1].open_time + tf.delta
            if nxt <= cursor or nxt + tf.delta > now:
                break
            cursor = nxt
        if total == 0:
            self.stdout.write(f"  {symbol} {tf.value}: up to date" if last else f"  {symbol} {tf.value}: no data")
            return
        note = ""
        if not last and first_seen and first_seen - start > timedelta(days=7):
            note = f"  (Quidax only serves bars from {first_seen:%Y-%m-%d %H:%M} UTC)"
        self.stdout.write(f"  {symbol} {tf.value}: {total} bars{note}")
