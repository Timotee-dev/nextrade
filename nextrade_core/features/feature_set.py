"""Feature set v1: one function that turns a candle frame into the model's inputs.

Two kinds of columns come out:
* LEVEL columns (prices such as ema_fast or donchian_high) used by strategy rules.
* MODEL columns: scale-free numbers (ratios, oscillators, ATR-normalised distances)
  that mean the same thing on BTC at 60,000 and DOGE at 0.10. Only these feed models.

Every column belongs to one family, so the confirmation framework (spec 4.5) can
treat correlated indicators as one source of evidence, not several.
The version hash changes whenever a parameter or the version tag changes, and is
stored with every signal so any decision can be reproduced.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from nextrade_core.data.candles import Timeframe

from . import indicators as ind
from .structure import structure_features

FEATURE_SET_VERSION = "fs-1"


class Family(StrEnum):
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    VOLUME = "volume"
    STRUCTURE = "structure"


@dataclass(frozen=True)
class FeatureParams:
    ema_fast: int = 20
    ema_slow: int = 50
    sma_long: int = 200
    slope_bars: int = 5
    adx: int = 14
    rsi: int = 14
    stoch: int = 14
    roc: int = 12
    cci: int = 20
    atr: int = 14
    bb: int = 20
    hist_vol: int = 30
    percentile_window: int = 500
    rel_volume: int = 20
    obv_slope: int = 20
    swing_k: int = 3
    donchian: int = 20

    @property
    def warmup_bars(self) -> int:
        """Rows needed before every column is defined."""
        return max(self.sma_long, self.percentile_window + self.atr, 3 * self.adx) + 5


def version_hash(params: FeatureParams) -> str:
    payload = json.dumps({"version": FEATURE_SET_VERSION, **asdict(params)}, sort_keys=True)
    return f"{FEATURE_SET_VERSION}-{hashlib.sha256(payload.encode()).hexdigest()[:10]}"


LEVEL_COLUMNS = ["ema_fast", "ema_slow", "sma_long", "atr", "bb_upper", "bb_lower", "bb_mid",
                 "session_vwap", "last_swing_high", "last_swing_low", "donchian_high", "donchian_low"]

MODEL_FAMILIES: dict[str, Family] = {
    # trend
    "ema_spread_atr": Family.TREND, "close_vs_ema_slow_atr": Family.TREND,
    "close_vs_sma_long_atr": Family.TREND, "ema_fast_slope_atr": Family.TREND,
    "macd_hist_pct": Family.TREND, "adx": Family.TREND, "di_diff": Family.TREND,
    # momentum
    "rsi": Family.MOMENTUM, "stoch_k": Family.MOMENTUM, "stoch_d": Family.MOMENTUM,
    "roc": Family.MOMENTUM, "cci": Family.MOMENTUM,
    # volatility
    "atr_pct": Family.VOLATILITY, "atr_pct_percentile": Family.VOLATILITY,
    "bb_width": Family.VOLATILITY, "bb_width_percentile": Family.VOLATILITY,
    "bb_pct_b": Family.VOLATILITY, "hist_vol": Family.VOLATILITY,
    # volume
    "rel_volume": Family.VOLUME, "volume_change": Family.VOLUME, "obv_slope": Family.VOLUME,
    "vwap_dist_atr": Family.VOLUME,
    # structure
    "structure_trend": Family.STRUCTURE, "higher_high": Family.STRUCTURE,
    "higher_low": Family.STRUCTURE, "lower_high": Family.STRUCTURE, "lower_low": Family.STRUCTURE,
    "break_of_structure_up": Family.STRUCTURE, "break_of_structure_down": Family.STRUCTURE,
    "breakout_up": Family.STRUCTURE, "breakout_down": Family.STRUCTURE,
    "dist_resistance_atr": Family.STRUCTURE, "dist_support_atr": Family.STRUCTURE,
}
MODEL_COLUMNS = list(MODEL_FAMILIES)


def periods_per_year(tf: Timeframe, minutes_per_year: float = 365 * 24 * 60) -> float:
    """Crypto trades 24/7. Forex will pass its own minutes_per_year (about 5 days a week)."""
    return minutes_per_year / tf.minutes


def build_features(df: pd.DataFrame, timeframe: Timeframe,
                   params: FeatureParams | None = None) -> pd.DataFrame:
    """All level and model columns for every row of `df` (rows inside warm-up are NaN)."""
    p = params or FeatureParams()
    close = df["close"]
    atr = ind.atr(df, p.atr)
    safe_atr = atr.replace(0, np.nan)
    ema_fast, ema_slow, sma_long = ind.ema(close, p.ema_fast), ind.ema(close, p.ema_slow), ind.sma(close, p.sma_long)
    macd = ind.macd(close)
    adx = ind.adx(df, p.adx)
    stoch = ind.stochastic(df, p.stoch)
    bb = ind.bollinger(close, p.bb)
    vwap = ind.session_vwap(df)
    obv = ind.obv(df)
    atr_pct = atr / close
    structure = structure_features(df, p.swing_k, p.donchian, atr)

    out = pd.DataFrame(index=df.index)
    # levels
    out["ema_fast"], out["ema_slow"], out["sma_long"], out["atr"] = ema_fast, ema_slow, sma_long, atr
    out[["bb_upper", "bb_lower", "bb_mid"]] = bb[["bb_upper", "bb_lower", "bb_mid"]]
    out["session_vwap"] = vwap
    # trend
    out["ema_spread_atr"] = (ema_fast - ema_slow) / safe_atr
    out["close_vs_ema_slow_atr"] = (close - ema_slow) / safe_atr
    out["close_vs_sma_long_atr"] = (close - sma_long) / safe_atr
    out["ema_fast_slope_atr"] = (ema_fast - ema_fast.shift(p.slope_bars)) / safe_atr
    out["macd_hist_pct"] = 100 * macd["macd_hist"] / close
    out["adx"] = adx["adx"]
    out["di_diff"] = adx["plus_di"] - adx["minus_di"]
    # momentum
    out["rsi"] = ind.rsi(close, p.rsi)
    out["stoch_k"], out["stoch_d"] = stoch["stoch_k"], stoch["stoch_d"]
    out["roc"] = ind.roc(close, p.roc)
    out["cci"] = ind.cci(df, p.cci)
    # volatility
    out["atr_pct"] = atr_pct
    out["atr_pct_percentile"] = ind.rolling_percentile(atr_pct, p.percentile_window)
    out["bb_width"] = bb["bb_width"]
    out["bb_width_percentile"] = ind.rolling_percentile(bb["bb_width"], p.percentile_window)
    out["bb_pct_b"] = bb["bb_pct_b"]
    out["hist_vol"] = ind.historical_volatility(close, p.hist_vol, periods_per_year(timeframe))
    # volume
    out["rel_volume"] = ind.relative_volume(df["volume"], p.rel_volume)
    out["volume_change"] = df["volume"].pct_change().replace([np.inf, -np.inf], np.nan)
    vol_mean = df["volume"].rolling(p.obv_slope, min_periods=p.obv_slope).mean().replace(0, np.nan)
    out["obv_slope"] = (obv - obv.shift(p.obv_slope)) / (p.obv_slope * vol_mean)
    out["vwap_dist_atr"] = (close - vwap) / safe_atr
    # structure
    for col in structure.columns:
        out[col] = structure[col]
    return out[LEVEL_COLUMNS + MODEL_COLUMNS]


def prune_correlated(features: pd.DataFrame, columns: list[str] | None = None,
                     threshold: float = 0.9) -> list[str]:
    """Keep columns in priority order, dropping any whose |correlation| with an already
    kept column exceeds `threshold`. Must be fitted on TRAINING data only."""
    columns = columns or MODEL_COLUMNS
    corr = features[columns].corr().abs()
    kept: list[str] = []
    for col in columns:
        if features[col].notna().sum() < 10:
            continue
        if all(not (corr.loc[col, k] > threshold) for k in kept):
            kept.append(col)
    return kept


def with_features(df: pd.DataFrame, timeframe: Timeframe, params: FeatureParams | None = None) -> pd.DataFrame:
    """OHLCV plus every feature column, on the same index (the frame strategies read)."""
    return df.join(build_features(df, timeframe, params))
