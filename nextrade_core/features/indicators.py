"""Technical indicators (spec 4.2). Every function is causal: the value at row t
uses rows <= t only. tests/unit/test_features.py checks this for every indicator,
and checks the classic ones against the independent `ta` library.

Smoothing conventions (stated because they change the numbers):
* EMA: span n, no bias adjustment, first value after n bars.
* RSI, ATR, ADX: Wilder smoothing (alpha = 1/n), first value seeded with a simple
  average of the first n inputs, as in Wilder's original definition.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------- helpers
def wilder(series: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing seeded with the simple mean of the first n valid values."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return pd.Series(out, index=series.index)
    first = valid[0]
    seed_end = first + n - 1
    if np.isnan(values[first:seed_end + 1]).any():
        raise ValueError("wilder() needs a contiguous run of values after the first one")
    out[seed_end] = values[first:seed_end + 1].mean()
    for i in range(seed_end + 1, len(values)):
        out[i] = (out[i - 1] * (n - 1) + values[i]) / n
    return pd.Series(out, index=series.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = np.nan  # no previous close: undefined (matches ta)
    return tr


# ----------------------------------------------------------------------------- trend
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, min_periods=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    plus_dm.iloc[0] = minus_dm.iloc[0] = np.nan
    atr_ = wilder(true_range(df), n)
    plus_di = 100 * wilder(plus_dm, n) / atr_
    minus_di = 100 * wilder(minus_dm, n) / atr_
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return pd.DataFrame({"adx": wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


# ----------------------------------------------------------------------------- momentum
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), n)
    loss = wilder(-delta.clip(upper=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3) -> pd.DataFrame:
    low_n = df["low"].rolling(n, min_periods=n).min()
    high_n = df["high"].rolling(n, min_periods=n).max()
    k = 100 * (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan)
    return pd.DataFrame({"stoch_k": k, "stoch_d": sma(k, d)})


def roc(close: pd.Series, n: int = 12) -> pd.Series:
    return 100 * (close - close.shift(n)) / close.shift(n)


def cci(df: pd.DataFrame, n: int = 20, c: float = 0.015) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mean = tp.rolling(n, min_periods=n).mean()
    mad = tp.rolling(n, min_periods=n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - mean) / (c * mad)


# ----------------------------------------------------------------------------- volatility
def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower,
                         "bb_width": width, "bb_pct_b": pct_b})


def historical_volatility(close: pd.Series, n: int, periods_per_year: float) -> pd.Series:
    """Annualised standard deviation of log returns over n bars."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(periods_per_year)


def rolling_percentile(s: pd.Series, n: int) -> pd.Series:
    """Where the current value ranks among the last n values (0 to 1), current included."""
    return s.rolling(n, min_periods=n).rank(pct=True)


# ----------------------------------------------------------------------------- volume
def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def rolling_vwap(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).rolling(n, min_periods=n).sum()
    return pv / df["volume"].rolling(n, min_periods=n).sum().replace(0, np.nan)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP reset at 00:00 UTC each day."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    return pv / vol.replace(0, np.nan)


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    """Current volume divided by the average of the PREVIOUS n bars."""
    return volume / volume.shift(1).rolling(n, min_periods=n).mean().replace(0, np.nan)
