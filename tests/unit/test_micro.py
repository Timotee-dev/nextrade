"""Quidax microstructure research: price gaps and market-making simulation."""

import pandas as pd
import pytest

from nextrade_core.micro.edge import align_world, gap_summary, gap_trades, simulate_market_making, top_of_book

T = pd.Timestamp("2026-11-12 10:00", tz="UTC")


def book(prices, step="10s"):
    idx = pd.date_range(T, periods=len(prices), freq=step)
    return pd.DataFrame({"bid": [b for b, _ in prices], "ask": [a for _, a in prices]}, index=idx).assign(
        mid=lambda d: (d.bid + d.ask) / 2)


def test_top_of_book_parses_and_drops_crossed_books():
    rows = [{"ts": T, "bids": [["100", "1"]], "asks": [["101", "2"]]},
            {"ts": T + pd.Timedelta("10s"), "bids": [["102", "1"]], "asks": [["101", "1"]]}]
    df = top_of_book(rows)
    assert len(df) == 1 and df.iloc[0].mid == 100.5


def test_gap_is_measured_against_world_price_in_naira():
    qx = book([(99_000, 101_000)] * 5)                 # mid 100,000 NGN
    world = book([(66.0, 68.0)] * 5)                   # 67 USDT
    usdt = book([(1490, 1510)] * 5)                    # 1,500 NGN per USDT -> world 100,500 NGN
    j = align_world(qx, world, usdt)
    assert j["gap_bps"].iloc[0] == pytest.approx((100_000 / 100_500 - 1) * 1e4)
    s = gap_summary(j, 0.0015)
    assert s["spread_median_bps"] == pytest.approx(200.0) and s["round_trip_cost_median_bps"] == pytest.approx(230.0)


def test_gap_trade_buys_cheap_and_exits_when_closed():
    idx = pd.date_range(T, periods=4, freq="1min")
    df = pd.DataFrame({"bid": [99.0, 99.5, 101.0, 101.0], "ask": [99.2, 99.7, 101.2, 101.2],
                       "gap_bps": [-200.0, -100.0, 10.0, 10.0]}, index=idx)
    t = gap_trades(df, 150, taker_fee=0.0)
    assert len(t) == 1 and t[0].reason == "closed"
    assert t[0].net_bps == pytest.approx((101.0 / 99.2 - 1) * 1e4)


def test_market_maker_earns_spread_when_price_trades_through_both_sides():
    # spread 10 ticks; quotes 101 / 109. Next snapshot the ask drops to 100 (we buy at 101); that
    # book is too tight to quote; then the bid rises to 110 and our 109 ask sells.
    b = book([(100, 110), (99, 100), (100, 110), (110, 120)])
    r = simulate_market_making(b, tick=1, order_ngn=1010, max_inventory_ngn=5000, maker_fee=0.0)
    assert r.bid_fills == 1 and r.ask_fills == 1
    assert r.spread_earned_ngn == pytest.approx(1010 / 109 * 8, abs=0.01)      # 8 NGN on each unit sold


def test_after_a_drop_the_market_maker_requotes_and_can_sell_below_cost():
    b = book([(100, 110), (95, 100), (100, 110)])        # buys at 101, then quotes 96/99 and sells at 99
    r = simulate_market_making(b, tick=1, order_ngn=1010, maker_fee=0.0)
    assert r.ask_fills == 1 and r.spread_earned_ngn < 0


def test_market_maker_is_hurt_by_a_falling_price():
    b = book([(100, 110), (90, 100), (85, 95), (80, 90), (70, 80), (60, 70)])
    r = simulate_market_making(b, tick=1, order_ngn=1000, max_inventory_ngn=3000, maker_fee=0.0015)
    assert r.ask_fills == 0 and r.inventory_pnl_ngn < 0 and r.net_ngn < 0
    assert r.max_inventory_ngn <= 3000 * 1.2                        # capped (marked at the falling mid)


def test_market_maker_skips_tight_spreads_and_cannot_sell_what_it_does_not_hold():
    b = book([(100, 101), (100, 101), (102, 104), (110, 120)])
    r = simulate_market_making(b, tick=1, order_ngn=1000, maker_fee=0.0)
    assert r.bid_fills == 0 and r.ask_fills == 0
