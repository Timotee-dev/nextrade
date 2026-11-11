"""Strategy D: breakout above the prior N-bar high, confirmed by volume."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .base import Strategy, StrategyParams


@dataclass(frozen=True)
class BreakoutParams(StrategyParams):
    rel_volume_min: float = 1.5
    max_extension_atr: float = 1.0   # skip if the close already ran far past the level
    stop_buffer_atr: float = 0.5     # stop this far below the breakout level
    min_stop_atr: float = 1.0        # but never tighter than this from the close
    reward_risk: float = 2.0
    max_hold: int = 36
    cooldown_bars: int = 6


class Breakout(Strategy):
    code = "breakout"
    version_tag = "1"
    required = ("donchian_high", "atr", "rel_volume")

    @classmethod
    def default_params(cls) -> BreakoutParams:
        return BreakoutParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: BreakoutParams = self.params  # type: ignore[assignment]
        close, atr, level = f["close"], f["atr"], f["donchian_high"]
        broke = (close > level) & (close - level <= p.max_extension_atr * atr)
        stop = pd.concat([level - p.stop_buffer_atr * atr, close - p.min_stop_atr * atr], axis=1).min(axis=1)
        return pd.DataFrame({
            "entry_ok": broke & (f["rel_volume"] >= p.rel_volume_min),
            "stop": stop,
            "target": close + p.reward_risk * (close - stop),
            "max_hold": p.max_hold,
            "invalidation_level": level,
        }, index=f.index)

    def invalidation_rule(self) -> str:
        return "close back below the breakout level"
