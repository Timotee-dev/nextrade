"""Database side of the dataset coverage check (pure logic lives in nextrade_core.data.coverage)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from django.db.models import Count

from nextrade_core.data.coverage import PairReport, Thresholds, assess

from .models import Candle as CandleRow
from .models import Instrument


def load_1m(inst: Instrument, start: datetime | None = None) -> pd.DataFrame:
    qs = CandleRow.objects.filter(instrument=inst, timeframe="1m")
    if start is not None:
        qs = qs.filter(open_time__gte=start)
    rows = list(qs.order_by("open_time").values_list("open_time", "open", "high", "low", "close", "volume"))
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC"))
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df.set_index("t").astype(float)


def duplicates_for(inst: Instrument) -> int:
    d = (CandleRow.objects.filter(instrument=inst, timeframe="1m").values("open_time")
         .annotate(n=Count("id")).filter(n__gt=1).count())
    return d


def check_pairs(instruments: list[Instrument], now: datetime, *, window_days: float | None = None,
                th: Thresholds | None = None) -> list[PairReport]:
    start = now - timedelta(days=window_days) if window_days else None
    return [assess(i.symbol, load_1m(i, start), pd.Timestamp(now), duplicates=duplicates_for(i), th=th)
            for i in instruments]
