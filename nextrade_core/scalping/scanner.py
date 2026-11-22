"""Opportunity scanner (research mode): what would the strategies do on the latest bar, and why not.

For every symbol and strategy it takes the newest CLOSED bar's signal (if any), then applies the
same gates real trading uses: data freshness, regime, cost viability (net R after costs), capital
feasibility, venue shorting. It returns accepted AND rejected candidates with reasons, ranked by
expected net R. It places no orders. Waiting is a valid outcome: an empty accepted list is normal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.domain.instrument import Instrument
from nextrade_core.regime.classifier import Regime, classify
from nextrade_core.strategies.base import Signal

from .capital import check_instrument

HOSTILE = {Regime.HIGH_VOLATILITY.value, Regime.UNCERTAIN.value, "unknown"}


@dataclass(frozen=True)
class Candidate:
    signal: Signal
    regime: str
    expected_net_r: float
    cost_r: float
    accepted: bool
    reasons: tuple[str, ...] = ()


@dataclass
class ScanResult:
    at: datetime
    candidates: list[Candidate] = field(default_factory=list)
    unavailable: dict[str, list[str]] = field(default_factory=dict)    # symbol -> reasons
    stale: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> list[Candidate]:
        return sorted((c for c in self.candidates if c.accepted), key=lambda c: -c.expected_net_r)

    @property
    def rejected(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.accepted]


def scan(frames: dict[str, pd.DataFrame], instruments: dict[str, Instrument], strategies: dict, *,
         timeframe: Timeframe, costs: CostModel, now: datetime, capital: Decimal, risk_fraction: Decimal,
         can_short: bool = False, max_age_bars: int = 3, min_net_r: float = 0.3,
         max_leverage: Decimal = Decimal(1)) -> ScanResult:
    res = ScanResult(at=now)
    for sym, f in frames.items():
        if f.empty or now - f.index[-1].to_pydatetime() > timeframe.delta * (max_age_bars + 1):
            res.stale.append(sym)                       # stale or missing data: never scan it
            continue
        label = classify(f).iloc[-1] if "adx" in f.columns else None
        regime = label if isinstance(label, str) else "unknown"       # warm-up: no entries
        for code, strat in strategies.items():
            last_bar = f.index[-1]
            sigs = [s for s in strat.generate(f.tail(600), sym, timeframe) if pd.Timestamp(s.bar_time) == last_bar]
            for s in sigs:
                cost_r = costs.cost_in_r(sym, s.entry, s.stop)
                net_r = s.reward_risk - cost_r
                why: list[str] = []
                if regime in HOSTILE:
                    why.append(f"regime {regime}: no new entries")
                if net_r < min_net_r:
                    why.append(f"expected net {net_r:+.2f}R after costs is below {min_net_r:.2f}R")
                inst = instruments.get(sym)
                if inst is None:
                    why.append("instrument specification missing")
                else:
                    feas = check_instrument(inst, capital=capital, price=Decimal(str(s.entry)),
                                            stop_pct=abs(s.entry - s.stop) / s.entry,
                                            target_pct=abs(s.target - s.entry) / s.entry,
                                            risk_fraction=risk_fraction, costs=costs, max_leverage=max_leverage,
                                            needs_short=s.sign < 0, can_short=can_short)
                    why.extend(feas.reasons)
                    if not feas.available:
                        res.unavailable[sym] = feas.reasons
                res.candidates.append(Candidate(s, regime, net_r, cost_r, not why, tuple(why)))
    return res

