"""Load 1-minute history from Binance's public archive (plus REST for today).

Resumable: a month or day already complete in the database is skipped, and
downloaded zips are cached under data/raw, so rerunning after an interruption
only fetches what is missing.

Defaults: PostgreSQL -> from 2022-01-01. SQLite -> last 90 days only.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from adapters.base import RetryableError, VenueError
from adapters.binance.archive import BinanceArchive
from adapters.binance.klines import fetch_closed_klines
from apps.market.services import ensure_instruments
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

from ._common import make_rest, parse_day, parse_symbols, unreachable, using_sqlite

TF = Timeframe.M1
MONTHLY_PUBLISH_GRACE = timedelta(days=4)

make_client = make_rest
make_archive = lambda cache_dir: BinanceArchive(cache_dir)  # noqa: E731  (patch point for tests)
now_utc = lambda: datetime.now(UTC)  # noqa: E731


def month_starts(start: datetime, end: datetime):
    cur = start.replace(day=1)
    while cur < end:
        yield cur
        cur = (cur + timedelta(days=32)).replace(day=1)


class Command(BaseCommand):
    help = "Download Binance 1m klines (archive + REST tail) into the database."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--start", help="YYYY-MM-DD (default: 2022-01-01 on PostgreSQL, 90 days ago on SQLite)")
        parser.add_argument("--end", help="YYYY-MM-DD exclusive (default: now)")
        parser.add_argument("--cache-dir", default=str(Path(settings.BASE_DIR) / "data" / "raw"))
        parser.add_argument("--no-rest-tail", action="store_true", help="skip REST fill for today")

    def handle(self, *args, **opts):
        symbols = parse_symbols(opts["symbols"])
        now = now_utc()
        today = parse_day(now.date().isoformat())
        if opts["start"]:
            start = parse_day(opts["start"])
        elif using_sqlite():
            start = today - timedelta(days=90)
            self.stdout.write(self.style.WARNING(
                "SQLite detected: loading the last 90 days only. Set DATABASE_URL to PostgreSQL for full history."))
        else:
            start = parse_day("2022-01-01")
        end = parse_day(opts["end"]) if opts["end"] else now

        rest = make_client()
        archive = make_archive(Path(opts["cache_dir"]))
        failures: list[str] = []
        try:
            try:
                instruments = ensure_instruments(rest, symbols)
            except VenueError as exc:
                raise unreachable(exc) from exc
            store = CandleStore(instruments, run_label="load_history")
            for symbol in symbols:
                self.stdout.write(f"\n{symbol}  {start:%Y-%m-%d} -> {end:%Y-%m-%d %H:%M} UTC")
                for m0 in month_starts(start, min(end, today)):
                    m1 = (m0 + timedelta(days=32)).replace(day=1)
                    lo, hi = max(start, m0), min(end, today, m1)
                    if lo >= hi:
                        continue
                    try:
                        self._load_range(archive, store, symbol, lo, hi, m0, m1, now)
                    except RetryableError as exc:
                        failures.append(f"{symbol} {m0:%Y-%m}: {exc}")
                        self.stdout.write(self.style.ERROR(f"  {m0:%Y-%m}: failed after retries ({exc})"))
                if not opts["no_rest_tail"] and end >= today:
                    last = store.last_open_time(symbol)
                    tail_start = max(today, last + TF.delta) if last else today
                    try:
                        candles = fetch_closed_klines(rest, symbol, TF, tail_start, now)
                    except VenueError as exc:
                        failures.append(f"{symbol} today (REST): {exc}")
                        continue
                    store.save_candles(candles, "rest")
                    self.stdout.write(f"  today via REST: {len(candles)} bars")
        finally:
            rest.close()
            archive.close()

        mb = archive.bytes_downloaded / 1_000_000
        self.stdout.write(f"\nDownloaded {mb:.1f} MB (cached zips are reused on reruns).")
        if failures:
            self.stdout.write(self.style.ERROR(f"{len(failures)} ranges failed; rerun to retry:"))
            for f in failures:
                self.stdout.write(f"  {f}")
        else:
            self.stdout.write(self.style.SUCCESS("History load complete. Next: python manage.py data_quality_report"))

    def _load_range(self, archive, store, symbol, lo, hi, m0, m1, now) -> None:
        expected = int((hi - lo) / TF.delta)
        have = store.count(symbol, lo, hi)
        if have >= expected:
            self.stdout.write(f"  {m0:%Y-%m}: complete ({have} bars), skipped")
            return
        candles = None
        monthly_due = m1 + MONTHLY_PUBLISH_GRACE <= now
        if monthly_due:
            candles = _retry(lambda: archive.monthly(symbol, TF, m0.year, m0.month))
            if candles is None:
                self.stdout.write(f"  {m0:%Y-%m}: no archive file (pair not listed yet?)")
                return
            how = "monthly file"
        else:
            candles, day = [], lo
            while day < hi:
                if store.count(symbol, day, day + timedelta(days=1)) < 1440:
                    got = _retry(lambda d=day: archive.daily(symbol, TF, date(d.year, d.month, d.day)))
                    candles.extend(got or [])
                day += timedelta(days=1)
            how = "daily files"
        in_range = [c for c in candles if lo <= c.open_time < hi]
        store.save_candles(in_range, "archive")
        self.stdout.write(f"  {m0:%Y-%m}: {len(in_range)} bars from {how}")


def _retry(fn, attempts: int = 3, wait_s: float = 2.0):
    for i in range(attempts):
        try:
            return fn()
        except RetryableError:
            if i == attempts - 1:
                raise
            time.sleep(wait_s * (i + 1))
