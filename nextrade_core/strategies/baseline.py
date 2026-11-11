"""Random-entry baseline (spec 11: compare each strategy against simple baselines).

Enters on pseudo-random bars with a fixed probability. `RandomEntry.matching(s, p)`
copies strategy s's stops, targets, holding time and cooldown, so the only
difference from s is WHEN it enters. A strategy that cannot beat this has no
entry edge.

The random draw for a bar depends only on (seed, symbol, bar time), not on how
much history is in the frame, so live replay and backtest pick the same bars.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from nextrade_core.data.candles import Timeframe

from .base import Signal, Strategy, StrategyParams

_GOLDEN = np.uint64(0x9E3779B97F4A7C15)


def bar_uniforms(index: pd.DatetimeIndex, seed: int, salt: str = "") -> np.ndarray:
    """Deterministic U(0,1) per timestamp (splitmix64 hash)."""
    x = index.asi8.astype(np.uint64)
    x ^= np.uint64(seed) * _GOLDEN
    x ^= np.uint64(zlib.crc32(salt.encode()))
    with np.errstate(over="ignore"):
        x = x + _GOLDEN
        z = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        z = z ^ (z >> np.uint64(31))
    return (z >> np.uint64(11)).astype(np.float64) / float(2**53)


@dataclass(frozen=True)
class RandomEntryParams(StrategyParams):
    probability: float = 0.05
    stop_atr: float = 2.0
    reward_risk: float = 2.0
    max_hold: int = 24
    seed: int = 0
    cooldown_bars: int = 0


class RandomEntry(Strategy):
    code = "random"
    version_tag = "1"
    required = ("atr",)

    def __init__(self, params: RandomEntryParams | None = None, template: Strategy | None = None) -> None:
        super().__init__(params)
        self.template = template
        self._salt = ""

    @classmethod
    def default_params(cls) -> RandomEntryParams:
        return RandomEntryParams()

    @classmethod
    def matching(cls, strategy: Strategy, probability: float, seed: int = 0) -> RandomEntry:
        cooldown = int(getattr(strategy.params, "cooldown_bars", 0))
        return cls(RandomEntryParams(probability=probability, seed=seed, cooldown_bars=cooldown), template=strategy)

    @property
    def version(self) -> str:
        base = super().version
        return f"{base}-{self.template.code}" if self.template else base

    def invalidation_rule(self) -> str:
        return self.template.invalidation_rule() if self.template else "none (random baseline)"

    def is_invalidated(self, signal, bar) -> bool:
        return self.template.is_invalidated(signal, bar) if self.template else super().is_invalidated(signal, bar)

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: RandomEntryParams = self.params  # type: ignore[assignment]
        draw = pd.Series(bar_uniforms(f.index, p.seed, self._salt) < p.probability, index=f.index)
        if self.template is not None:
            plan = self.template._plan(f).copy()
            plan["entry_ok"] = draw
            return plan
        close, atr = f["close"], f["atr"]
        stop = close - p.stop_atr * atr
        return pd.DataFrame({"entry_ok": draw, "stop": stop, "target": close + p.reward_risk * (close - stop),
                             "max_hold": p.max_hold, "invalidation_level": np.nan}, index=f.index)

    def generate(self, f: pd.DataFrame, symbol: str, timeframe: Timeframe) -> list[Signal]:
        self._salt = symbol  # independent draws per symbol
        try:
            return super().generate(f, symbol, timeframe)
        finally:
            self._salt = ""
