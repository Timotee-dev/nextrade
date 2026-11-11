"""Record live 1m candles and sampled best bid/ask from Binance until stopped (Ctrl+C)."""

from __future__ import annotations

import asyncio
import logging

from django.core.management.base import BaseCommand
from websockets.asyncio.client import connect as ws_connect

from adapters.binance.klines import fetch_closed_klines
from apps.market.services import ensure_instruments
from apps.market.store import CandleStore
from nextrade_core.clock import WallClock
from nextrade_core.data.candles import Timeframe
from services.market_recorder import LiveRecorder

from adapters.base import VenueError

from ._common import make_rest, parse_symbols, unreachable

log = logging.getLogger("nextrade.recorder")


class Command(BaseCommand):
    help = "Stream Binance klines and quotes into the database, repairing gaps automatically."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--quote-interval", type=float, default=10.0, help="seconds between stored quotes")
        parser.add_argument("--stats-every", type=float, default=300.0, help="seconds between status lines")

    def handle(self, *args, **opts):
        symbols = parse_symbols(opts["symbols"])
        rest = make_rest()
        try:
            store = CandleStore(ensure_instruments(rest, symbols), run_label="record_live")
        except VenueError as exc:
            rest.close()
            raise unreachable(exc) from exc
        clock = WallClock()
        recorder = LiveRecorder(
            symbols, store,
            fetch_closed=lambda s, start, now: fetch_closed_klines(rest, s, Timeframe.M1, start, now),
            connect=lambda url: ws_connect(url, ping_interval=20, ping_timeout=20, max_size=2**20),
            clock=clock, quote_interval_s=opts["quote_interval"],
        )
        self.stdout.write(f"Recording {len(symbols)} symbols. Press Ctrl+C to stop.")
        try:
            asyncio.run(self._main(recorder, opts["stats_every"]))
        except KeyboardInterrupt:
            self.stdout.write("\nStopped by operator.")
        finally:
            rest.close()
        self.stdout.write(f"Final stats: {recorder.stats}")

    async def _main(self, recorder: LiveRecorder, stats_every: float) -> None:
        stop = asyncio.Event()

        async def report():
            while not stop.is_set():
                await asyncio.sleep(stats_every)
                self.stdout.write(f"[{WallClock().now():%H:%M} UTC] {recorder.stats}")

        reporter = asyncio.create_task(report())
        try:
            await recorder.run(stop)
        finally:
            stop.set()
            reporter.cancel()
