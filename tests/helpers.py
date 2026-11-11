"""Builders for synthetic market data used across tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nextrade_core.data.candles import Candle, Timeframe, utc_to_ms

T0 = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday, 00:00 UTC


def minute(i: int, symbol: str = "BTCUSDT", start: datetime = T0, price: str | None = None,
           volume: str = "1.5") -> Candle:
    """Bar i: gently rising price, internally consistent OHLC."""
    p = Decimal(price) if price else Decimal("60000") + Decimal(i)
    return Candle(symbol, Timeframe.M1, start + timedelta(minutes=i), open=p, high=p + 5, low=p - 5,
                  close=p + 1, volume=Decimal(volume), quote_volume=Decimal(volume) * p, trade_count=10)


def minutes(n: int, symbol: str = "BTCUSDT", start: datetime = T0, skip: set[int] | None = None) -> list[Candle]:
    return [minute(i, symbol, start) for i in range(n) if not skip or i not in skip]


def to_row(c: Candle, micros: bool = False) -> list[str]:
    ot = utc_to_ms(c.open_time)
    ct = utc_to_ms(c.close_time) - 1
    if micros:
        ot, ct = ot * 1000, ct * 1000 + 999
    return [str(ot), str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume), str(ct),
            str(c.quote_volume), str(c.trade_count), "0", "0", "0"]


def random_walk_frame(n: int, seed: int = 7, start: datetime = T0, minutes_per_bar: int = 1):
    """Realistic float OHLCV frame: geometric random walk with volatility clusters."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    vol = 0.002 * np.exp(np.cumsum(rng.normal(0, 0.05, n)).clip(-1.5, 1.5))
    ret = rng.normal(0, 1, n) * vol
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0, 1, n)) * vol * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 1, n)) * vol * close
    volume = rng.lognormal(3, 0.6, n)
    idx = pd.date_range(start, periods=n, freq=f"{minutes_per_bar}min", tz="UTC", name="open_time")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume,
                         "quote_volume": volume * close, "trades": rng.integers(5, 50, n)}, index=idx)
