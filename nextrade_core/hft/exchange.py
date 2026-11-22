"""Paper exchange for the high-frequency scalper: simulated fills against the REAL best bid/ask and trade tape.

Rules (conservative on purpose; every rule can only make the result worse than an ideal fill):
* Every order reaches the "exchange" `latency_s` after it is sent (default 150 ms) and is filled against the book at that moment.
* Market buy: fills at the ask plus `slippage_bps`; market sell: at the bid minus `slippage_bps`. Quantity is capped by the
  displayed size at that level (immediate-or-cancel: the rest is not filled). A stale book (no quote for 2 s) rejects the order.
* Resting limit sell (a profit target): fills only when the market trades STRICTLY through the price, or the best bid is at or
  above it. It fills at its own price, never better, and a trade exactly at the price does not fill it (queue position unknown).
* Fees are charged on every fill (taker rate for market orders, maker rate for limit orders), in the quote currency.
* A cancel also takes `latency_s` to arrive, so an order can still fill while its cancel is in flight.
* Duplicate client order ids are rejected. There is no code path to a real exchange in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import Quote, Trade
from .market import SymbolState


class DuplicateOrder(RuntimeError):
    pass


@dataclass
class Order:
    client_id: str
    symbol: str
    side: str                  # "buy" | "sell"
    kind: str                  # "market" | "limit"
    qty: float
    price: float | None
    active_at: float
    status: str = "pending"    # pending | open | filled | cancelled | rejected
    cancel_at: float | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    client_id: str
    symbol: str
    side: str
    kind: str
    qty: float
    price: float
    fee: float
    ts: float
    mid_at_fill: float


@dataclass(frozen=True)
class InstrumentRules:
    step: float = 0.0001
    min_qty: float = 0.0001
    min_notional: float = 1.0

    def floor_qty(self, qty: float) -> float:
        n = int(qty / self.step + 1e-9)
        return round(n * self.step, 12)


@dataclass
class PaperExchange:
    cash: float
    taker_fee: float = 0.001
    maker_fee: float = 0.001
    latency_s: float = 0.15
    slippage_bps: float = 1.0
    max_quote_age_s: float = 2.0
    rules: dict[str, InstrumentRules] = field(default_factory=dict)
    holdings: dict[str, float] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    submitted: int = 0
    rejected: int = 0

    def rule(self, symbol: str) -> InstrumentRules:
        return self.rules.get(symbol) or InstrumentRules()

    # ------------------------------------------------------------------ order entry
    def _new(self, client_id: str, symbol: str, side: str, kind: str, qty: float, price: float | None, now: float) -> Order:
        if client_id in self.orders:
            raise DuplicateOrder(client_id)
        o = Order(client_id, symbol, side, kind, qty, price, now + self.latency_s)
        self.orders[client_id] = o
        self.submitted += 1
        return o

    def market_buy(self, client_id: str, symbol: str, qty: float, now: float) -> Order:
        return self._new(client_id, symbol, "buy", "market", qty, None, now)

    def market_sell(self, client_id: str, symbol: str, qty: float, now: float) -> Order:
        return self._new(client_id, symbol, "sell", "market", qty, None, now)

    def limit_sell(self, client_id: str, symbol: str, qty: float, price: float, now: float) -> Order:
        return self._new(client_id, symbol, "sell", "limit", qty, price, now)

    def cancel(self, client_id: str, now: float) -> None:
        o = self.orders.get(client_id)
        if o is not None and o.status in ("pending", "open") and o.cancel_at is None:
            o.cancel_at = now + self.latency_s

    # ------------------------------------------------------------------ matching
    def _reject(self, o: Order, why: str) -> None:
        o.status, o.reason = "rejected", why
        self.rejected += 1

    def _fill(self, o: Order, qty: float, price: float, fee_rate: float, st: SymbolState, now: float) -> Fill:
        notional = qty * price
        fee = notional * fee_rate
        if o.side == "buy":
            self.cash -= notional + fee
            self.holdings[o.symbol] = self.holdings.get(o.symbol, 0.0) + qty
        else:
            self.cash += notional - fee
            self.holdings[o.symbol] = self.holdings.get(o.symbol, 0.0) - qty
        o.status = "filled"
        return Fill(o.client_id, o.symbol, o.side, o.kind, qty, price, fee, now, st.mid)

    def process(self, st: SymbolState, now: float, trade: Trade | None = None) -> list[Fill]:
        """Advance every order on `st.symbol` to `now`; `trade` is the print that triggered this call, if any."""
        out: list[Fill] = []
        for o in list(self.orders.values()):
            if o.symbol != st.symbol or o.status in ("filled", "cancelled", "rejected"):
                continue
            if o.cancel_at is not None and now >= o.cancel_at and now >= o.active_at:
                o.status = "cancelled"
                continue
            if now < o.active_at:
                continue
            if o.kind == "market":
                if not st.fresh(now, self.max_quote_age_s):
                    self._reject(o, "stale book")
                    continue
                slip = self.slippage_bps / 10_000
                if o.side == "buy":
                    px, avail = st.ask * (1 + slip), st.ask_sz
                    qty = min(self.rule(o.symbol).floor_qty(o.qty), self.rule(o.symbol).floor_qty(avail))
                    if qty < self.rule(o.symbol).min_qty or qty * px < self.rule(o.symbol).min_notional:
                        self._reject(o, "size or notional below the minimum after capping to the displayed size")
                        continue
                    if self.cash < qty * px * (1 + self.taker_fee):
                        self._reject(o, "insufficient cash")
                        continue
                else:
                    px, avail = st.bid * (1 - slip), st.bid_sz
                    held = self.holdings.get(o.symbol, 0.0)
                    qty = min(o.qty, held, avail)
                    if qty <= 1e-12:
                        self._reject(o, "nothing to sell or no bid size")
                        continue
                out.append(self._fill(o, qty, px, self.taker_fee, st, now))
            else:                                   # resting limit sell
                if o.status == "pending":
                    o.status = "open"
                price = float(o.price)
                crossed = st.fresh(now, 1e9) and st.bid >= price
                through = trade is not None and trade.symbol == o.symbol and trade.price > price
                if crossed or through:
                    qty = min(o.qty, self.holdings.get(o.symbol, 0.0))
                    if qty > 0:
                        out.append(self._fill(o, qty, price, self.maker_fee, st, now))
                    else:
                        o.status = "cancelled"
        return out

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.status in ("pending", "open") and (symbol is None or o.symbol == symbol)]
