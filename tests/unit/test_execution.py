import asyncio
import json
import time
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from adapters.base import RejectedError, RetryableError, UnknownStateError
from adapters.binance.filters import instrument_from_symbol
from adapters.paper.broker import PaperBroker
from nextrade_core.domain.ids import make_client_order_id
from nextrade_core.execution.engine import DuplicateOrderError, ExecutionEngine
from nextrade_core.execution.orders import (Order, OrderRequest, OrderSide, OrderStateError, OrderStatus as S,
                                            OrderType, VenueOrderState)
from nextrade_core.execution.ratelimit import TokenBucket
from nextrade_core.nara.engine import RiskEngine, TradeProposal
from nextrade_core.nara.limits import RiskLimits
from nextrade_core.nara.portfolio import OpenPosition, PortfolioSnapshot
from nextrade_core.nctpi.allocation import Candidate, Funnel, OpportunityQueue, allocate, capacity
from tests.helpers import T0

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())


def run(coro):
    return asyncio.run(coro)


def market_buy(cid="c1", symbol="BTCUSDT", qty="0.001"):
    return OrderRequest(cid, symbol, OrderSide.BUY, OrderType.MARKET, D(qty))


def broker(**kw):
    b = PaperBroker(**kw)
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"):
        b.update_quote(s, D(100), D("100.1"))
    return b


def engine(b, **kw):
    return ExecutionEngine(b, limiter=TokenBucket(1000, 1000), retry_delay_s=0.001, reconcile_delay_s=0.001, **kw)


# ------------------------------------------------------------------ order state machine
def test_order_state_rules():
    o = Order(market_buy())
    o.transition(S.SUBMITTING)
    o.apply(VenueOrderState("c1", S.PARTIALLY_FILLED, D("0.0005"), D(100)))
    o.apply(VenueOrderState("c1", S.FILLED, D("0.001"), D(100)))
    assert o.is_final and o.remaining == 0 and len(o.events) == 3
    with pytest.raises(OrderStateError):
        o.transition(S.CANCELED)
    p = Order(market_buy("c2"))
    p.transition(S.SUBMITTING)
    p.apply(VenueOrderState("c2", S.PARTIALLY_FILLED, D("0.0005"), D(100)))
    with pytest.raises(OrderStateError):
        p.apply(VenueOrderState("c2", S.PARTIALLY_FILLED, D("0.0001"), D(100)))   # fills cannot shrink
    with pytest.raises(OrderStateError):
        Order(market_buy()).transition(S.FILLED)                                     # must submit first


# ------------------------------------------------------------------ execution engine
def test_market_order_fills():
    b = broker()
    o = run(engine(b).submit(market_buy()))
    assert o.status is S.FILLED and o.avg_price > D("100.1") and b.balances_["BTC"] == D("0.001")


def test_rejection_is_final_and_not_retried():
    b = broker(balances_={"USDT": D(1)})
    e = engine(b)
    o = run(e.submit(market_buy(qty="1")))   # 100 USDT order, 1 USDT balance
    assert o.status is S.REJECTED and b.submit_calls == 1


def test_retryable_errors_are_retried():
    b = broker(fail_next=[RetryableError("503 before acceptance"), RetryableError("again")])
    o = run(engine(b).submit(market_buy()))
    assert o.status is S.FILLED and b.submit_calls == 3


def test_lost_response_is_reconciled_without_a_duplicate():
    """The venue accepted the order but the reply was lost: never send it twice."""
    b = broker(lose_next_response=1)
    e = engine(b)
    o = run(e.submit(market_buy()))
    assert o.status is S.FILLED and b.submit_calls == 1 and len(b.orders) == 1
    assert [ev.to_status for ev in o.events][-2:] == [S.UNKNOWN, S.FILLED]
    assert e.stats["reconciled"] == 1


def test_timeout_before_acceptance_resends_the_same_id_once():
    b = broker(fail_next=[UnknownStateError("connection dropped before the venue saw it")])
    o = run(engine(b).submit(market_buy()))
    assert o.status is S.FILLED and b.submit_calls == 2 and list(b.orders) == ["c1"]


def test_unreachable_venue_leaves_order_unknown_and_reported():
    class DeadGets(PaperBroker):
        async def get(self, symbol, cid):
            raise RetryableError("venue down")

    b = DeadGets(lose_next_response=1)
    b.update_quote("BTCUSDT", D(100), D("100.1"))
    events = []
    e = engine(b, reconcile_attempts=2, on_event=lambda o, what: events.append(what))
    o = run(e.submit(market_buy()))
    assert o.status is S.UNKNOWN and e.unresolved == [o] and "reconcile_failed" in events


def test_engine_refuses_duplicate_ids_and_venue_refuses_duplicates():
    b = broker()
    e = engine(b)
    run(e.submit(market_buy()))
    with pytest.raises(DuplicateOrderError):
        run(e.submit(market_buy()))
    with pytest.raises(RejectedError):
        run(b.submit(market_buy()))


def test_concurrent_orders_across_symbols_no_duplicates_and_serialised_per_symbol():
    active, peak = {}, {}

    class Slow(PaperBroker):
        async def submit(self, req):
            active[req.symbol] = active.get(req.symbol, 0) + 1
            peak[req.symbol] = max(peak.get(req.symbol, 0), active[req.symbol])
            await asyncio.sleep(0.005)
            try:
                return await super().submit(req)
            finally:
                active[req.symbol] -= 1

    b = Slow(balances_={"USDT": D(100000)})
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    for s in symbols:
        b.update_quote(s, D(100), D("100.1"))
    e = engine(b)
    reqs = [market_buy(make_client_order_id("run", f"opp{i}", "entry"), symbols[i % 5]) for i in range(25)]

    async def go():
        t = time.perf_counter()
        orders = await asyncio.gather(*(e.submit(r) for r in reqs))
        return orders, time.perf_counter() - t

    orders, elapsed = run(go())
    assert all(o.status is S.FILLED for o in orders) and len(b.orders) == 25
    assert max(peak.values()) == 1                        # one in flight per symbol
    assert elapsed < 25 * 0.005                           # symbols ran in parallel


def test_partial_fill_then_cancel_remainder():
    b = broker(partial_fraction=D("0.4"))
    e = engine(b)

    async def go():
        o = await e.submit(market_buy(qty="0.001"))
        assert o.status is S.PARTIALLY_FILLED and o.filled_qty == D("0.0004")
        return await e.cancel_remainder_after(o, 0.001)

    o = run(go())
    assert o.status is S.CANCELED and o.filled_qty == D("0.0004")


def test_cancel_of_already_filled_order_refreshes():
    b = broker()
    e = engine(b)
    o = run(e.submit(OrderRequest("lim", "BTCUSDT", OrderSide.BUY, OrderType.LIMIT, D("0.001"), D(99))))
    assert o.status is S.OPEN
    b.update_quote("BTCUSDT", D("98.9"), D(99))         # fills at the venue meanwhile
    o = run(e.cancel(o))
    assert o.status is S.FILLED


def test_token_bucket_limits_rate():
    async def go():
        bucket = TokenBucket(rate_per_s=200, burst=2)
        t = time.perf_counter()
        for _ in range(12):
            await bucket.acquire()
        return time.perf_counter() - t
    assert run(go()) >= 0.045


# ------------------------------------------------------------------ paper protective exits
def test_oco_stop_cancels_take_profit():
    b = broker()
    run(b.submit(market_buy(qty="0.01")))
    run(b.place_protective_exit("BTCUSDT", D("0.01"), stop=D(95), target=D(110), list_client_id="x"))
    with pytest.raises(RejectedError):                    # quantity already reserved by the OCO
        run(b.place_protective_exit("BTCUSDT", D("0.01"), stop=D(95), target=D(110), list_client_id="y"))
    b.update_quote("BTCUSDT", D(94), D("94.1"))
    assert b.orders["x-sl"].status is S.FILLED and b.orders["x-tp"].status is S.CANCELED
    assert b.balances_["BTC"] == 0


def test_oco_target_cancels_stop():
    b = broker()
    run(b.submit(market_buy(qty="0.01")))
    run(b.place_protective_exit("BTCUSDT", D("0.01"), stop=D(95), target=D(110), list_client_id="x"))
    b.update_quote("BTCUSDT", D(111), D("111.1"))
    assert b.orders["x-tp"].status is S.FILLED and b.orders["x-sl"].status is S.CANCELED


# ------------------------------------------------------------------ allocation
BTC = instrument_from_symbol(INFO["symbols"][0])
INSTR = {s: instrument_from_symbol(dict(INFO["symbols"][0], symbol=s)) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")}


def snapshot(cash="100", positions=()):
    c = D(cash)
    return PortfolioSnapshot(c, c, c, c, c, positions=tuple(positions))


def cand(symbol, score):
    return Candidate(f"k-{symbol}", symbol, score, TradeProposal(symbol, D(100), D(98), D("0.0026")))


def test_allocator_ranks_and_respects_cash():
    risk = RiskEngine(RiskLimits(risk_per_trade=D("0.2"), max_open_risk=D(1), max_cluster_risk=D(1),
                                 max_stress_loss=D(1)))
    res = allocate([cand("AAAUSDT", 0.1), cand("BBBUSDT", 0.9), cand("CCCUSDT", 0.5)], snapshot("120"), risk,
                   INSTR, entries_allowed=True)
    assert [c.symbol for c, _ in res.approved][0] == "BBBUSDT"
    spent = sum(d.notional for _, d in res.approved)
    assert spent <= D(120) and res.rejected  # cash ran out for the weakest


def test_allocator_skips_held_symbols_and_blocked_state():
    held = OpenPosition("AAAUSDT", D("0.1"), D(100), D(98), D(100))
    res = allocate([cand("AAAUSDT", 1.0)], snapshot(positions=[held]), RiskEngine(), INSTR, entries_allowed=True)
    assert res.reasons["already_in_position"] == 1
    res = allocate([cand("BBBUSDT", 1.0)], snapshot("10000"), RiskEngine(), INSTR, entries_allowed=False)
    assert res.reasons["nara:system_allows_entries"] == 1


def test_capacity_shows_binding_constraint():
    c = capacity(free_cash=D("13.3"), min_notional=D(5), risk_budget=D("0.27"), min_risk_per_trade=D("0.03"),
                 rate_capacity=100, operator_cap=10)
    assert c["max_positions"] == 2 and c["binding"] == "cash"


def test_queue_expiry_and_funnel():
    q = OpportunityQueue()
    q.push(cand("AAAUSDT", 1), T0 + timedelta(minutes=15))
    q.push(cand("BBBUSDT", 1), T0)
    live = q.drain(T0 + timedelta(minutes=1))
    assert [c.symbol for c in live] == ["AAAUSDT"] and q.expired == 1
    f = Funnel(signals=10, approved=2)
    assert f.as_table()["signals"] == 10 and f.as_table()["orders_filled"] == 0
