"""Can this capital trade this instrument at all? Answer per instrument, with reasons.

An instrument is UNAVAILABLE when a position sized to the risk budget would be below the venue
minimum, when the minimum order costs more than the capital allows, when round-trip cost eats too
much of the planned target, or when shorting is needed and the venue does not allow it. We never
raise leverage or risk to make an instrument look feasible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from nextrade_core.costs.model import CostModel
from nextrade_core.domain.instrument import AssetClass, Instrument


@dataclass(frozen=True)
class Feasibility:
    symbol: str
    available: bool
    reasons: list[str] = field(default_factory=list)
    qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    cost_share_of_target: float = 0.0     # round-trip cost / planned target move


def check_instrument(inst: Instrument, *, capital: Decimal, price: Decimal, stop_pct: float, target_pct: float,
                     risk_fraction: Decimal, costs: CostModel, leverage: Decimal = Decimal(1),
                     max_leverage: Decimal = Decimal(1), needs_short: bool = False, can_short: bool = False,
                     fx: Decimal = Decimal(1), max_cost_share: float = 0.35) -> Feasibility:
    why: list[str] = []
    if not inst.tradable:
        why.append("instrument not tradable on the venue")
    if needs_short and not can_short:
        why.append("strategy needs shorting; this venue/account cannot short")
    if leverage > max_leverage:
        why.append(f"leverage {leverage} exceeds the configured maximum {max_leverage}")
    if price <= 0 or stop_pct <= 0 or target_pct <= 0:
        why.append("invalid price or stop/target distance")
        return Feasibility(inst.symbol, False, why)
    cost = costs.round_trip_fraction(inst.symbol)
    share = cost / target_pct
    if share > max_cost_share:
        why.append(f"round-trip cost {cost:.3%} is {share:.0%} of the {target_pct:.3%} target "
                   f"(limit {max_cost_share:.0%})")
    unit_value = price * inst.contract_size * fx
    risk_budget = capital * risk_fraction
    per_unit_risk = unit_value * (Decimal(str(stop_pct)) + Decimal(str(cost)))
    raw = risk_budget / per_unit_risk if per_unit_risk > 0 else Decimal(0)
    cap_qty = capital * leverage / (unit_value * (1 + Decimal(str(cost))))
    qty = min(raw, cap_qty)
    step = inst.step_size or Decimal(1)
    qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if qty < inst.min_qty:
        why.append(f"risk-sized quantity {qty} is below the venue minimum {inst.min_qty}")
    notional = qty * unit_value
    if inst.min_notional and notional < inst.min_notional:
        why.append(f"order value {notional:.4g} is below the venue minimum {inst.min_notional}")
    if inst.asset_class is AssetClass.FOREX and leverage <= 1 and qty < inst.min_qty:
        why.append("forex needs margin; capital too small for the minimum lot without extra leverage")
    return Feasibility(inst.symbol, not why, why, qty, notional, share)
