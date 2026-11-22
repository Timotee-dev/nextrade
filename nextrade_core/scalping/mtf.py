"""Higher-timeframe context for fast bars, without look-ahead.

A 1h bar that opens at 10:00 is only known at 11:00. A base bar may therefore read the 1h
context only when its own OPEN time is at or after the 1h bar's close. Columns added:
  htf_trend   +1 uptrend, -1 downtrend, 0 mixed/unknown (EMA slow vs SMA long and close vs SMA long)
  htf_adx     trend strength on the higher timeframe (NaN while warming up)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nextrade_core.data.candles import Timeframe

HTF_COLUMNS = ("htf_trend", "htf_adx")
_FAST = (Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15)
_OHLCV = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def wants_htf(timeframe: Timeframe) -> bool:
    return timeframe in _FAST


def add_htf_context(frame: pd.DataFrame, timeframe: Timeframe, htf: Timeframe = Timeframe.H1) -> pd.DataFrame:
    """Return `frame` with htf_* columns. Never changes existing columns."""
    from nextrade_core.features.feature_set import build_features   # local: avoid import cycle

    out = frame.copy()
    out["htf_trend"] = 0.0
    out["htf_adx"] = np.nan
    if len(frame) < 2 or htf.delta <= timeframe.delta:
        return out
    ohlcv = frame[["open", "high", "low", "close", "volume"]].astype(float)
    # Only COMPLETE higher bars: drop the last (possibly partial) one.
    hi = ohlcv.resample(f"{int(htf.delta.total_seconds() // 60)}min", label="left", closed="left").agg(_OHLCV)
    hi = hi.dropna(subset=["close"]).iloc[:-1]
    if len(hi) < 60:
        return out
    f = build_features(hi, htf)
    up = (f["ema_slow"] > f["sma_long"]) & (hi["close"] > f["sma_long"])
    down = (f["ema_slow"] < f["sma_long"]) & (hi["close"] < f["sma_long"])
    known = f["sma_long"].notna()
    ctx = pd.DataFrame({"htf_trend": np.where(up, 1.0, np.where(down, -1.0, 0.0)) * known.astype(float),
                        "htf_adx": f["adx"]}, index=hi.index)
    ctx["available_at"] = ctx.index + htf.delta
    ctx = ctx.sort_values("available_at")
    base = pd.DataFrame({"t": frame.index}, index=frame.index).sort_values("t")
    merged = pd.merge_asof(base, ctx, left_on="t", right_on="available_at", direction="backward")
    merged.index = base.index
    out["htf_trend"] = merged["htf_trend"].reindex(frame.index).fillna(0.0).to_numpy()
    out["htf_adx"] = merged["htf_adx"].reindex(frame.index).to_numpy()
    return out
