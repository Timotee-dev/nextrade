"""NARA: final authority over every new order, and the source of automatic halts.

check() runs the pre-trade checks in a fixed order and records every rule's result.
Any failed rule rejects the trade; a size reduction to fit a limit is never done
silently here: the rule fails and says by how much.
halts() reports which halt the portfolio's state requires right now.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum

from nextrade_core.domain.instrument import Instrument

from .limits import RiskLimits
from .portfolio import OpenPosition, PortfolioSnapshot, StressScenario, open_risk, stress_loss
from .sizing import SizeResult, size_position


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class Halt(StrEnum):
    NONE = "none"
    PAUSE_DAY = "pause_until_next_utc_day"
    PAUSE_LOSSES = "pause_after_consecutive_losses"
    RISK_SHUTDOWN = "risk_shutdown"


@dataclass(frozen=True)
class TradeProposal:
    symbol: str
    entry: Decimal
    stop: Decimal
    cost_fraction: Decimal
    asset_class: str = "crypto"
    beta: Decimal = Decimal(1)
    spread_bps: Decimal | None = None
    sign: int = 1                       # +1 long, -1 short
    fx: Decimal = Decimal(1)            # account currency per unit of quote currency
    leverage: Decimal = Decimal(1)      # 1 = spot (full notional in cash)


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskDecision:
    decision: Decision
    qty: Decimal
    risk_amount: Decimal
    notional: Decimal
    rules: tuple[RuleResult, ...]
    limits_version: str

    @property
    def approved(self) -> bool:
        return self.decision is Decision.APPROVE

    @property
    def failed(self) -> list[str]:
        return [r.rule for r in self.rules if not r.passed]


@dataclass
class RiskEngine:
    limits: RiskLimits = field(default_factory=RiskLimits)
    stress: StressScenario = field(default_factory=StressScenario)

    def check(self, proposal: TradeProposal, snap: PortfolioSnapshot, instrument: Instrument, *,
              entries_allowed: bool, data_fresh: bool, market_open: bool = True) -> RiskDecision:
        L = self.limits
        rules: list[RuleResult] = []

        def rule(name: str, ok: bool, detail: str = "") -> bool:
            rules.append(RuleResult(name, bool(ok), detail))
            return bool(ok)

        rule("system_allows_entries", entries_allowed)
        rule("data_fresh", data_fresh)
        rule("instrument_tradable", instrument.tradable and market_open)
        if proposal.spread_bps is not None:
            rule("spread_within_limit", proposal.spread_bps <= L.max_spread_bps,
                 f"{proposal.spread_bps} bps vs {L.max_spread_bps}")

        size: SizeResult = size_position(
            equity=snap.equity, risk_fraction=L.risk_per_trade, entry=proposal.entry, stop=proposal.stop,
            instrument=instrument, cost_fraction=proposal.cost_fraction, free_cash=snap.free_cash,
            fx=proposal.fx, sign=proposal.sign, leverage=proposal.leverage)
        rule("size_valid", size.ok, size.reject_reason or f"qty {size.qty}")
        budget = snap.equity * L.risk_per_trade
        rule("trade_risk_within_limit", size.ok and size.risk_amount <= budget,
             f"{size.risk_amount:.4f} vs {budget:.4f}")
        if L.max_capital_at_risk is not None:
            rule("capital_cap", size.notional + sum((p.value for p in snap.positions), Decimal(0)) <= L.max_capital_at_risk,
                 f"cap {L.max_capital_at_risk}")

        new_pos = OpenPosition(proposal.symbol, size.qty, proposal.entry, proposal.stop, proposal.entry,
                               proposal.asset_class, proposal.beta, proposal.sign,
                               instrument.contract_size, proposal.fx)
        after = snap.positions + ((new_pos,) if size.ok else ())
        total = open_risk(after, L.gap_allowance)
        rule("open_risk_within_limit", total <= snap.equity * L.max_open_risk,
             f"{total:.4f} vs {snap.equity * L.max_open_risk:.4f}")
        cluster = snap.cluster_of(proposal.symbol)
        in_cluster = [p for p in after if snap.cluster_of(p.symbol) == cluster]
        c_risk = open_risk(in_cluster, L.gap_allowance)
        rule("cluster_risk_within_limit", c_risk <= snap.equity * L.max_cluster_risk,
             f"{cluster}: {c_risk:.4f} vs {snap.equity * L.max_cluster_risk:.4f}")
        rule("daily_loss_ok", snap.day_loss < L.daily_loss, f"{snap.day_loss:.4%}")
        rule("weekly_loss_ok", snap.week_loss < L.weekly_loss, f"{snap.week_loss:.4%}")
        rule("drawdown_ok", snap.drawdown < L.max_drawdown, f"{snap.drawdown:.4%}")
        rule("consecutive_losses_ok", snap.consecutive_losses < L.max_consecutive_losses,
             str(snap.consecutive_losses))
        rule("position_count_ok", len(snap.positions) < L.max_positions,
             f"{len(snap.positions)} open, cap {L.max_positions}")
        s_loss = stress_loss(after, self.stress)
        rule("stress_loss_within_limit", s_loss <= snap.equity * L.max_stress_loss,
             f"{s_loss:.4f} vs {snap.equity * L.max_stress_loss:.4f}")

        ok = all(r.passed for r in rules)
        return RiskDecision(Decision.APPROVE if ok else Decision.REJECT,
                            size.qty if ok else Decimal(0), size.risk_amount if ok else Decimal(0),
                            size.notional if ok else Decimal(0), tuple(rules), L.version)

    def halts(self, snap: PortfolioSnapshot) -> tuple[Halt, str]:
        L = self.limits
        if snap.drawdown >= L.max_drawdown:
            return Halt.RISK_SHUTDOWN, f"drawdown {snap.drawdown:.2%} reached limit {L.max_drawdown:.2%}"
        if snap.week_loss >= L.weekly_loss:
            return Halt.RISK_SHUTDOWN, f"weekly loss {snap.week_loss:.2%} reached limit {L.weekly_loss:.2%}"
        if snap.day_loss >= L.daily_loss:
            return Halt.PAUSE_DAY, f"daily loss {snap.day_loss:.2%} reached limit {L.daily_loss:.2%}"
        if snap.consecutive_losses >= L.max_consecutive_losses:
            return Halt.PAUSE_LOSSES, f"{snap.consecutive_losses} losses in a row"
        return Halt.NONE, ""

    def with_limits(self, **changes) -> RiskEngine:
        return RiskEngine(replace(self.limits, **changes), self.stress)
