"""Pre-registered strategy T2: monthly momentum rotation (ADR 0006). Parameters fixed BEFORE testing.

On the first daily bar of each calendar month, buy the `top_n` coins with the highest
30-day return among the universe, but only those whose 30-day return is positive. Exit
after `max_hold` bars (just before the next month), then re-select: one round trip per
coin per month, which overstates costs when a coin stays on top (conservative).
Catastrophic stop 3 ATR below entry; target out of reach (10 R).

Needs a cross-sectional rank, so frames are prepared together (`prepare_frames`): the rank
at day t uses only closes up to day t of every coin (no look-ahead).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .base import Strategy, StrategyParams


@dataclass(frozen=True)
class RotationParams(StrategyParams):
    lookback: int = 30
    top_n: int = 2
    stop_atr: float = 3.0
    target_r: float = 10.0
    max_hold: int = 25
    cooldown_bars: int = 0


def add_cross_sectional_momentum(frames: dict[str, pd.DataFrame], lookback: int) -> dict[str, pd.DataFrame]:
    mom = pd.DataFrame({s: f["close"] / f["close"].shift(lookback) - 1 for s, f in frames.items()})
    rank = mom.rank(axis=1, ascending=False, method="first")
    return {s: f.assign(xs_mom=mom[s].reindex(f.index), xs_rank=rank[s].reindex(f.index))
            for s, f in frames.items()}


class MomentumRotation(Strategy):
    code = "rotation"
    version_tag = "1"
    required = ("atr", "xs_mom", "xs_rank")
    judged_raw = True
    exit_on_invalidation = False

    @classmethod
    def default_params(cls) -> RotationParams:
        return RotationParams()

    def prepare_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        return add_cross_sectional_momentum(frames, self.params.lookback)

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: RotationParams = self.params  # type: ignore[assignment]
        if "xs_rank" not in f:
            raise KeyError("rotation needs prepare_frames() first (cross-sectional rank)")
        close, atr = f["close"], f["atr"]
        month = pd.Series(f.index.month, index=f.index)
        first_bar_of_month = month != month.shift(1)
        stop = close - p.stop_atr * atr
        return pd.DataFrame({
            "entry_ok": first_bar_of_month & (f["xs_rank"] <= p.top_n) & (f["xs_mom"] > 0),
            "stop": stop,
            "target": close + p.target_r * (close - stop),
            "max_hold": p.max_hold,
            "invalidation_level": float("nan"),
        }, index=f.index)

    def invalidation_rule(self) -> str:
        return "none (time exit before next month)"
