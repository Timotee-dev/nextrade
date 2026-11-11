"""Strategy A: trend following, entering on a pullback to the fast EMA."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .base import Strategy, StrategyParams


@dataclass(frozen=True)
class TrendParams(StrategyParams):
    adx_min: float = 20.0
    slope_min_atr: float = 0.0     # fast EMA must be rising
    pullback_atr: float = 0.25     # bar's low within this many ATRs of the fast EMA
    stop_atr: float = 2.0
    reward_risk: float = 2.0
    max_hold: int = 48
    cooldown_bars: int = 6


class TrendFollowing(Strategy):
    code = "trend"
    version_tag = "1"
    required = ("ema_fast", "ema_slow", "atr", "adx", "ema_fast_slope_atr")

    @classmethod
    def default_params(cls) -> TrendParams:
        return TrendParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: TrendParams = self.params  # type: ignore[assignment]
        close, atr = f["close"], f["atr"]
        uptrend = (f["ema_fast"] > f["ema_slow"]) & (f["adx"] >= p.adx_min) & (f["ema_fast_slope_atr"] > p.slope_min_atr)
        pullback = (f["low"] <= f["ema_fast"] + p.pullback_atr * atr) & (close > f["ema_fast"])
        stop = close - p.stop_atr * atr
        return pd.DataFrame({
            "entry_ok": uptrend & pullback,
            "stop": stop,
            "target": close + p.reward_risk * (close - stop),
            "max_hold": p.max_hold,
            "invalidation_level": f["ema_slow"],
        }, index=f.index)

    def invalidation_rule(self) -> str:
        return "close below the slow EMA at entry"
