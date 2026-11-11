"""Record Quidax 1m candles and best bid/ask every minute until stopped (Ctrl+C).

Quidax documents no websocket stream, so this polls: per market and minute, one
k-line request (closed bars since the newest stored one) and one depth request
(spread sample). 6 markets use 12 of the 300 requests allowed per minute.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.binance.stream import Quote
from adapters.quidax.markets import best_bid_ask, market_of, parse_klines
from apps.market.services import sync_quidax_markets
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

from ._quidax import make_quidax, quidax_symbols

TF = Timeframe.M1
now_utc = lambda: datetime.now(UTC)  # noqa: E731
sleep = time.sleep


def poll_once(client, store: CandleStore, symbols: list[str], now: datetime, stats: dict) -> None:
    for symbol in symbols:
        try:
            last = store.last_open_time(symbol, TF)
            since = last + TF.delta if last else now - timedelta(hours=2)
            rows = client.klines(market_of(symbol), 1, since_s=int(since.timestamp()), limit=1000)
            closed = [c for c in parse_klines(rows, symbol, TF) if c.open_time >= since and c.is_closed(now)]
            if closed:
                store.save_candles(closed, "rest")
                stats["candles"] += len(closed)
            top = best_bid_ask(client.depth(market_of(symbol), 5))
            if top:
                store.save_quotes([Quote(symbol, now, *top)])
                stats["quotes"] += 1
        except VenueError as exc:
            stats["errors"] += 1
            store.record_issue("poll_error", symbol, {"error": str(exc)[:300]})


class Command(BaseCommand):
    help = "Poll Quidax every minute for new candles and spreads."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--interval", type=float, default=60.0)
        parser.add_argument("--max-polls", type=int, default=0, help="0 = run until Ctrl+C")

    def handle(self, *args, **o):
        symbols = quidax_symbols(o["symbols"])
        client = make_quidax()
        try:
            instruments = sync_quidax_markets(client, symbols)
        except (VenueError, ValueError) as exc:
            client.close()
            raise CommandError(f"Cannot start: {exc}") from exc
        store = CandleStore(instruments, run_label="record_quidax")
        stats = {"polls": 0, "candles": 0, "quotes": 0, "errors": 0}
        self.stdout.write(f"Recording {len(symbols)} Quidax markets every {o['interval']:.0f}s. Ctrl+C to stop.")
        try:
            while True:
                t0 = time.monotonic()
                poll_once(client, store, symbols, now_utc(), stats)
                stats["polls"] += 1
                if stats["polls"] % 10 == 0:
                    self.stdout.write(f"[{now_utc():%H:%M} UTC] {stats}")
                if o["max_polls"] and stats["polls"] >= o["max_polls"]:
                    break
                sleep(max(0.0, o["interval"] - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            self.stdout.write("\nStopped by operator.")
        finally:
            client.close()
        self.stdout.write(f"Final stats: {stats}")
