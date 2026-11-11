"""Order model and state machine (spec 3.6, 14).

    NEW -> SUBMITTING -> OPEN -> PARTIALLY_FILLED -> FILLED
                 |          \\-> CANCELING -> CANCELED
                 |-> REJECTED, EXPIRED
                 \\-> UNKNOWN  (timeout or 5xx: the venue may or may not have it)

UNKNOWN can only be left through reconciliation (asking the venue by client order id).
Filled quantity never decreases. Final states never change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class OrderStatus(StrEnum):
    NEW = "new"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELING = "canceling"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS_LIMIT = "stop_loss_limit"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


S = OrderStatus
FINAL = {S.FILLED, S.CANCELED, S.REJECTED, S.EXPIRED}
_ALLOWED = {
    S.NEW: {S.SUBMITTING},
    S.SUBMITTING: {S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.EXPIRED, S.CANCELED, S.UNKNOWN},
    S.OPEN: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELING, S.CANCELED, S.EXPIRED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELING, S.CANCELED, S.EXPIRED, S.UNKNOWN},
    S.CANCELING: {S.CANCELED, S.FILLED, S.PARTIALLY_FILLED, S.UNKNOWN},
    # UNKNOWN -> SUBMITTING only after the venue CONFIRMED it does not have the order
    S.UNKNOWN: {S.SUBMITTING, S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED, S.EXPIRED},
}


class OrderStateError(Exception):
    pass


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False     # exits: never increase exposure


@dataclass(frozen=True)
class VenueOrderState:
    """What the venue says about an order."""
    client_order_id: str
    status: OrderStatus
    filled_qty: Decimal
    avg_price: Decimal | None
    venue_order_id: str | None = None
    fee: Decimal = Decimal(0)
    fee_asset: str | None = None


@dataclass(frozen=True)
class OrderEvent:
    at: datetime
    from_status: OrderStatus
    to_status: OrderStatus
    filled_qty: Decimal
    note: str = ""


@dataclass
class Order:
    request: OrderRequest
    status: OrderStatus = S.NEW
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    venue_order_id: str | None = None
    fee: Decimal = Decimal(0)
    events: list[OrderEvent] = field(default_factory=list)

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def is_final(self) -> bool:
        return self.status in FINAL

    @property
    def remaining(self) -> Decimal:
        return self.request.qty - self.filled_qty

    def transition(self, to: OrderStatus, note: str = "", at: datetime | None = None) -> None:
        if self.is_final:
            raise OrderStateError(f"{self.client_order_id} is {self.status}; it cannot change")
        if to not in _ALLOWED.get(self.status, set()):
            raise OrderStateError(f"{self.client_order_id}: {self.status} -> {to} not allowed")
        self.events.append(OrderEvent(at or datetime.now(UTC), self.status, to, self.filled_qty, note))
        self.status = to

    def apply(self, state: VenueOrderState, note: str = "", at: datetime | None = None) -> None:
        """Bring the order in line with the venue's view (fills only ever grow)."""
        if state.client_order_id != self.client_order_id:
            raise OrderStateError("venue state is for a different order")
        if state.filled_qty < self.filled_qty:
            raise OrderStateError(f"venue reports less filled ({state.filled_qty}) than known ({self.filled_qty})")
        if state.filled_qty > self.request.qty:
            raise OrderStateError("venue reports more filled than ordered")
        self.filled_qty = state.filled_qty
        self.avg_price = state.avg_price if state.avg_price is not None else self.avg_price
        self.venue_order_id = state.venue_order_id or self.venue_order_id
        self.fee = max(self.fee, state.fee)
        if state.status != self.status or state.status is S.PARTIALLY_FILLED:
            self.transition(state.status, note, at)
