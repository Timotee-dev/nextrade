"""Paper broker: a simulated spot venue that behaves like Binance where it matters.

* Market orders fill against the latest quote (buy at ask, sell at bid) plus slippage.
* Limit buys fill once the ask reaches the limit; limit sells once the bid reaches it.
* Protective exits are OCO pairs: a take-profit limit and a stop. The stop fills at the
  worse of the stop price and the bid when triggered (paper models a stop-market; the
  live venue uses stop-limit, which can fail to fill in a gap - see docs).
* Duplicate client order ids are rejected, as Binance does.
* Fault injection (`fail_next`, `lose_next_response`, `partial_fraction`) drives the
  execution engine's timeout, reconciliation and partial-fill tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from adapters.base import RejectedError
from nextrade_core.execution.orders import OrderRequest, OrderSide, OrderStatus, OrderType, VenueOrderState


@dataclass
class _PaperOrder:
    req: OrderRequest
    status: OrderStatus = OrderStatus.OPEN
    filled: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)        # sum of qty * price
    fee: Decimal = Decimal(0)
    oco_peer: str | None = None
    venue_id: str = ""

    def state(self) -> VenueOrderState:
        avg = (self.cost / self.filled) if self.filled else None
        return VenueOrderState(self.req.client_order_id, self.status, self.filled, avg, self.venue_id,
                               self.fee, "USDT")


@dataclass
class PaperBroker:
    quote_asset: str = "USDT"
    fee_rate: Decimal = Decimal("0.001")
    slippage: Decimal = Decimal("0.0002")
    balances_: dict[str, Decimal] = field(default_factory=lambda: {"USDT": Decimal(1000)})
    name: str = "paper"
    fail_next: list[Exception] = field(default_factory=list)
    lose_next_response: int = 0
    partial_fraction: Decimal | None = None
    quotes: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    orders: dict[str, _PaperOrder] = field(default_factory=dict)
    submit_calls: int = 0
    _seq: int = 0

    # ------------------------------------------------------------------ market data
    def update_quote(self, symbol: str, bid: Decimal, ask: Decimal) -> list[VenueOrderState]:
        """Feed a new best bid/ask; returns states of orders that changed."""
        self.quotes[symbol] = (Decimal(bid), Decimal(ask))
        changed = []
        for o in list(self.orders.values()):
            if o.req.symbol != symbol or o.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                continue
            if self._try_fill(o):
                changed.append(o.state())
        return changed

    def _base(self, symbol: str) -> str:
        return symbol[: -len(self.quote_asset)]

    # ------------------------------------------------------------------ fills
    def _try_fill(self, o: _PaperOrder) -> bool:
        bid, ask = self.quotes.get(o.req.symbol, (None, None))
        if bid is None:
            return False
        r = o.req
        price = None
        if r.type is OrderType.MARKET:
            price = ask * (1 + self.slippage) if r.side is OrderSide.BUY else bid * (1 - self.slippage)
        elif r.type is OrderType.LIMIT:
            if r.side is OrderSide.BUY and ask <= r.price:
                price = min(ask, r.price)
            elif r.side is OrderSide.SELL and bid >= r.price:
                price = max(bid, r.price)
        elif r.type is OrderType.STOP_LOSS_LIMIT and bid <= r.stop_price:
            price = min(bid, r.stop_price) * (1 - self.slippage)
        if price is None:
            return False
        qty = o.req.qty - o.filled
        if self.partial_fraction is not None and o.filled == 0 and r.type is OrderType.MARKET:
            qty = (o.req.qty * self.partial_fraction).quantize(Decimal("0.00000001"))
        self._settle(o, qty, price)
        return True

    def _settle(self, o: _PaperOrder, qty: Decimal, price: Decimal) -> None:
        base, notional = self._base(o.req.symbol), qty * price
        fee = notional * self.fee_rate
        if o.req.side is OrderSide.BUY:
            self.balances_[self.quote_asset] -= notional + fee
            self.balances_[base] = self.balances_.get(base, Decimal(0)) + qty
        else:
            self.balances_[base] = self.balances_.get(base, Decimal(0)) - qty
            self.balances_[self.quote_asset] += notional - fee
        o.filled += qty
        o.cost += notional
        o.fee += fee
        o.status = OrderStatus.FILLED if o.filled >= o.req.qty else OrderStatus.PARTIALLY_FILLED
        if o.status is OrderStatus.FILLED and o.oco_peer:
            peer = self.orders.get(o.oco_peer)
            if peer and peer.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                peer.status = OrderStatus.CANCELED

    # ------------------------------------------------------------------ broker contract
    async def submit(self, request: OrderRequest) -> VenueOrderState:
        self.submit_calls += 1
        if self.fail_next:
            raise self.fail_next.pop(0)
        existing = self.orders.get(request.client_order_id)
        if existing is not None:
            raise RejectedError("Duplicate order sent.", code=-2010)
        r = request
        if r.side is OrderSide.BUY:
            bid, ask = self.quotes.get(r.symbol, (None, None))
            ref = r.price if r.type is OrderType.LIMIT else ask
            if ref is None:
                raise RejectedError("no market price", code=-1013)
            if (r.qty * ref) * (1 + self.fee_rate + self.slippage) > self.balances_.get(self.quote_asset, Decimal(0)):
                raise RejectedError("Account has insufficient balance for requested action.", code=-2010)
        else:
            held = self.balances_.get(self._base(r.symbol), Decimal(0)) - self._reserved_sells(r.symbol)
            if r.qty > held:
                raise RejectedError("Account has insufficient balance for requested action.", code=-2010)
        self._seq += 1
        o = _PaperOrder(r, venue_id=str(self._seq))
        self.orders[r.client_order_id] = o
        self._try_fill(o)
        if self.lose_next_response:
            self.lose_next_response -= 1
            from adapters.base import UnknownStateError
            raise UnknownStateError("simulated timeout after the venue accepted the order")
        return o.state()

    def _reserved_sells(self, symbol: str) -> Decimal:
        seen, total = set(), Decimal(0)
        for o in self.orders.values():
            if (o.req.symbol == symbol and o.req.side is OrderSide.SELL
                    and o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)):
                group = tuple(sorted({o.req.client_order_id, o.oco_peer or o.req.client_order_id}))
                if group not in seen:
                    seen.add(group)
                    total += o.req.qty - o.filled
        return total

    async def get(self, symbol: str, client_order_id: str) -> VenueOrderState | None:
        o = self.orders.get(client_order_id)
        return o.state() if o else None

    async def cancel(self, symbol: str, client_order_id: str) -> VenueOrderState:
        o = self.orders.get(client_order_id)
        if o is None or o.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            raise RejectedError("Unknown order sent.", code=-2011)
        o.status = OrderStatus.CANCELED
        return o.state()

    async def open_orders(self, symbol: str | None = None) -> list[VenueOrderState]:
        return [o.state() for o in self.orders.values()
                if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
                and (symbol is None or o.req.symbol == symbol)]

    async def balances(self) -> dict[str, Decimal]:
        return dict(self.balances_)

    async def place_protective_exit(self, symbol: str, qty: Decimal, stop: Decimal, target: Decimal,
                                    list_client_id: str) -> list[VenueOrderState]:
        held = self.balances_.get(self._base(symbol), Decimal(0)) - self._reserved_sells(symbol)
        if qty > held:
            raise RejectedError("Account has insufficient balance for requested action.", code=-2010)
        tp = _PaperOrder(OrderRequest(f"{list_client_id}-tp", symbol, OrderSide.SELL, OrderType.LIMIT, qty, target))
        sl = _PaperOrder(OrderRequest(f"{list_client_id}-sl", symbol, OrderSide.SELL, OrderType.STOP_LOSS_LIMIT,
                                      qty, stop * Decimal("0.995"), stop))
        tp.oco_peer, sl.oco_peer = sl.req.client_order_id, tp.req.client_order_id
        for o in (tp, sl):
            self._seq += 1
            o.venue_id = str(self._seq)
            self.orders[o.req.client_order_id] = o
        self._try_fill(sl) or self._try_fill(tp)
        return [tp.state(), sl.state()]
