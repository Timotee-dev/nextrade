"""Binance spot websocket messages: combined kline + best bid/ask streams."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from nextrade_core.data.candles import Candle, Timeframe, ms_to_utc

STREAM_URL = "wss://stream.binance.com:9443/stream"


@dataclass(frozen=True, slots=True)
class KlineEvent:
    candle: Candle
    closed: bool


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


def build_stream_url(symbols: list[str], timeframe: Timeframe = Timeframe.M1,
                     quotes: bool = True, base: str = STREAM_URL) -> str:
    streams = []
    for s in symbols:
        streams.append(f"{s.lower()}@kline_{timeframe.value}")
        if quotes:
            streams.append(f"{s.lower()}@bookTicker")
    return f"{base}?streams={'/'.join(streams)}"


def parse_message(raw: str | bytes, received_at: datetime) -> KlineEvent | Quote | None:
    msg = json.loads(raw)
    data = msg.get("data", msg)
    if data.get("e") == "kline":
        k = data["k"]
        candle = Candle(
            symbol=k["s"], timeframe=Timeframe(k["i"]), open_time=ms_to_utc(int(k["t"])),
            open=Decimal(k["o"]), high=Decimal(k["h"]), low=Decimal(k["l"]), close=Decimal(k["c"]),
            volume=Decimal(k["v"]), quote_volume=Decimal(k["q"]), trade_count=int(k["n"]),
        )
        return KlineEvent(candle, bool(k["x"]))
    if {"s", "b", "a", "B", "A"} <= data.keys():
        return Quote(data["s"], received_at, Decimal(data["b"]), Decimal(data["a"]),
                     Decimal(data["B"]), Decimal(data["A"]))
    return None


class QuoteSampler:
    """bookTicker sends many updates a second. Keep one quote per symbol per interval."""

    def __init__(self, interval_s: float = 10.0) -> None:
        self.interval_s = interval_s
        self._last_emit: dict[str, datetime] = {}

    def offer(self, quote: Quote) -> Quote | None:
        last = self._last_emit.get(quote.symbol)
        if last is None or (quote.ts - last).total_seconds() >= self.interval_s:
            self._last_emit[quote.symbol] = quote.ts
            return quote
        return None
