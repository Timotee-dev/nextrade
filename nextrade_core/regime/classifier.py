"""Rule-based market regime classifier, version 1 (spec 4.3).

Transparent on purpose: each label comes from a decision table on features that
are already validated. It replaces nothing until it earns its place: Phase 7 checks
whether gating strategies by regime improves out-of-sample results. No confidence
number is produced, because a rule table has no honest probability to report.

Priority order matters (first match wins):
  high volatility > breakout > strong trend > weak trend > low volatility > range > uncertain
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd


class Regime(StrEnum):
    STRONG_BULL = "strong_bull"
    WEAK_BULL = "weak_bull"
    STRONG_BEAR = "strong_bear"
    WEAK_BEAR = "weak_bear"
    RANGE = "range"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    BREAKOUT = "breakout"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class RegimeRules:
    version: str = "rules-1"
    high_vol_pct: float = 0.90
    low_vol_pct: float = 0.10
    breakout_rel_volume: float = 1.5
    strong_adx: float = 30.0
    weak_adx: float = 20.0
    range_bb_width_pct: float = 0.50


def classify(features: pd.DataFrame, rules: RegimeRules | None = None) -> pd.Series:
    r = rules or RegimeRules()
    f = features
    up = f["ema_spread_atr"] > 0
    down = f["ema_spread_atr"] < 0
    breakout = ((f["breakout_up"] == 1) | (f["breakout_down"] == 1)) & (f["rel_volume"] >= r.breakout_rel_volume)
    conditions = [
        f["atr_pct_percentile"] >= r.high_vol_pct,
        breakout,
        (f["adx"] >= r.strong_adx) & up,
        (f["adx"] >= r.strong_adx) & down,
        (f["adx"] >= r.weak_adx) & up,
        (f["adx"] >= r.weak_adx) & down,
        f["atr_pct_percentile"] <= r.low_vol_pct,
        (f["adx"] < r.weak_adx) & (f["bb_width_percentile"] <= r.range_bb_width_pct),
    ]
    labels = [Regime.HIGH_VOLATILITY, Regime.BREAKOUT, Regime.STRONG_BULL, Regime.STRONG_BEAR,
              Regime.WEAK_BULL, Regime.WEAK_BEAR, Regime.LOW_VOLATILITY, Regime.RANGE]
    needed = ["atr_pct_percentile", "adx", "ema_spread_atr", "bb_width_percentile", "rel_volume",
              "breakout_up", "breakout_down"]
    out = pd.Series(np.select(conditions, [l.value for l in labels], Regime.UNCERTAIN.value),
                    index=f.index, dtype=object)
    out[f[needed].isna().any(axis=1)] = None
    return out


def transitions(regimes: pd.Series) -> pd.DataFrame:
    """Counts of regime -> next regime, to study how regimes hand over to each other."""
    s = regimes.dropna()
    pairs = pd.DataFrame({"from": s.to_numpy()[:-1], "to": s.to_numpy()[1:]})
    return pd.crosstab(pairs["from"], pairs["to"])
