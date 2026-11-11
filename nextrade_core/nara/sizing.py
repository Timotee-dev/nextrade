"""Position sizing: risk a fixed fraction of equity, costs included, never round up.

    q = floor_step( (E*r - C) / (|entry - stop| * contract * fx) )

E equity, r risk fraction, C estimated round-trip costs for the position, fx converts
the quote currency to the account currency. After rounding: if the venue minimum
order would push risk above E*r, the trade is REJECTED, never enlarged.
Spot also needs the full notional in free cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nextrade_core.domain.instrument import Instrument
from nextrade_core.domain.money import to_decimal


@dataclass(frozen=True)
class SizeResult:
    qty: Decimal
    risk_amount: Decimal       # loss at the stop including costs, account currency
    notional: Decimal
    reject_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reject_reason is None and self.qty > 0


def size_position(*, equity: Decimal, risk_fraction: Decimal, entry: Decimal, stop: Decimal,
                  instrument: Instrument, cost_fraction: Decimal, free_cash: Decimal,
                  fx: Decimal = Decimal(1)) -> SizeResult:
    equity, risk_fraction, entry, stop = map(to_decimal, (equity, risk_fraction, entry, stop))
    cost_fraction, free_cash, fx = map(to_decimal, (cost_fraction, free_cash, fx))
    zero = Decimal(0)
    if entry <= stop:
        return SizeResult(zero, zero, zero, "stop must be below entry for a long")
    budget = equity * risk_fraction
    per_unit_loss = ((entry - stop) + entry * cost_fraction) * instrument.contract_size * fx
    raw = budget / per_unit_loss
    qty = instrument.normalize_qty(raw)
    cash_cap = instrument.normalize_qty(free_cash / (entry * instrument.contract_size * fx * (1 + cost_fraction)))
    qty = min(qty, cash_cap)
    min_qty = instrument.min_qty_for_notional(entry)
    if qty < min_qty:
        needed = min_qty * per_unit_loss
        reason = ("venue minimum order needs more cash than is free" if cash_cap < min_qty
                  else f"venue minimum order would risk {needed:.4f}, above the budget {budget:.4f}")
        return SizeResult(zero, zero, zero, reason)
    problems = instrument.order_violations(qty, instrument.normalize_price(entry))
    if problems:
        return SizeResult(zero, zero, zero, "; ".join(problems))
    return SizeResult(qty, qty * per_unit_loss, qty * entry * instrument.contract_size * fx)
