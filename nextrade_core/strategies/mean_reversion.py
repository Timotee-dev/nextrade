"""Strategy C: mean reversion after a statistically unusual drop in a ranging market."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .base import Strategy, StrategyParams


@dataclass(frozen=True)
class MeanReversionParams(StrategyParams):
    lookback: int = 20
    z_entry: float = 2.0
    adx_max: float = 20.0          # only when there is no strong trend
    rsi_max: float = 30.0
    stop_atr: float = 1.5
    min_reward_risk: float = 1.0   # skip when the mean is too close to be worth the risk
    max_hold: int = 24
    cooldown_bars: int = 6


class MeanReversion(Strategy):
    code = "meanrev"
    version_tag = "1"
    required = ("atr", "adx", "rsi")

    @classmethod
    def default_params(cls) -> MeanReversionParams:
        return MeanReversionParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: MeanReversionParams = self.params  # type: ignore[assignment]
        close, atr = f["close"], f["atr"]
        mean = close.rolling(p.lookback, min_periods=p.lookback).mean()
        sd = close.rolling(p.lookback, min_periods=p.lookback).std(ddof=0)
        z = (close - mean) / sd
        stop = close - p.stop_atr * atr
        worth_it = (mean - close) >= p.min_reward_risk * (close - stop)
        return pd.DataFrame({
            "entry_ok": (z <= -p.z_entry) & (f["adx"] <= p.adx_max) & (f["rsi"] <= p.rsi_max) & worth_it,
            "stop": stop,
            "target": mean,
            "max_hold": p.max_hold,
            "invalidation_level": stop,
        }, index=f.index)

    def invalidation_rule(self) -> str:
        return "price reaches the stop (no separate invalidation)"
