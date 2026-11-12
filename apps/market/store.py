"""Django-backed candle storage (implements services.market_recorder.CandleSink)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from django.db import close_old_connections
from django.db.models import Max

from adapters.binance.stream import Quote
import pandas as pd

from nextrade_core.data.candles import Candle, Timeframe
from nextrade_core.data.resample import resample, resample_from
from nextrade_core.features.frame import candles_to_frame

from .models import Candle as CandleRow
from .models import DataQualityEvent, Instrument, QuoteSample

BATCH = 5000


class CandleStore:
    def __init__(self, instruments: dict[str, Instrument], run_label: str = "") -> None:
        self.instruments = instruments
        self.run_label = run_label

    def _inst(self, symbol: str) -> Instrument:
        return self.instruments[symbol.upper()]

    def first_open_time(self, symbol: str, timeframe: Timeframe = Timeframe.M1) -> datetime | None:
        from django.db.models import Min
        close_old_connections()
        return (CandleRow.objects.filter(instrument=self._inst(symbol), timeframe=timeframe.value)
                .aggregate(m=Min("open_time"))["m"])

    def last_open_time(self, symbol: str, timeframe: Timeframe = Timeframe.M1) -> datetime | None:
        close_old_connections()
        return (CandleRow.objects.filter(instrument=self._inst(symbol), timeframe=timeframe.value)
                .aggregate(m=Max("open_time"))["m"])

    def count(self, symbol: str, start: datetime, end: datetime,
              timeframe: Timeframe = Timeframe.M1) -> int:
        return CandleRow.objects.filter(instrument=self._inst(symbol), timeframe=timeframe.value,
                                        open_time__gte=start, open_time__lt=end).count()

    def save_candles(self, candles: list[Candle], source: str) -> int:
        """Insert, ignoring bars already stored. Returns the number offered."""
        close_old_connections()
        rows = [
            CandleRow(instrument=self._inst(c.symbol), timeframe=c.timeframe.value,
                      open_time=c.open_time, open=c.open, high=c.high, low=c.low, close=c.close,
                      volume=c.volume, quote_volume=c.quote_volume, trade_count=c.trade_count,
                      source=source)
            for c in candles
        ]
        for i in range(0, len(rows), BATCH):
            CandleRow.objects.bulk_create(rows[i:i + BATCH], ignore_conflicts=True)
        return len(rows)

    def save_quotes(self, quotes: list[Quote]) -> int:
        close_old_connections()
        QuoteSample.objects.bulk_create([
            QuoteSample(instrument=self._inst(q.symbol), ts=q.ts, bid=q.bid, ask=q.ask,
                        bid_qty=q.bid_qty, ask_qty=q.ask_qty) for q in quotes
        ])
        return len(quotes)

    def record_issue(self, kind: str, symbol: str | None, detail: dict, *,
                     timeframe: str = "", at: datetime | None = None) -> None:
        close_old_connections()
        DataQualityEvent.objects.create(
            instrument=self._inst(symbol) if symbol else None, timeframe=timeframe,
            kind=kind, at=at, detail=detail, run_label=self.run_label,
        )

    def iter_candles(self, symbol: str, start: datetime, end: datetime,
                     timeframe: Timeframe = Timeframe.M1, chunk: int = 50_000) -> Iterator[list[Candle]]:
        """Stored bars in [start, end) as domain Candles, in chunks, oldest first."""
        cursor = start
        inst = self._inst(symbol)
        while cursor < end:
            rows = list(CandleRow.objects.filter(instrument=inst, timeframe=timeframe.value,
                                                 open_time__gte=cursor, open_time__lt=end)
                        .order_by("open_time")
                        .values_list("open_time", "open", "high", "low", "close", "volume",
                                     "quote_volume", "trade_count")[:chunk])
            if not rows:
                return
            yield [Candle(inst.symbol, timeframe, t, o, h, low, c, v, q, n)
                   for t, o, h, low, c, v, q, n in rows]
            if len(rows) < chunk:
                return
            cursor = rows[-1][0] + timeframe.delta

    def load_frame(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
        """Float OHLCV frame at any timeframe: native bars at that timeframe if stored,
        otherwise resampled from stored 1m bars (complete buckets only).
        """
        if timeframe is not Timeframe.M1:
            native = [c for chunk in self.iter_candles(symbol, start, end, timeframe) for c in chunk]
            if native:  # stored at this timeframe already (e.g. Quidax native 1h/4h k-lines)
                return candles_to_frame(native)
        bars = [c for chunk in self.iter_candles(symbol, start, end) for c in chunk]
        if timeframe is not Timeframe.M1:
            if timeframe.minutes % 60 == 0 and timeframe is not Timeframe.H1:
                hourly = [c for chunk in self.iter_candles(symbol, start, end, Timeframe.H1) for c in chunk]
                if hourly and len(hourly) * 60 >= len(bars):   # 1h coverage beats 1m: build from 1h
                    return candles_to_frame(resample_from(hourly, timeframe))
            bars = resample(bars, timeframe).candles
        return candles_to_frame(bars)
