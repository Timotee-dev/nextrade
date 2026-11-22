"""Bybit v5 PUBLIC spot WebSocket: best bid/ask (orderbook.1) and every trade (publicTrade).

READ-ONLY by construction: the only messages this client can send are subscribe, unsubscribe and ping.
Any other operation (an order, a cancel, an auth request) raises before it reaches the socket.
Reconnects with backoff, pings, and drops a silent connection (no message for `silence_s`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nextrade_core.hft.events import Quote, Trade

log = logging.getLogger("nextrade.ws")
URL = "wss://stream.bybit.com/v5/public/spot"
ALLOWED_OPS = {"subscribe", "unsubscribe", "ping"}
MAX_ARGS = 10                       # Bybit spot: at most 10 topics per subscribe request


class ForbiddenWsOperation(RuntimeError):
    """Raised when something tries to send anything but a subscription or a ping."""


def check_op(msg: dict) -> dict:
    if msg.get("op") not in ALLOWED_OPS:
        raise ForbiddenWsOperation(f"blocked websocket operation {msg.get('op')!r}: the feed is read-only")
    return msg


def topics(symbols: list[str]) -> list[str]:
    return [t for s in symbols for t in (f"orderbook.1.{s}", f"publicTrade.{s}")]


def subscribe_messages(symbols: list[str]) -> list[dict]:
    ts = topics(symbols)
    return [check_op({"op": "subscribe", "args": ts[i:i + MAX_ARGS]}) for i in range(0, len(ts), MAX_ARGS)]


class BookState:
    """Level-1 state per symbol: snapshots replace it, deltas overwrite a side (size 0 = that level is gone)."""

    def __init__(self) -> None:
        self.bid: dict[str, tuple[float, float] | None] = {}
        self.ask: dict[str, tuple[float, float] | None] = {}

    def apply(self, symbol: str, kind: str, data: dict, now: float | None = None) -> Quote | None:
        if kind == "snapshot":
            self.bid[symbol] = self.ask[symbol] = None
        for key, store in (("b", self.bid), ("a", self.ask)):
            rows = data.get(key) or []
            if rows:
                p, s = float(rows[0][0]), float(rows[0][1])
                store[symbol] = (p, s) if s > 0 else None
        b, a = self.bid.get(symbol), self.ask.get(symbol)
        if b is None or a is None or a[0] <= b[0]:       # an empty or crossed book is not a quote
            return None
        return Quote(symbol, time.time() if now is None else now, b[0], a[0], b[1], a[1])


def parse(raw: str | bytes, book: BookState, now: float | None = None) -> list[Quote | Trade]:
    """Turn one websocket message into events. Unknown or administrative messages give []."""
    try:
        m = json.loads(raw)
    except ValueError:
        return []
    topic = m.get("topic")
    if not topic:
        return []
    t = time.time() if now is None else now
    if topic.startswith("orderbook.1."):
        d = m.get("data") or {}
        sym = d.get("s") or topic.rsplit(".", 1)[-1]
        q = book.apply(sym, m.get("type", "delta"), d, t)
        return [q] if q is not None else []
    if topic.startswith("publicTrade."):
        out = []
        for r in m.get("data") or []:
            try:
                out.append(Trade(r["s"], t, float(r["p"]), float(r["v"]), r.get("S") == "Buy"))
            except (KeyError, ValueError):
                continue
        return out
    return []


class BybitWs:
    def __init__(self, symbols: list[str], on_event: Callable[[Quote | Trade], Any], *, url: str = URL,
                 connect: Callable[..., Any] | None = None, silence_s: float = 15.0, ping_s: float = 20.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.symbols, self.on_event, self.url = symbols, on_event, url
        self.connect, self.silence_s, self.ping_s, self._sleep = connect, silence_s, ping_s, sleep
        self.stats = {"connects": 0, "messages": 0, "events": 0, "reconnects": 0, "last_message": 0.0, "last_error": ""}

    async def _send(self, ws, msg: dict) -> None:
        await ws.send(json.dumps(check_op(msg)))

    async def _pinger(self, ws) -> None:
        while True:
            await self._sleep(self.ping_s)
            await self._send(ws, {"op": "ping"})

    async def _session(self, stop: asyncio.Event) -> None:
        connect = self.connect
        if connect is None:
            import websockets
            connect = websockets.connect
        book = BookState()
        async with connect(self.url, ping_interval=None, max_size=2 ** 22) as ws:
            self.stats["connects"] += 1
            for msg in subscribe_messages(self.symbols):
                await self._send(ws, msg)
            pinger = asyncio.create_task(self._pinger(ws))
            try:
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.silence_s)
                    self.stats["messages"] += 1
                    self.stats["last_message"] = time.time()
                    for ev in parse(raw, book):
                        self.stats["events"] += 1
                        self.on_event(ev)
            finally:
                pinger.cancel()

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self._session(stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # noqa: BLE001  any network trouble: reconnect
                self.stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                self.stats["reconnects"] += 1
                log.warning("feed dropped (%s); reconnecting in %.0fs", self.stats["last_error"], delay)
            if stop.is_set():
                break
            delay = 1.0 if time.monotonic() - started > 60 else min(30.0, delay * 2)
            await self._sleep(delay)
