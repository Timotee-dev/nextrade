"""Live market-data recorder: Binance websocket -> storage, with gap repair.

Sequence on every (re)connection:
  1. open the websocket (messages start buffering immediately),
  2. backfill each symbol over REST from its last stored bar up to now,
  3. consume the stream.
Because the socket is open before the backfill starts, there is no window in
which a bar can be missed. Duplicates are ignored by the store's unique key.

The recorder also reconnects when the stream goes quiet for `idle_timeout_s`
(a half-dead connection is treated as dead) and repairs any jump in bar times.
Framework-free: storage, REST backfill, websocket connection and clock are injected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any, Protocol

from adapters.binance.stream import KlineEvent, Quote, QuoteSampler, build_stream_url, parse_message
from nextrade_core.clock import Clock
from nextrade_core.data.candles import Candle, Timeframe

log = logging.getLogger("nextrade.recorder")


class CandleSink(Protocol):
    def last_open_time(self, symbol: str, timeframe: Timeframe) -> datetime | None: ...
    def save_candles(self, candles: list[Candle], source: str) -> int: ...
    def save_quotes(self, quotes: list[Quote]) -> int: ...
    def record_issue(self, kind: str, symbol: str | None, detail: dict) -> None: ...


class StaleStreamError(Exception):
    pass


Fetcher = Callable[[str, datetime, datetime], list[Candle]]  # (symbol, start, now) -> closed bars
Connector = Callable[[str], AbstractAsyncContextManager[Any]]


class LiveRecorder:
    def __init__(self, symbols: list[str], sink: CandleSink, fetch_closed: Fetcher,
                 connect: Connector, clock: Clock, *, timeframe: Timeframe = Timeframe.M1,
                 quote_interval_s: float = 10.0, idle_timeout_s: float = 60.0,
                 initial_lookback: timedelta = timedelta(hours=1), max_backoff_s: float = 60.0,
                 max_reconnects: int | None = None) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.sink = sink
        self.fetch_closed = fetch_closed
        self.connect = connect
        self.clock = clock
        self.tf = timeframe
        self.idle_timeout_s = idle_timeout_s
        self.initial_lookback = initial_lookback
        self.max_backoff_s = max_backoff_s
        self.max_reconnects = max_reconnects
        self.sampler = QuoteSampler(quote_interval_s)
        self.url = build_stream_url(self.symbols, timeframe)
        self._last_saved: dict[str, datetime] = {}
        self._quotes: list[Quote] = []
        self.stats = {"connections": 0, "reconnects": 0, "candles_ws": 0, "candles_rest": 0,
                      "quotes": 0, "gaps_repaired": 0}

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with self.connect(self.url) as ws:
                    self.stats["connections"] += 1
                    backoff = 1.0
                    log.info("connected (%d symbols)", len(self.symbols))
                    await self._backfill_all()
                    await self._consume(ws, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any failure -> reconnect with backoff
                self.stats["reconnects"] += 1
                log.warning("stream ended: %r; reconnecting in %.0fs", exc, backoff)
                await asyncio.to_thread(self.sink.record_issue, "ws_disconnect", None,
                                        {"error": repr(exc)[:500]})
                if self.max_reconnects is not None and self.stats["reconnects"] > self.max_reconnects:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, self.max_backoff_s)
        await self._flush_quotes()

    async def _consume(self, ws: Any, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        last_message = loop.time()
        last_flush = loop.time()
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                if loop.time() - last_message > self.idle_timeout_s:
                    raise StaleStreamError(f"no message for {self.idle_timeout_s:.0f}s")
                continue
            last_message = loop.time()
            event = parse_message(raw, self.clock.now())
            if isinstance(event, KlineEvent) and event.closed:
                await self._on_closed_bar(event.candle)
            elif isinstance(event, Quote):
                sampled = self.sampler.offer(event)
                if sampled is not None:
                    self._quotes.append(sampled)
            if self._quotes and (len(self._quotes) >= 50 or loop.time() - last_flush >= 10):
                await self._flush_quotes()
                last_flush = loop.time()

    # ------------------------------------------------------------------ bars
    async def _on_closed_bar(self, candle: Candle) -> None:
        last = self._last_saved.get(candle.symbol)
        if last is not None and candle.open_time <= last:
            return  # already stored (e.g. by backfill)
        if last is not None and candle.open_time > last + self.tf.delta:
            # the stream skipped bars: repair from REST before storing this one
            self.stats["gaps_repaired"] += 1
            await asyncio.to_thread(self.sink.record_issue, "ws_gap", candle.symbol,
                                    {"after": last.isoformat(), "next": candle.open_time.isoformat()})
            await self._backfill(candle.symbol)
        await asyncio.to_thread(self.sink.save_candles, [candle], "ws")
        self.stats["candles_ws"] += 1
        self._last_saved[candle.symbol] = max(candle.open_time, self._last_saved.get(candle.symbol, candle.open_time))

    async def _backfill_all(self) -> None:
        for symbol in self.symbols:
            await self._backfill(symbol)

    async def _backfill(self, symbol: str) -> None:
        now = self.clock.now()
        last = await asyncio.to_thread(self.sink.last_open_time, symbol, self.tf)
        start = last + self.tf.delta if last else self.tf.floor(now - self.initial_lookback)
        candles = await asyncio.to_thread(self.fetch_closed, symbol, start, now)
        if candles:
            await asyncio.to_thread(self.sink.save_candles, candles, "rest")
            self.stats["candles_rest"] += len(candles)
            self._last_saved[symbol] = candles[-1].open_time
        elif last is not None:
            self._last_saved[symbol] = last

    async def _flush_quotes(self) -> None:
        if not self._quotes:
            return
        batch, self._quotes = self._quotes, []
        await asyncio.to_thread(self.sink.save_quotes, batch)
        self.stats["quotes"] += len(batch)
