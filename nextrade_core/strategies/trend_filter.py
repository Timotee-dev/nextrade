"""Pre-registered strategy T1: 200-day trend filter (ADR 0006). Parameters fixed BEFORE testing.

Buy when the daily close crosses above its 200-day simple average; sell on the first daily
close back below it. A catastrophic stop sits 3 ATR below entry; the target is set out of
reach (10 R) because the exit is the rule, not a price. Holds up to 365 bars.

Its outcome is continuous (ride the trend, exit on the rule), so NAIA's "probability of
target before stop" does not describe it: it is judged on its raw trades (`judged_raw`).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .base import Signal, Strategy, StrategyParams


@dataclass(frozen=True)
class TrendFilterParams(StrategyParams):
    stop_atr: float = 3.0
    target_r: float = 10.0
    max_hold: int = 365
    cooldown_bars: int = 0


class TrendFilter(Strategy):
    code = "trendfilter"
    version_tag = "1"
    required = ("sma_long", "atr")      # sma_long = 200 bars (FeatureParams.sma_long)
    judged_raw = True
    exit_on_invalidation = True

    @classmethod
    def default_params(cls) -> TrendFilterParams:
        return TrendFilterParams()

    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        p: TrendFilterParams = self.params  # type: ignore[assignment]
        close, atr = f["close"], f["atr"]
        above = close > f["sma_long"]
        stop = close - p.stop_atr * atr
        return pd.DataFrame({
            "entry_ok": above & ~above.shift(1, fill_value=True),
            "stop": stop,
            "target": close + p.target_r * (close - stop),
            "max_hold": p.max_hold,
            "invalidation_level": f["sma_long"],
        }, index=f.index)

    def invalidation_rule(self) -> str:
        return "daily close below the 200-day average"

    def is_invalidated(self, signal: Signal, bar: pd.Series) -> bool:
        sma = bar.get("sma_long")
        if sma is None or pd.isna(sma):
            return super().is_invalidated(signal, bar)
        return float(bar["close"]) < float(sma)
