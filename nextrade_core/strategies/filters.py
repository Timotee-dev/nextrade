"""Spec strategies B, E, F and G, used as filters on A, C and D (see the architecture spec).

They do not define complete trades on their own (no stop, target or exit), so
each one only narrows when another strategy may signal.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class MomentumFilter:            # B
    min_roc: float = 0.0

    def mask(self, bars: pd.DataFrame, f: pd.DataFrame) -> pd.Series:
        return f["roc"] > self.min_roc

    def describe(self) -> str:
        return f"momentum(roc>{self.min_roc})"


@dataclass(frozen=True)
class VolatilityBand:            # E
    low: float = 0.1
    high: float = 0.9

    def mask(self, bars: pd.DataFrame, f: pd.DataFrame) -> pd.Series:
        return f["atr_pct_percentile"].between(self.low, self.high)

    def describe(self) -> str:
        return f"vol_band({self.low}-{self.high})"


@dataclass(frozen=True)
class HigherTimeframeTrend:      # F
    column: str = "4h_ema_spread_atr"   # produced by align_higher_timeframe on fs-1 features

    def mask(self, bars: pd.DataFrame, f: pd.DataFrame) -> pd.Series:
        if self.column not in f:
            raise KeyError(f"{self.column} missing: align the higher timeframe first")
        return f[self.column] > 0

    def describe(self) -> str:
        return f"htf_trend({self.column}>0)"


@dataclass(frozen=True)
class VolumeConfirmation:        # G
    min_rel_volume: float = 1.2

    def mask(self, bars: pd.DataFrame, f: pd.DataFrame) -> pd.Series:
        return f["rel_volume"] >= self.min_rel_volume

    def describe(self) -> str:
        return f"volume(rel>={self.min_rel_volume})"
