"""Scalping strategies A-F (ADR 0012). Parameters are fixed BEFORE any test and are not tuned.

Every strategy: trades from several independent evidence families (never one indicator),
reads the 1h trend context (htf_*, closed higher bars only), exits only by stop, target or
time, and is judged on its raw trades against a random-entry baseline with identical exits.
A strategy may trade ONLY after it passes the decision gate and the operator approves it
(scalping/approvals.py). Until then it is research.

A momentum_scalp   directional momentum + volume + trend strength, with the 1h trend.
B trend_scalp      pullback to VWAP inside an established trend, resuming with it.
C meanrev_scalp    Bollinger/RSI extreme in a quiet, trendless market.
D breakout_scalp   squeeze (narrow bands) then a confirmed, high-volume breakout.
E volatility_scalp volatility expansion out of quiet, in the direction of the move.
F short_scalp      bearish structure break with the 1h downtrend (needs a venue that allows shorts).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .base import Side, Strategy, StrategyParams
from .forex import _two_sided


@dataclass(frozen=True)
class ScalpParams(StrategyParams):
    stop_atr: float = 1.2
    target_r: float = 1.5
    max_hold: int = 24
    cooldown_bars: int = 6


class _Scalp(Strategy):
    judged_raw = True
    exit_on_invalidation = False
    require_stable_halves = True
    needs_shorting = False
    fast = True

    def invalidation_rule(self) -> str:
        return "none: exits by stop, target or time only (ADR 0012)"

    @classmethod
    def default_params(cls) -> ScalpParams:
        return ScalpParams()

    def _build(self, f: pd.DataFrame, long_ok: pd.Series, short_ok: pd.Series) -> pd.DataFrame:
        p: ScalpParams = self.params  # type: ignore[assignment]
        return _two_sided(f, long_ok.fillna(False), short_ok.fillna(False), p.stop_atr * f["atr"],
                          p.target_r, p.max_hold)


class MomentumScalp(_Scalp):
    code = "scalp_momentum"
    version_tag = "1"
    required = ("atr", "close_vs_ema_slow_atr", "rel_volume", "adx")

    def _plan(self, f):
        strong = (f["rel_volume"] >= 1.3) & (f["adx"] >= 22)
        up = (f["close_vs_ema_slow_atr"] >= 1.0) & (f["htf_trend"] > 0)
        down = (f["close_vs_ema_slow_atr"] <= -1.0) & (f["htf_trend"] < 0)
        return self._build(f, strong & up, strong & down)


class TrendScalp(_Scalp):
    code = "scalp_trend"
    version_tag = "1"
    required = ("atr", "ema_spread_atr", "di_diff", "vwap_dist_atr", "rsi")

    def _plan(self, f):
        pull_up = f["vwap_dist_atr"].between(-0.5, 0.3) & f["rsi"].between(40, 55)
        pull_dn = f["vwap_dist_atr"].between(-0.3, 0.5) & f["rsi"].between(45, 60)
        long_ok = (f["htf_trend"] > 0) & (f["ema_spread_atr"] > 0.5) & (f["di_diff"] > 0) & pull_up
        short_ok = (f["htf_trend"] < 0) & (f["ema_spread_atr"] < -0.5) & (f["di_diff"] < 0) & pull_dn
        return self._build(f, long_ok, short_ok)


@dataclass(frozen=True)
class MeanRevScalpParams(ScalpParams):
    stop_atr: float = 1.5
    target_r: float = 1.0
    max_hold: int = 12


class MeanRevScalp(_Scalp):
    code = "scalp_meanrev"
    version_tag = "1"
    required = ("atr", "adx", "rsi", "bb_pct_b")

    @classmethod
    def default_params(cls) -> MeanRevScalpParams:
        return MeanRevScalpParams()

    def _plan(self, f):
        quiet = (f["adx"] < 20) & (f["htf_adx"].fillna(0) < 25)
        return self._build(f, quiet & (f["bb_pct_b"] < 0.02) & (f["rsi"] < 25),
                           quiet & (f["bb_pct_b"] > 0.98) & (f["rsi"] > 75))


@dataclass(frozen=True)
class BreakoutScalpParams(ScalpParams):
    stop_atr: float = 1.0
    target_r: float = 2.0


class BreakoutScalp(_Scalp):
    code = "scalp_breakout"
    version_tag = "1"
    required = ("atr", "bb_width_percentile", "breakout_up", "breakout_down", "rel_volume")

    @classmethod
    def default_params(cls) -> BreakoutScalpParams:
        return BreakoutScalpParams()

    def _plan(self, f):
        squeeze = f["bb_width_percentile"].shift(1) <= 0.20
        rng = (f["high"] - f["low"]).replace(0, np.nan)
        pos = (f["close"] - f["low"]) / rng                  # where the bar closed inside its range
        vol = f["rel_volume"] >= 1.5
        long_ok = squeeze & (f["breakout_up"] == 1) & vol & (pos >= 0.75) & (f["htf_trend"] >= 0)
        short_ok = squeeze & (f["breakout_down"] == 1) & vol & (pos <= 0.25) & (f["htf_trend"] <= 0)
        return self._build(f, long_ok, short_ok)


@dataclass(frozen=True)
class VolScalpParams(ScalpParams):
    stop_atr: float = 1.5
    target_r: float = 2.0
    max_hold: int = 16


class VolatilityScalp(_Scalp):
    code = "scalp_vol"
    version_tag = "1"
    required = ("atr", "atr_pct_percentile")

    @classmethod
    def default_params(cls) -> VolScalpParams:
        return VolScalpParams()

    def _plan(self, f):
        pct = f["atr_pct_percentile"]
        expanding = (pct.rolling(6).min().shift(1) <= 0.25) & (pct >= 0.50)
        move = f["close"] - f["close"].shift(3)
        return self._build(f, expanding & (move > f["atr"]) & (f["htf_trend"] >= 0),
                           expanding & (move < -f["atr"]) & (f["htf_trend"] <= 0))


class ShortScalp(_Scalp):
    code = "scalp_short"
    version_tag = "1"
    side = Side.SHORT
    needs_shorting = True
    required = ("atr", "adx", "rel_volume", "break_of_structure_down")

    def _plan(self, f):
        short_ok = ((f["htf_trend"] < 0) & (f["break_of_structure_down"] == 1)
                    & (f["rel_volume"] >= 1.2) & (f["adx"] >= 22))
        return self._build(f, pd.Series(False, index=f.index), short_ok)


SCALP_CLASSES = (MomentumScalp, TrendScalp, MeanRevScalp, BreakoutScalp, VolatilityScalp, ShortScalp)
SCALP_TIMEFRAMES = {"scalp_momentum": "5m", "scalp_trend": "5m", "scalp_meanrev": "5m",
                    "scalp_breakout": "5m", "scalp_vol": "15m", "scalp_short": "5m"}
