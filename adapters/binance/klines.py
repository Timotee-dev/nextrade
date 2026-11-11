"""Kline rows (archive CSV or REST) to Candles, and paginated REST backfill."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from nextrade_core.data.candles import Candle, Timeframe, ms_to_utc, utc_to_ms

from .rest import BinanceRest

MAX_LIMIT = 1000


def row_to_candle(row: Sequence[Any], symbol: str, timeframe: Timeframe) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=ms_to_utc(int(row[0])),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
        quote_volume=Decimal(str(row[7])),
        trade_count=int(row[8]),
    )


def fetch_closed_klines(client: BinanceRest, symbol: str, timeframe: Timeframe,
                        start: datetime, now: datetime) -> list[Candle]:
    """All CLOSED bars with open_time >= start, paging 1,000 at a time. The forming bar is dropped."""
    out: list[Candle] = []
    cursor = start
    while True:
        rows = client.klines(symbol, timeframe.value, start_ms=utc_to_ms(cursor), limit=MAX_LIMIT)
        if not rows:
            break
        batch = [row_to_candle(r, symbol, timeframe) for r in rows]
        closed = [c for c in batch if c.is_closed(now)]
        out.extend(closed)
        if len(rows) < MAX_LIMIT or len(closed) < len(batch):
            break
        cursor = batch[-1].open_time + timeframe.delta
    return out
