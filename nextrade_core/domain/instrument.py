"""Venue-independent description of a tradable instrument and its order rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .money import Numeric, ceil_to_step, floor_to_step, round_to_tick, to_decimal


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    FOREX = "forex"


@dataclass(frozen=True, slots=True)
class Instrument:
    venue: str
    symbol: str
    base: str
    quote: str
    asset_class: AssetClass
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal | None = None
    contract_size: Decimal = Decimal(1)
    tradable: bool = True
    order_types: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("tick_size", "step_size", "min_qty", "min_notional", "contract_size"):
            object.__setattr__(self, name, to_decimal(getattr(self, name)))
        if self.max_qty is not None:
            object.__setattr__(self, "max_qty", to_decimal(self.max_qty))
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError(f"{self.symbol}: tick and step must be positive")

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"

    def normalize_qty(self, qty: Numeric) -> Decimal:
        """Round quantity DOWN to the venue step."""
        return floor_to_step(qty, self.step_size)

    def normalize_price(self, price: Numeric) -> Decimal:
        return round_to_tick(price, self.tick_size)

    def notional(self, qty: Numeric, price: Numeric) -> Decimal:
        return to_decimal(qty) * to_decimal(price) * self.contract_size

    def min_qty_for_notional(self, price: Numeric) -> Decimal:
        """Smallest valid quantity whose notional meets both venue minimums."""
        p = to_decimal(price)
        if p <= 0:
            raise ValueError("price must be positive")
        by_notional = ceil_to_step(self.min_notional / (p * self.contract_size), self.step_size)
        return max(by_notional, ceil_to_step(self.min_qty, self.step_size))

    def order_violations(self, qty: Numeric, price: Numeric) -> list[str]:
        """Return every reason the venue would reject this order (empty list = valid)."""
        q, p = to_decimal(qty), to_decimal(price)
        problems: list[str] = []
        if not self.tradable:
            problems.append("instrument not tradable")
        if q <= 0:
            problems.append("quantity must be positive")
        if p <= 0:
            problems.append("price must be positive")
        if q > 0 and q != floor_to_step(q, self.step_size):
            problems.append(f"quantity {q} not a multiple of step {self.step_size}")
        if p > 0 and p != round_to_tick(p, self.tick_size):
            problems.append(f"price {p} not a multiple of tick {self.tick_size}")
        if 0 < q < self.min_qty:
            problems.append(f"quantity {q} below minimum {self.min_qty}")
        if self.max_qty is not None and q > self.max_qty:
            problems.append(f"quantity {q} above maximum {self.max_qty}")
        if q > 0 and p > 0 and self.notional(q, p) < self.min_notional:
            problems.append(
                f"notional {self.notional(q, p)} below minimum {self.min_notional} {self.quote}"
            )
        return problems
