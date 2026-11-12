"""Pre-registered forex strategies FX1-FX3 (ADR 0007). Parameters fixed BEFORE any forex test.

All three trade BOTH directions (column `short` in the plan), exit only by stop, target or
time (no invalidation exits, so the random baseline gets identical exits), and are judged on
their raw trades. Each also has to be positive in BOTH halves of the out-of-sample period.

FX1 fx_trend   (1h): 20-bar channel breakout in the direction of the 50/200 average trend.
FX2 fx_meanrev (1h): RSI extreme fade when there is no trend (ADX below 20).
FX3 fx_london  (15m): breakout of the Asian-session range (00:00-07:00 UTC) during 07:00-10:00 UTC.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .base import Strategy, StrategyParams


def _two_sided(f: pd.DataFrame, long_ok: pd.Series, short_ok: pd.Series, stop_dist: pd.Series,
               target_r: float, max_hold: int) -> pd.DataFrame:
    close = f["close"]
    short = short_ok & ~long_ok
    sgn = np.where(short, -1.0, 1.0)
    stop = close - sgn * stop_dist
    return pd.DataFrame({
        "entry_ok": (long_ok | short_ok) & stop_dist.gt(0),
        "short": short,
        "stop": stop,
        "target": close + sgn * target_r * stop_dist,
        "max_hold": max_hold,
        "invalidation_level": np.nan,
    }, index=f.index)


class _ForexStrategy(Strategy):
    judged_raw = True
    exit_on_invalidation = False
    require_stable_halves = True

    def invalidation_rule(self) -> str:
        return "none: exits by stop, target or time only (ADR 0007)"


# ------------------------------------------------------------------ FX1
@dataclass(frozen=True)
class FxTrendParams(StrategyParams):
    stop_atr: float = 2.0
    target_r: float = 3.0
    max_hold: int = 120          # 1h bars = 5 trading days
    cooldown_bars: int = 10


class FxTrend(_ForexStrategy):
    code = "fx_trend"
    version_tag = "1"
    required = ("donchian_high", "donchian_low", "ema_slow", "sma_long", "atr")

    @classmethod
    def default_params(cls) -> FxTrendParams:
        return FxTrendParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: FxTrendParams = self.params  # type: ignore[assignment]
        close = f["close"]
        up = (f["ema_slow"] > f["sma_long"]) & (close > f["sma_long"])
        down = (f["ema_slow"] < f["sma_long"]) & (close < f["sma_long"])
        long_ok = up & (close > f["donchian_high"])
        short_ok = down & (close < f["donchian_low"])
        return _two_sided(f, long_ok, short_ok, p.stop_atr * f["atr"], p.target_r, p.max_hold)


# ------------------------------------------------------------------ FX2
@dataclass(frozen=True)
class FxMeanRevParams(StrategyParams):
    adx_max: float = 20.0
    rsi_low: float = 25.0
    rsi_high: float = 75.0
    stop_atr: float = 1.5
    target_r: float = 1.5
    max_hold: int = 24           # 1h bars = one day
    cooldown_bars: int = 6


class FxMeanRev(_ForexStrategy):
    code = "fx_meanrev"
    version_tag = "1"
    required = ("adx", "rsi", "atr")

    @classmethod
    def default_params(cls) -> FxMeanRevParams:
        return FxMeanRevParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: FxMeanRevParams = self.params  # type: ignore[assignment]
        calm = f["adx"] < p.adx_max
        return _two_sided(f, calm & (f["rsi"] < p.rsi_low), calm & (f["rsi"] > p.rsi_high),
                          p.stop_atr * f["atr"], p.target_r, p.max_hold)


# ------------------------------------------------------------------ FX3
@dataclass(frozen=True)
class FxLondonParams(StrategyParams):
    range_start_hour: int = 0    # UTC
    range_end_hour: int = 7      # range uses bars opening 00:00-06:45
    entry_end_hour: int = 10     # entries on bars closing up to 10:00
    min_range_pct: float = 0.15  # skip dead (< 0.15%) and wild (> 0.80%) nights
    max_range_pct: float = 0.80
    target_r: float = 1.5
    max_hold: int = 32           # 15m bars = 8 hours
    cooldown_bars: int = 0       # one trade per pair per day is enforced in the plan


class FxLondon(_ForexStrategy):
    code = "fx_london"
    version_tag = "1"
    required = ("atr",)

    @classmethod
    def default_params(cls) -> FxLondonParams:
        return FxLondonParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: FxLondonParams = self.params  # type: ignore[assignment]
        idx = f.index
        hour = idx.hour
        day = idx.normalize()
        in_range = (hour >= p.range_start_hour) & (hour < p.range_end_hour)
        hi = f["high"].where(in_range).groupby(day).transform("max")
        lo = f["low"].where(in_range).groupby(day).transform("min")
        width_pct = 100 * (hi - lo) / f["close"]
        # decision at the bar's CLOSE: bars opening 07:00..09:45 close by 10:00
        window = (hour >= p.range_end_hour) & (hour < p.entry_end_hour)
        sane = width_pct.between(p.min_range_pct, p.max_range_pct)
        close = f["close"]
        long_hit = window & sane & (close > hi)
        short_hit = window & sane & (close < lo)
        first = (long_hit | short_hit) & ((long_hit | short_hit).astype(int).groupby(day).cumsum() == 1)
        long_ok, short_ok = first & long_hit, first & short_hit
        stop_dist = pd.Series(np.where(long_ok, close - lo, hi - close), index=idx)
        return _two_sided(f, long_ok, short_ok, stop_dist, p.target_r, p.max_hold)


# ================================================================== batch F5 (ADR 0008)
# Pre-registered 2026-11-12 after FX1-FX3 failed, BEFORE any test of these three.

# ------------------------------------------------------------------ FX4
@dataclass(frozen=True)
class FxSweepParams(StrategyParams):
    buffer_atr: float = 0.1      # stop this far beyond the sweep bar's extreme
    target_r: float = 2.0
    max_hold: int = 24           # 1h bars = one day
    cooldown_bars: int = 6


class FxSweep(_ForexStrategy):
    """FX4, 1h: 'liquidity sweep' (stop hunt) reversal, as taught in Smart Money Concepts.

    A bar trades ABOVE the previous UTC day's high but CLOSES back below it: short, stop just
    above that bar's high. Mirror for longs at the previous day's low."""

    code = "fx_sweep"
    version_tag = "1"
    required = ("atr",)

    @classmethod
    def default_params(cls) -> FxSweepParams:
        return FxSweepParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: FxSweepParams = self.params  # type: ignore[assignment]
        day = f.index.normalize()
        daily = f.groupby(day).agg(day_high=("high", "max"), day_low=("low", "min"))
        prev = daily.shift(1).reindex(day)
        prev_high = pd.Series(prev["day_high"].to_numpy(), index=f.index)
        prev_low = pd.Series(prev["day_low"].to_numpy(), index=f.index)
        close, buf = f["close"], p.buffer_atr * f["atr"]
        short_ok = (f["high"] > prev_high) & (close < prev_high)
        long_ok = (f["low"] < prev_low) & (close > prev_low)
        stop_dist = pd.Series(np.where(short_ok, f["high"] + buf - close, close - (f["low"] - buf)), index=f.index)
        return _two_sided(f, long_ok & ~short_ok, short_ok & ~long_ok, stop_dist, p.target_r, p.max_hold)


# ------------------------------------------------------------------ FX5
@dataclass(frozen=True)
class FxTsmomParams(StrategyParams):
    lookback: int = 126          # daily bars (Exness has short Sunday bars: about 5-6 months)
    stop_atr: float = 3.0
    target_r: float = 10.0       # out of reach: the exit is the monthly re-decision (time)
    max_hold: int = 21           # daily bars, ends before the next month's decision
    cooldown_bars: int = 0


class FxTsmom(_ForexStrategy):
    """FX5, 1d: time-series momentum. On the first daily bar of each month, each pair is
    bought if it rose over the lookback and sold short if it fell. Wide catastrophe stop."""

    code = "fx_tsmom"
    version_tag = "1"
    required = ("atr",)

    @classmethod
    def default_params(cls) -> FxTsmomParams:
        return FxTsmomParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: FxTsmomParams = self.params  # type: ignore[assignment]
        close = f["close"]
        mom = close / close.shift(p.lookback) - 1
        month = pd.Series(f.index.month, index=f.index)
        first = month != month.shift(1)
        return _two_sided(f, first & (mom > 0), first & (mom < 0), p.stop_atr * f["atr"], p.target_r, p.max_hold)


# ------------------------------------------------------------------ FX6
@dataclass(frozen=True)
class FxXsmomParams(StrategyParams):
    lookback: int = 63           # daily bars, about 2.5-3 months
    n_each_side: int = 2
    stop_atr: float = 3.0
    target_r: float = 10.0
    max_hold: int = 21
    cooldown_bars: int = 0


class FxXsmom(_ForexStrategy):
    """FX6, 1d: cross-sectional (relative) momentum. On the first daily bar of each month,
    rank the pairs by lookback return: buy the strongest 2, sell short the weakest 2."""

    code = "fx_xsmom"
    version_tag = "1"
    required = ("atr", "xs_mom", "xs_rank", "xs_count")

    @classmethod
    def default_params(cls) -> FxXsmomParams:
        return FxXsmomParams()

    def prepare_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        from .rotation import add_cross_sectional_momentum
        out = add_cross_sectional_momentum(frames, self.params.lookback)
        count = pd.DataFrame({s: f["xs_mom"] for s, f in out.items()}).notna().sum(axis=1)
        return {s: f.assign(xs_count=count.reindex(f.index)) for s, f in out.items()}

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: FxXsmomParams = self.params  # type: ignore[assignment]
        if "xs_rank" not in f:
            raise KeyError("fx_xsmom needs prepare_frames() first (cross-sectional rank)")
        month = pd.Series(f.index.month, index=f.index)
        first = month != month.shift(1)
        enough = f["xs_count"] >= 2 * p.n_each_side
        long_ok = first & enough & (f["xs_rank"] <= p.n_each_side)
        short_ok = first & enough & (f["xs_rank"] > f["xs_count"] - p.n_each_side)
        return _two_sided(f, long_ok, short_ok, p.stop_atr * f["atr"], p.target_r, p.max_hold)
