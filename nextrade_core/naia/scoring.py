"""NAIA opportunity scoring (spec 4.7, 4.8): the default answer is NO TRADE.

Expected net R for a long plan with reward:risk b, probability p of hitting the
target first and round-trip cost c (all in R):

    E = p * b - (1 - p) * 1 - c

Score = E - k * uncertainty. A signal becomes an opportunity only when every filter
passes and the score clears the hurdle. Every rejection carries reason codes.
`p_source` states where p came from ("hit_rate" or "model:<version>"), so the
dashboard never presents a hit rate as a model confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from nextrade_core.strategies.base import Signal


class Reason(StrEnum):
    DATA_UNRELIABLE = "data_unreliable"
    MARKET_CLOSED = "market_closed"
    NEWS_BLACKOUT = "news_blackout"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LOW_LIQUIDITY = "low_liquidity"
    REGIME_INCOMPATIBLE = "regime_incompatible"
    COST_HURDLE = "cost_hurdle"
    HIGH_UNCERTAINTY = "high_uncertainty"
    LOW_EXPECTED_VALUE = "low_expected_value"


@dataclass(frozen=True)
class ScoringConfig:
    hurdle_r: float = 0.10
    uncertainty_k: float = 1.0
    max_uncertainty: float = 0.15
    max_spread_bps: float = 15.0
    min_quote_volume_24h: float = 1_000_000.0
    allowed_regimes: dict[str, frozenset[str]] = field(default_factory=dict)  # empty = no gating


@dataclass(frozen=True)
class MarketContext:
    data_ok: bool = True
    market_open: bool = True
    blackout: bool = False
    spread_bps: float | None = None
    quote_volume_24h: float | None = None
    regime: str | None = None


@dataclass(frozen=True)
class Opportunity:
    signal: Signal
    accepted: bool
    reasons: tuple[Reason, ...]
    p: float | None
    p_source: str
    uncertainty: float
    cost_r: float
    expected_net_r: float | None
    score: float | None
    explanation: dict

    @property
    def max_expected_r(self) -> float:
        """E if the target were certain: b - c. Below the hurdle, no p can save the trade."""
        return self.signal.reward_risk - self.cost_r


def expected_net_r(p: float, reward_risk: float, cost_r: float) -> float:
    return p * reward_risk - (1 - p) - cost_r


def score_signal(signal: Signal, *, p: float | None, p_source: str, uncertainty: float, cost_r: float,
                 context: MarketContext, config: ScoringConfig | None = None,
                 contributions: dict | None = None) -> Opportunity:
    cfg = config or ScoringConfig()
    reasons: list[Reason] = []
    if not context.data_ok:
        reasons.append(Reason.DATA_UNRELIABLE)
    if not context.market_open:
        reasons.append(Reason.MARKET_CLOSED)
    if context.blackout:
        reasons.append(Reason.NEWS_BLACKOUT)
    if context.spread_bps is not None and context.spread_bps > cfg.max_spread_bps:
        reasons.append(Reason.SPREAD_TOO_WIDE)
    if context.quote_volume_24h is not None and context.quote_volume_24h < cfg.min_quote_volume_24h:
        reasons.append(Reason.LOW_LIQUIDITY)
    allowed = cfg.allowed_regimes.get(signal.strategy)
    if allowed is not None and context.regime not in allowed:
        reasons.append(Reason.REGIME_INCOMPATIBLE)
    if signal.reward_risk - cost_r < cfg.hurdle_r:
        reasons.append(Reason.COST_HURDLE)

    e = score = None
    if p is not None:
        e = expected_net_r(p, signal.reward_risk, cost_r)
        score = e - cfg.uncertainty_k * uncertainty
        if uncertainty > cfg.max_uncertainty:
            reasons.append(Reason.HIGH_UNCERTAINTY)
        if score < cfg.hurdle_r and Reason.COST_HURDLE not in reasons:
            reasons.append(Reason.LOW_EXPECTED_VALUE)
    else:
        reasons.append(Reason.LOW_EXPECTED_VALUE)

    explanation = {
        "strategy": signal.strategy_version, "reward_risk": round(signal.reward_risk, 3),
        "cost_r": round(cost_r, 4), "p": None if p is None else round(p, 4), "p_source": p_source,
        "uncertainty": round(uncertainty, 4), "expected_net_r": None if e is None else round(e, 4),
        "regime": context.regime, "top_features": contributions or {},
        "rejected_for": [r.value for r in reasons],
    }
    return Opportunity(signal, not reasons, tuple(reasons), p, p_source, uncertainty, cost_r, e, score, explanation)
