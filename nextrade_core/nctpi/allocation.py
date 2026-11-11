"""NCTPI allocation (spec 5): spend a small risk budget on the best opportunities.

Each cycle (bar close): candidates are ranked by NAIA score, then NARA checks them
one at a time against a snapshot that already includes the trades approved earlier
in the same cycle. First come, first served is never used: a weaker early signal
cannot crowd out a stronger one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from nextrade_core.domain.instrument import Instrument
from nextrade_core.nara.engine import RiskDecision, RiskEngine, TradeProposal
from nextrade_core.nara.portfolio import OpenPosition, PortfolioSnapshot


@dataclass(frozen=True)
class Candidate:
    key: str
    symbol: str
    score: float
    proposal: TradeProposal


@dataclass
class AllocationResult:
    approved: list[tuple[Candidate, RiskDecision]] = field(default_factory=list)
    rejected: list[tuple[Candidate, RiskDecision | None, str]] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)


def allocate(candidates: list[Candidate], snapshot: PortfolioSnapshot, risk: RiskEngine,
             instruments: dict[str, Instrument], *, entries_allowed: bool, data_fresh: dict[str, bool] | None = None,
             held_symbols: set[str] | None = None) -> AllocationResult:
    out = AllocationResult()
    held = set(held_symbols or {p.symbol for p in snapshot.positions})
    snap = snapshot
    for c in sorted(candidates, key=lambda c: c.score, reverse=True):
        if c.symbol in held:
            out.rejected.append((c, None, "already_in_position"))
            out.reasons["already_in_position"] += 1
            continue
        decision = risk.check(c.proposal, snap, instruments[c.symbol], entries_allowed=entries_allowed,
                              data_fresh=(data_fresh or {}).get(c.symbol, True))
        if not decision.approved:
            out.rejected.append((c, decision, ",".join(decision.failed)))
            for rule in decision.failed:
                out.reasons[f"nara:{rule}"] += 1
            continue
        out.approved.append((c, decision))
        held.add(c.symbol)
        spent = decision.notional * (1 + c.proposal.cost_fraction)
        snap = replace(snap, free_cash=snap.free_cash - spent,
                       positions=snap.positions + (OpenPosition(c.symbol, decision.qty, c.proposal.entry,
                                                                c.proposal.stop, c.proposal.entry,
                                                                c.proposal.asset_class, c.proposal.beta),))
    return out


def capacity(*, free_cash: Decimal, min_notional: Decimal, risk_budget: Decimal, min_risk_per_trade: Decimal,
             rate_capacity: int, operator_cap: int) -> dict:
    """How many positions can be opened now, and which constraint binds (shown on the dashboard)."""
    terms = {
        "cash": int(free_cash // min_notional) if min_notional > 0 else operator_cap,
        "risk_budget": int(risk_budget // min_risk_per_trade) if min_risk_per_trade > 0 else operator_cap,
        "rate_limit": rate_capacity,
        "operator_cap": operator_cap,
    }
    binding = min(terms, key=terms.get)
    return {"max_positions": max(0, terms[binding]), "binding": binding, "terms": terms}


@dataclass
class OpportunityQueue:
    """Holds scored opportunities until the next allocation; they expire after one bar."""
    items: list[tuple[datetime, Candidate]] = field(default_factory=list)
    expired: int = 0

    def push(self, candidate: Candidate, expires_at: datetime) -> None:
        self.items.append((expires_at, candidate))

    def drain(self, now: datetime) -> list[Candidate]:
        live = [c for exp, c in self.items if exp > now]
        self.expired += len(self.items) - len(live)
        self.items = []
        return live


class Funnel(Counter):
    """Discovered -> filtered -> scored -> approved -> submitted -> filled -> closed (spec 9)."""
    STAGES = ("signals", "rejected_by_naia", "rejected_by_nara", "approved", "orders_submitted",
              "orders_filled", "orders_partially_filled", "orders_rejected", "positions_closed",
              "net_winners", "net_losers")

    def as_table(self) -> dict:
        return {stage: int(self.get(stage, 0)) for stage in self.STAGES}
