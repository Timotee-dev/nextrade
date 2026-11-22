"""High-frequency paper scalper: websocket parsing and guard, paper exchange fills, strategies, engine behaviour."""
import json

import pytest

from adapters.bybit.ws import BookState, ForbiddenWsOperation, check_op, parse, subscribe_messages
from nextrade_core.hft.engine import HftConfig, HftEngine
from nextrade_core.hft.events import Quote, Trade
from nextrade_core.hft.exchange import DuplicateOrder, InstrumentRules, PaperExchange
from nextrade_core.hft.market import Market, SymbolState
from nextrade_core.hft.strategies import make_all

T0 = 1_700_000_000.0


# ---------------------------------------------------------------- websocket
def test_ws_only_allows_subscribe_and_ping():
    check_op({"op": "ping"})
    check_op({"op": "subscribe", "args": ["orderbook.1.BTCUSDT"]})
    for bad in ("order.create", "order.cancel", "withdraw", "auth", ""):
        with pytest.raises(ForbiddenWsOperation):
            check_op({"op": bad})


def test_subscribe_batches_of_ten():
    msgs = subscribe_messages([f"C{i}USDT" for i in range(12)])
    assert all(len(m["args"]) <= 10 for m in msgs) and sum(len(m["args"]) for m in msgs) == 24


def test_book_snapshot_delta_and_removal():
    b = BookState()
    q = b.apply("BTCUSDT", "snapshot", {"b": [["100", "2"]], "a": [["101", "3"]]}, now=T0)
    assert (q.bid, q.ask, q.bid_sz, q.ask_sz) == (100.0, 101.0, 2.0, 3.0)
    q = b.apply("BTCUSDT", "delta", {"b": [["100.5", "1"]], "a": []}, now=T0 + 1)
    assert q.bid == 100.5
    assert b.apply("BTCUSDT", "delta", {"b": [["100.5", "0"], ["100", "0"]], "a": []}, now=T0 + 2) is None   # empty side


def test_parse_trade_message():
    raw = json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{"T": 1700000000123, "s": "BTCUSDT", "S": "Buy", "v": "0.5", "p": "100"}]})
    evs = parse(raw, BookState(), now=T0)
    assert len(evs) == 1 and isinstance(evs[0], Trade) and evs[0].buy and evs[0].price == 100.0


# ---------------------------------------------------------------- paper exchange
def book(bid=100.0, ask=100.1, sz=10.0, ts=T0, sym="X"):
    st = SymbolState(sym)
    st.on_quote(Quote(sym, ts, bid, ask, sz, sz))
    return st


def test_market_buy_waits_for_latency_and_fills_at_ask_plus_slippage():
    ex = PaperExchange(cash=1000, rules={"X": InstrumentRules(0.001, 0.001, 1.0)})
    st = book()
    ex.market_buy("b1", "X", 1.0, T0)
    assert ex.process(st, T0 + 0.05) == []                      # not there yet
    (f,) = ex.process(st, T0 + 0.2)
    assert f.price == pytest.approx(100.1 * 1.0001) and f.fee == pytest.approx(f.price * 0.001)
    assert ex.holdings["X"] == 1.0 and ex.cash == pytest.approx(1000 - f.price - f.fee)


def test_fill_is_capped_by_displayed_size_and_stale_book_rejects():
    ex = PaperExchange(cash=1000)
    st = book(sz=0.4)
    ex.market_buy("b1", "X", 1.0, T0)
    (f,) = ex.process(st, T0 + 0.2)
    assert f.qty == pytest.approx(0.4)
    ex.market_buy("b2", "X", 0.1, T0 + 5)
    assert ex.process(st, T0 + 5.2) == [] and ex.orders["b2"].status == "rejected"


def test_limit_sell_needs_trade_through_or_bid_at_price_and_pays_maker_fee():
    ex = PaperExchange(cash=1000, holdings={"X": 1.0})
    st = book(bid=100, ask=100.1)
    ex.limit_sell("s1", "X", 1.0, 100.5, T0)
    assert ex.process(st, T0 + 0.3, Trade("X", T0 + 0.3, 100.5, 5, True)) == []          # touching is not through
    st.on_quote(Quote("X", T0 + 0.4, 100.2, 100.3, 5, 5))
    (f,) = ex.process(st, T0 + 0.4, Trade("X", T0 + 0.4, 100.6, 5, True))
    assert f.price == 100.5 and f.kind == "limit" and f.fee == pytest.approx(100.5 * 0.001)


def test_cancel_takes_latency_so_order_can_still_fill():
    ex = PaperExchange(cash=1000, holdings={"X": 1.0})
    st = book(bid=101, ask=101.1)
    ex.limit_sell("s1", "X", 1.0, 100.0, T0 - 1)
    ex.process(book(bid=99, ask=99.1, ts=T0 - 0.8), T0 - 0.8)
    ex.cancel("s1", T0)
    st = book(bid=101, ask=101.1, ts=T0 + 0.05)
    assert len(ex.process(st, T0 + 0.05)) == 1                  # filled before the cancel arrived


def test_duplicate_client_order_id_rejected():
    ex = PaperExchange(cash=1000)
    ex.market_buy("same", "X", 1, T0)
    with pytest.raises(DuplicateOrder):
        ex.market_buy("same", "X", 1, T0)


def test_insufficient_cash_rejected():
    ex = PaperExchange(cash=10)
    st = book()
    ex.market_buy("b", "X", 1.0, T0)
    assert ex.process(st, T0 + 0.2) == [] and ex.orders["b"].status == "rejected"


# ---------------------------------------------------------------- strategies
def feed_market(m, events):
    for e in events:
        m.on_event(e)


def test_no_strategy_fires_on_a_flat_quiet_market():
    m = Market(["X"])
    st = m.state["X"]
    strats = list(make_all().values())
    fired = 0
    for i in range(1200):
        t = T0 + i * 0.1
        m.on_event(Quote("X", t, 100.0, 100.02, 5, 5))
        if i % 5 == 0:
            m.on_event(Trade("X", t, 100.0 if i % 2 else 100.02, 1, bool(i % 2)))
        if i > 700:
            fired += sum(1 for s in strats if s.evaluate(st, t, 25.0))
    assert fired == 0


def test_flow_burst_fires_on_one_sided_buying_with_rising_price():
    m = Market(["X"])
    st = m.state["X"]
    s = make_all()["flow_burst"]
    sig = None
    p = 100.0
    for i in range(400):
        t = T0 + i * 0.1
        if i > 300:
            p *= 1.00005
        m.on_event(Quote("X", t, p * 0.99995, p * 1.00005, 300, 100))
        m.on_event(Trade("X", t, p, 1, True))
        got = s.evaluate(st, t, 25.0)
        sig = sig or got
    assert sig is not None and sig.target_bps > 25.0 and sig.max_hold_s > 0


# ---------------------------------------------------------------- engine
def make_engine(cash=27.0, cfg=None, sink=None, strategies=None):
    ex = PaperExchange(cash=cash, rules={s: InstrumentRules(0.0001, 0.0001, 1.0) for s in ("A", "B", "C")})
    return HftEngine(cfg or HftConfig(), ex, Market(["A", "B", "C"]), strategies or make_all(), sink=sink)


class Always:
    """A test strategy that signals whenever asked, so engine mechanics can be tested without market structure."""
    name = "always"

    def __init__(self, target=40.0, stop=20.0, hold=60.0):
        self.args = (target, stop, hold)

    def evaluate(self, st, now, cost_bps):
        from nextrade_core.hft.strategies import HSignal
        return HSignal(self.name, st.symbol, now, st.mid, self.args[0], self.args[1], self.args[2], 8.0, "test")


def prime(e, syms=("A", "B", "C"), t=T0, px=100.0, spread=0.02):
    for s in syms:
        e.on_event(Quote(s, t, px - spread / 2, px + spread / 2, 500, 500))


def drive(e, syms, seconds, px_fn, t0=T0, dt=0.1):
    t = t0
    for i in range(int(seconds / dt)):
        t = t0 + i * dt
        for s in syms:
            p = px_fn(s, t - t0)
            e.on_event(Quote(s, t, p - 0.01, p + 0.01, 500, 500))
        e.on_timer(t)
    return t


def test_opens_several_positions_at_once_without_waiting():
    e = make_engine(strategies={"always": Always(target=500.0)})
    drive(e, "ABC", 3, lambda s, t: 100.0)
    assert len(e.positions) == 3 and e.max_open == 3


def test_target_closes_with_net_profit_and_reenters():
    recs = []
    e = make_engine(sink=recs.append, strategies={"always": Always(target=40.0)}, cfg=HftConfig(cooldown_s=1.0))
    drive(e, "A", 40, lambda s, t: 100.0 * (1 + 0.0012 * (t % 6)))
    closes = [r for r in recs if r["type"] == "close"]
    assert len(closes) >= 2, "it must close and then open again"
    assert any(r["reason"] == "target" and r["net"] > 0 for r in closes)
    assert all(abs(r["gross"] - r["fees"] - r["net"]) < 1e-9 for r in closes)


def test_stop_exit_checked_on_every_tick():
    recs = []
    e = make_engine(sink=recs.append, strategies={"always": Always(target=500.0, stop=15.0)})
    drive(e, "A", 2, lambda s, t: 100.0)
    assert "A" in e.positions
    drive(e, "A", 3, lambda s, t: 99.5, t0=T0 + 2)               # -50 bps through the 15 bps stop
    closes = [r for r in recs if r["type"] == "close"]
    assert closes and closes[0]["reason"] == "stop" and closes[0]["net"] < 0


def test_time_stop_closes_a_quiet_position():
    recs = []
    e = make_engine(sink=recs.append, strategies={"always": Always(target=500.0, stop=500.0, hold=5.0)})
    drive(e, "A", 12, lambda s, t: 100.0)
    assert [r["reason"] for r in recs if r["type"] == "close"][0] in ("time", "time_profit")


def test_net_requirement_blocks_targets_that_do_not_beat_costs():
    e = make_engine(strategies={"always": Always(target=25.0)})      # costs are ~20 bps fees + spread + slippage
    drive(e, "A", 2, lambda s, t: 100.0)
    assert not e.positions and e.rejects["net_target_below_costs"] > 0


def test_max_positions_and_exposure_are_enforced():
    e = make_engine(cfg=HftConfig(max_positions=2), strategies={"always": Always(target=500.0)})
    drive(e, "ABC", 3, lambda s, t: 100.0)
    assert len(e.positions) == 2 and e.rejects["max_positions"] > 0
    e2 = make_engine(cfg=HftConfig(max_exposure=0.10), strategies={"always": Always(target=500.0)})
    drive(e2, "ABC", 3, lambda s, t: 100.0)
    assert len(e2.positions) <= 1 and e2.rejects["exposure_limit"] > 0


def test_drawdown_halt_flattens_and_latches():
    recs = []
    e = make_engine(cfg=HftConfig(max_drawdown=0.001, pos_frac=0.4, risk_per_trade=0.5), sink=recs.append,
                    strategies={"always": Always(target=5000.0, stop=5000.0, hold=999)})
    t = drive(e, "A", 2, lambda s, t: 100.0)
    drive(e, "A", 3, lambda s, t: 98.0, t0=t)
    assert e.halt == "drawdown" and not e.positions and any(r["type"] == "halt" for r in recs)
    n = e.stats["entries"]
    drive(e, "A", 3, lambda s, t: 100.0, t0=t + 4)
    assert e.stats["entries"] == n                               # latched: no new entries


def test_daily_loss_pauses_entries():
    recs = []
    e = make_engine(cfg=HftConfig(daily_loss=0.001, max_drawdown=0.5, pos_frac=0.4, risk_per_trade=0.5), sink=recs.append,
                    strategies={"always": Always(target=5000.0, stop=5000.0, hold=999)})
    t = drive(e, "A", 2, lambda s, t: 100.0)
    drive(e, "A", 3, lambda s, t: 99.0, t0=t)
    assert e.pause_day and any(r["type"] == "pause" for r in recs)


def test_loss_streak_pauses_that_strategy():
    e = make_engine(cfg=HftConfig(streak_losses=2, cooldown_s=0.5, cooldown_loss_s=0.5, resignal_s=0.2),
                    strategies={"always": Always(target=500.0, stop=10.0, hold=999)})
    drive(e, "A", 40, lambda s, tt: 100.0 * (1 - 0.003 * tt))        # keeps falling: every entry is stopped out
    assert e.rejects["strategy_paused_after_losses"] > 0


def test_snapshot_restore_keeps_cash_and_open_positions_and_manages_them():
    e = make_engine(strategies={"always": Always(target=500.0, stop=500.0, hold=20.0)})
    t = drive(e, "A", 2, lambda s, tt: 100.0)
    assert e.positions
    snap = json.loads(json.dumps(e.snapshot()))
    e2 = make_engine(strategies={})
    e2.restore(snap)
    assert set(e2.positions) == {"A"} and e2.ex.holdings["A"] > 0 and e2.ex.cash == pytest.approx(e.ex.cash)
    recs = []
    e2.sink = recs.append
    drive(e2, "A", 25, lambda s, tt: 100.0, t0=t + 1)
    assert any(r["type"] == "close" for r in recs), "restored positions must still be managed and closed"


def test_scanning_covers_all_markets_and_funnel_counts_add_up():
    e = make_engine(strategies={"always": Always(target=500.0)})
    drive(e, "ABC", 2, lambda s, t: 100.0)
    s = e.summary()
    assert s["scans"] >= 3 and s["signals"] >= 3 and s["entries"] == 3


def test_small_account_is_lifted_to_the_exchange_minimum_order_size():
    ex = PaperExchange(cash=27.0, rules={s: InstrumentRules(0.0001, 0.0001, 5.0) for s in ("A", "B", "C")})
    e = HftEngine(HftConfig(), ex, Market(["A", "B", "C"]), {"always": Always(target=500.0)})
    drive(e, "ABC", 3, lambda s, t: 100.0)
    assert e.stats["entries"] >= 1 and not e.rejects["below_min_notional"]
    for p in e.positions.values():
        assert p.entry_fill * p.entry_qty >= 5.0
    assert len(e.positions) <= 5                                 # 27 USDT cannot hold more 5 USDT orders than this


def test_minimum_order_larger_than_a_quarter_of_equity_is_refused():
    ex = PaperExchange(cash=10.0, rules={"A": InstrumentRules(0.0001, 0.0001, 5.0)})
    e = HftEngine(HftConfig(), ex, Market(["A"]), {"always": Always(target=500.0)})
    drive(e, "A", 2, lambda s, t: 100.0)
    assert not e.positions and e.rejects["below_min_notional"] > 0


def test_ws_rotates_to_the_next_host_after_a_failed_connection():
    import asyncio
    from adapters.bybit.ws import BybitWs
    tried = []

    def connect(url, **kw):
        tried.append(url)
        raise OSError("getaddrinfo failed")

    stop = asyncio.Event()

    async def fast(s):
        if len(tried) >= 3:
            stop.set()
        await asyncio.sleep(0)

    w = BybitWs(["X"], lambda e: None, connect=connect, sleep=fast)
    asyncio.run(w.run(stop))
    assert len(set(tried)) == 2 and tried[0] != tried[1] and tried[0] == tried[2]
