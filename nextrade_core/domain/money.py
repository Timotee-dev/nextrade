"""Exact decimal arithmetic for every financial value in NEXTRADE.

Rule: money, prices, quantities and fees are Decimal. Floats are refused at the
boundary, because a float like 0.1 cannot be represented exactly and errors
compound across thousands of fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation

Numeric = Decimal | int | str


def to_decimal(value: Numeric) -> Decimal:
    """Convert to Decimal, refusing floats and bools.

    Strings are accepted because exchanges send numbers as strings
    ("0.00010000"), which preserves their exact value.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a numeric amount")
    if isinstance(value, float):
        raise TypeError(
            f"float {value!r} refused: pass a str or Decimal to keep exact precision"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    else:
        raise TypeError(f"unsupported numeric type: {type(value).__name__}")
    if not result.is_finite():
        raise ValueError(f"non-finite amount refused: {value!r}")
    return result


def floor_to_step(value: Numeric, step: Numeric) -> Decimal:
    """Round down to a multiple of step (used for order quantities).

    Rounding quantity down never increases risk beyond what was approved.
    """
    v, s = to_decimal(value), to_decimal(step)
    if s <= 0:
        raise ValueError("step must be positive")
    return ((v / s).to_integral_value(rounding=ROUND_FLOOR) * s).quantize(s)


def ceil_to_step(value: Numeric, step: Numeric) -> Decimal:
    """Round up to a multiple of step (used only to reach a venue minimum)."""
    v, s = to_decimal(value), to_decimal(step)
    if s <= 0:
        raise ValueError("step must be positive")
    return ((v / s).to_integral_value(rounding=ROUND_CEILING) * s).quantize(s)


def round_to_tick(price: Numeric, tick: Numeric) -> Decimal:
    """Round a price to the nearest valid tick (banker's rounding on ties)."""
    p, t = to_decimal(price), to_decimal(tick)
    if t <= 0:
        raise ValueError("tick must be positive")
    return ((p / t).to_integral_value(rounding=ROUND_HALF_EVEN) * t).quantize(t)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in one currency or asset (NGN, USDT, BTC...).

    Arithmetic across currencies raises instead of silently mixing units.
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_decimal(self.amount))
        if not self.currency or not self.currency.isupper():
            raise ValueError(f"currency must be an upper-case code, got {self.currency!r}")

    @classmethod
    def of(cls, amount: Numeric, currency: str) -> Money:
        return cls(to_decimal(amount), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal(0), currency)

    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError("can only combine Money with Money")
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: Numeric) -> Money:
        return Money(self.amount * to_decimal(factor), self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.amount >= other.amount

    def convert(self, rate: Numeric, to_currency: str) -> Money:
        """Convert using an explicit rate (units of to_currency per 1 unit of self)."""
        return Money(self.amount * to_decimal(rate), to_currency)

    def quantize(self, places: int = 2) -> Money:
        return Money(self.amount.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN), self.currency)

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
