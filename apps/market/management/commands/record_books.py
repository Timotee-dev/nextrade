"""Record Quidax NGN order books and world prices every few seconds (microstructure research).

    python manage.py record_books                 (every 10 s, until Ctrl+C)

Per cycle: the top 5 levels of each Quidax NGN market (plus USDT/NGN), and the Gate.io best
bid/ask of the matching USDT pair (the "world" price). 8 Quidax + 7 Gate requests per cycle:
at 10 s that is 48 Quidax requests a minute (limit 300). About 130,000 rows a day.
Analyse with: python manage.py analyze_quidax_edge
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from django.core.management.base import BaseCommand

from adapters.base import VenueError
from adapters.gate.client import GateClient
from apps.market.models import BookSnapshot

from ._quidax import make_quidax

DEFAULT_BASES = "BTC,ETH,SOL,LTC,XRP,TRX,DASH"
now_utc = lambda: datetime.now(UTC)  # noqa: E731
sleep = time.sleep
make_gate = lambda: GateClient()  # noqa: E731


def _levels(rows, n: int, best_first_high: bool) -> list[list[str]]:
    out = sorted(([str(r[0]), str(r[1])] for r in rows or []), key=lambda r: float(r[0]),
                 reverse=best_first_high)
    return out[:n]


def poll_once(qx, gate, bases: list[str], levels: int, now: datetime, stats: dict) -> None:
    rows = []
    for market in [b.lower() + "ngn" for b in bases] + ["usdtngn"]:
        try:
            d = qx.depth(market, levels)
            bids, asks = _levels(d.get("bids"), levels, True), _levels(d.get("asks"), levels, False)
            if bids and asks:
                rows.append(BookSnapshot(venue="quidax", symbol=market.upper(), ts=now, bids=bids, asks=asks))
        except VenueError as exc:
            stats["errors"] += 1
            stats["last_error"] = f"quidax {market}: {str(exc)[:120]}"
    for base in bases:
        try:
            top = gate.ticker(f"{base}_USDT")
            if top:
                rows.append(BookSnapshot(venue="gate", symbol=f"{base}USDT", ts=now,
                                         bids=[[top[0], "0"]], asks=[[top[1], "0"]]))
        except VenueError as exc:
            stats["errors"] += 1
            stats["last_error"] = f"gate {base}: {str(exc)[:120]}"
    BookSnapshot.objects.bulk_create(rows)
    stats["snapshots"] += len(rows)


class Command(BaseCommand):
    help = "Record Quidax NGN order books and Gate.io world prices every few seconds."

    def add_arguments(self, parser):
        parser.add_argument("--bases", default=DEFAULT_BASES, help="coins, e.g. BTC,ETH,SOL")
        parser.add_argument("--interval", type=float, default=10.0)
        parser.add_argument("--levels", type=int, default=5)
        parser.add_argument("--max-polls", type=int, default=0, help="0 = run until Ctrl+C")

    def handle(self, *args, **o):
        bases = [b.strip().upper() for b in o["bases"].split(",") if b.strip()]
        for name in ("httpx", "httpcore"):          # one status line an hour instead of 15 lines per poll
            logging.getLogger(name).setLevel(logging.WARNING)
        qx, gate = make_quidax(), make_gate()
        stats = {"polls": 0, "snapshots": 0, "errors": 0}
        self.stdout.write(f"Recording Quidax books ({', '.join(bases)}, USDT) and Gate.io prices every "
                          f"{o['interval']:.0f}s. Leave running for 2-4 weeks. Ctrl+C to stop.")
        last_report = None
        try:
            while True:
                t0 = time.monotonic()
                now = now_utc()
                poll_once(qx, gate, bases, o["levels"], now, stats)
                stats["polls"] += 1
                hour = now.replace(minute=0, second=0, microsecond=0)
                if hour != last_report:
                    last_report = hour
                    total = BookSnapshot.objects.count()
                    err = f", last error: {stats['last_error']}" if stats.get("last_error") else ""
                    self.stdout.write(f"[{now:%Y-%m-%d %H:%M} UTC] alive · {stats['polls']} polls · "
                                      f"{total:,} snapshots stored · {stats['errors']} errors{err}")
                if o["max_polls"] and stats["polls"] >= o["max_polls"]:
                    break
                sleep(max(0.0, o["interval"] - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
        finally:
            qx.close()
            gate.close()
        self.stdout.write(f"Stopped after {stats['polls']} polls, {stats['snapshots']} snapshots, "
                          f"{stats['errors']} errors.")
