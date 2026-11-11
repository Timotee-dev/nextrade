"""Price structure (spec 4.2): swings, higher highs/lows, support, resistance, breakouts.

Leakage trap: a swing high at bar i is only KNOWN k bars later, once k lower highs
have printed after it. Every series here is stamped at the CONFIRMATION bar, never
at the pivot bar, so a backtest cannot see a swing before it could have been seen.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def confirmed_swings(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """At each bar t, the swing high/low whose pivot was bar t-k, if one was confirmed at t.

    Pivot high at i: high[i] is the maximum of high[i-k .. i+k] and strictly above
    every high in high[i-k .. i-1] (ties go to the earlier bar).
    """
    w = 2 * k + 1
    hk, lk = df["high"].shift(k), df["low"].shift(k)
    win_max = df["high"].rolling(w, min_periods=w).max()
    win_min = df["low"].rolling(w, min_periods=w).min()
    left_max = df["high"].shift(k + 1).rolling(k, min_periods=k).max()
    left_min = df["low"].shift(k + 1).rolling(k, min_periods=k).min()
    is_high = (hk == win_max) & (hk > left_max)
    is_low = (lk == win_min) & (lk < left_min)
    return pd.DataFrame({"swing_high": hk.where(is_high), "swing_low": lk.where(is_low)})


def _last_and_previous(levels: pd.Series) -> tuple[pd.Series, pd.Series]:
    events = levels.dropna()
    last = levels.ffill()
    prev = events.shift(1).reindex(levels.index).ffill()
    return last, prev


def structure_features(df: pd.DataFrame, k: int = 3, donchian: int = 20,
                       atr_series: pd.Series | None = None) -> pd.DataFrame:
    swings = confirmed_swings(df, k)
    last_high, prev_high = _last_and_previous(swings["swing_high"])
    last_low, prev_low = _last_and_previous(swings["swing_low"])
    hh = (last_high > prev_high).astype(float).where(prev_high.notna())
    hl = (last_low > prev_low).astype(float).where(prev_low.notna())
    lh = (last_high < prev_high).astype(float).where(prev_high.notna())
    ll = (last_low < prev_low).astype(float).where(prev_low.notna())
    trend = pd.Series(np.select([(hh == 1) & (hl == 1), (lh == 1) & (ll == 1)], [1.0, -1.0], 0.0),
                      index=df.index).where(hh.notna() & hl.notna())

    close = df["close"]
    don_high = df["high"].shift(1).rolling(donchian, min_periods=donchian).max()
    don_low = df["low"].shift(1).rolling(donchian, min_periods=donchian).min()
    out = pd.DataFrame({
        "last_swing_high": last_high, "last_swing_low": last_low,
        "higher_high": hh, "higher_low": hl, "lower_high": lh, "lower_low": ll,
        "structure_trend": trend,
        "break_of_structure_up": (close > last_high).astype(float).where(last_high.notna()),
        "break_of_structure_down": (close < last_low).astype(float).where(last_low.notna()),
        "donchian_high": don_high, "donchian_low": don_low,
        "breakout_up": (close > don_high).astype(float).where(don_high.notna()),
        "breakout_down": (close < don_low).astype(float).where(don_low.notna()),
    }, index=df.index)
    if atr_series is not None:
        safe_atr = atr_series.replace(0, np.nan)
        out["dist_resistance_atr"] = (last_high - close) / safe_atr
        out["dist_support_atr"] = (close - last_low) / safe_atr
    return out
