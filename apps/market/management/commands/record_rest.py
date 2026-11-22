"""Live recorder over public REST (no keys, no websockets): 1m candles and best bid/ask.

    python manage.py record_rest --venue bybit            # symbols = those already stored for the venue

Feeds the paper trader (run_trader --venue bybit) and builds a real spread history
(QuoteSample), which later replaces the assumed spreads in the cost model. Each cycle it
fetches the newest closed 1m bars for every symbol and ONE call for all quotes. A failing
symbol is skipped and counted; the status line shows errors. Stale data is never repaired by guessing.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.bybit.client import BybitClient
from adapters.gate.client import GateClient
from apps.market.models import Instrument, QuoteSample
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

now_utc = lambda: datetime.now(UTC)  # noqa: E731
sleep = time.sleep
CLIENTS = {"bybit": BybitClient, "gate": GateClient}


def poll_once(client, store: CandleStore, instruments: dict[str, Instrument], now: datetime, stats: dict) -> None:
    tf = Timeframe.M1
    end = tf.floor(now)
    for sym in instruments:
        last = store.last_open_time(sym, tf)
        start = (last + tf.delta) if last else end - timedelta(minutes=15)
        if start >= end:
            continue
        try:
            bars = client.candles(sym, tf, max(start, end - timedelta(minutes=300)), end)
            store.save_candles(bars, "rest")
            stats["bars"] += len(bars)
        except VenueError as exc:
            stats["errors"] += 1
            stats["last_error"] = f"{sym}: {str(exc)[:100]}"
    try:
        quotes = client.quotes()
    except VenueError as exc:
        stats["errors"] += 1
        stats["last_error"] = f"quotes: {str(exc)[:100]}"
        return
    rows = []
    for sym, inst in instruments.items():
        q = quotes.get(sym)
        if q and Decimal(q[1]) > Decimal(q[0]) > 0:                 # a crossed or empty book is not a quote
            rows.append(QuoteSample(instrument=inst, ts=now, bid=Decimal(q[0]), ask=Decimal(q[1]),
                                    bid_qty=Decimal(0), ask_qty=Decimal(0)))
    QuoteSample.objects.bulk_create(rows)
    stats["quotes"] += len(rows)


class Command(BaseCommand):
    help = "Record 1m candles and best bid/ask over REST for a venue."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default="bybit", choices=sorted(CLIENTS))
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--interval", type=float, default=20.0)
        parser.add_argument("--max-polls", type=int, default=0)
        parser.add_argument("--stop-file", default=None, help="exit cleanly when this file appears")

    def handle(self, *args, **o):
        logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per request would drown the log
        qs = Instrument.objects.filter(venue=o["venue"], candles__isnull=False).distinct()
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = {i.symbol: i for i in qs}
        if not instruments:
            raise CommandError(f"No {o['venue']} instruments with stored candles. Run load_{o['venue']}_history first.")
        store, client = CandleStore(instruments, run_label="record_rest"), CLIENTS[o["venue"]]()
        stats = {"polls": 0, "bars": 0, "quotes": 0, "errors": 0}
        self.stdout.write(f"Recording {len(instruments)} {o['venue']} symbols every {o['interval']:.0f}s. Ctrl+C to stop.")
        try:
            while True:
                t0 = time.monotonic()
                now = now_utc()
                if o["stop_file"] and Path(o["stop_file"]).exists():
                    break
                try:
                    poll_once(client, store, instruments, now, stats)
                except Exception as exc:               # noqa: BLE001  one bad poll must not end the recorder
                    stats["errors"] += 1
                    stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
                stats["polls"] += 1
                if stats["polls"] % 30 == 1:
                    err = f", last error: {stats['last_error']}" if stats.get("last_error") else ""
                    self.stdout.write(f"[{now:%Y-%m-%d %H:%M} UTC] alive · {stats['polls']} polls · {stats['bars']} bars · "
                                      f"{stats['quotes']} quotes · {stats['errors']} errors{err}")
                if o["max_polls"] and stats["polls"] >= o["max_polls"]:
                    break
                sleep(max(0.0, o["interval"] - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
        finally:
            client.close()
        self.stdout.write(f"Stopped after {stats['polls']} polls.")
