"""Net-profit exits, the net-of-cost entry gate and spot short handling: same rule in backtest and trader."""

import asyncio
from decimal import Decimal as D

import pandas as pd
import pytest

from nextrade_core.costs.model import cost_model_for
from nextrade_core.scalping.net_exit import (NetExitConfig, NetProfitExit, backtest_exit_policy, cost_breakdown,
                                             entry_gate, planned_net_frac)
from nextrade_core.strategies.base import Side, Signal
from nextrade_core.strategies.baseline import RandomEntry, RandomEntryParams
from tests.unit.test_trader_daemon import SYMBOLS, TF, build, drive

COSTS = cost_model_for("bybit")           # 0.10%/side fee, 4 bps spread, 3 bps slippage per side
CFG = NetExitConfig(min_net_bps=8, give_back=0.35, min_planned_bps=15)


def test_target_that_only_covers_costs_is_not_a_scalp():
    # round trip costs ~0.31%: a 0.25% gross target loses money after costs and is refused
    assert planned_net_frac(COSTS, "XUSDT", 100.0, 100.25) < 0
    assert entry_gate(COSTS, "XUSDT", 100.0, 100.25, 1, CFG) is not None
    # a 0.60% target nets about 0.29% and passes
    assert planned_net_frac(COSTS, "XUSDT", 100.0, 100.60) > 0.0025
    assert entry_gate(COSTS, "XUSDT", 100.0, 100.60, 1, CFG) is None


def test_positive_gross_is_not_a_win_and_never_triggers_a_profit_exit():
    ex = NetProfitExit(COSTS, CFG)
    fill = COSTS.entry_fill("XUSDT", 100.0)
    qty, fee = 1.0, COSTS.fees(fill)
    # price up 0.20%: gross is positive but exit costs make the net negative
    mid = 100.20
    assert ex.net_now("XUSDT", 1, fill, qty, fee, mid) < 0
    assert ex.reason("k", "XUSDT", 1, fill, qty, fee, mid) is None
    # even after it rose and fell back below the minimum, no profit exit fires
    assert ex.reason("k", "XUSDT", 1, fill, qty, fee, 100.25) is None


def test_trailing_exit_fires_only_in_net_profit_and_after_giving_back():
    ex = NetProfitExit(COSTS, CFG)
    fill = COSTS.entry_fill("XUSDT", 100.0)
    qty, fee = 10.0, COSTS.fees(fill) * 10
    assert ex.reason("k", "XUSDT", 1, fill, qty, fee, 100.80) is None            # net peak well above the minimum
    peak = ex.net_now("XUSDT", 1, fill, qty, fee, 100.80)
    assert peak > 0
    assert ex.reason("k", "XUSDT", 1, fill, qty, fee, 100.70) is None            # small pullback: held
    why = ex.reason("k", "XUSDT", 1, fill, qty, fee, 100.55)                      # gave back more than 35%
    assert why == "net_profit_trail"
    assert ex.net_now("XUSDT", 1, fill, qty, fee, 100.55) > CFG.min_net_bps / 10_000 * fill * qty   # still net positive


def test_backtest_and_trader_use_the_same_rule():
    fill = COSTS.entry_fill("XUSDT", 100.0)

    class P:                                                 # the fields Backtester.Position exposes
        signal = Signal("s", "s-v1", "XUSDT", TF, pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(), Side.LONG,
                        100.0, 99.0, 101.0, 10, "", None)
        sign, qty, entry_fee, bars_held = 1, D(10), D(str(COSTS.fees(fill) * 10)), 3
    P.fill = D(str(fill))
    policy = backtest_exit_policy(COSTS, CFG)
    live = NetProfitExit(COSTS, CFG)
    for px in (100.3, 100.8, 100.9, 100.7, 100.5, 100.4):
        a = policy(P, pd.Series({"close": px}), 0)
        b = live.reason("k", "XUSDT", 1, fill, 10.0, float(P.entry_fee), px, 3)
        assert a == b
        if a:
            break
    assert a == "net_profit_trail"


def test_cost_breakdown_separates_gross_fees_friction_and_net():
    b = cost_breakdown(COSTS, "XUSDT", 1, 10.0, 100.0, 100.5, 0.1, 0.1005)
    assert b["gross"] == pytest.approx(5.0)
    assert b["fees"] == pytest.approx(0.2005)
    assert b["net"] == pytest.approx(5.0 - 0.2005)
    assert b["spread_slippage_est"] > 0


class _ShortOnly:
    """A strategy whose every signal is a short. Spot cannot execute it."""
    code = "shorty"
    judged_raw = True

    def generate(self, f, sym, tf):
        t = f.index[-1]
        c = float(f["close"].iloc[-1])
        return [Signal("shorty", "shorty-v1", sym, tf, t.to_pydatetime(), Side.SHORT, c, c * 1.01, c * 0.97, 8, "", None)]


def test_spot_short_signals_are_skipped_counted_and_never_executed():
    daemon, source, broker, store, clock, frames = build()
    daemon.strategies = {"shorty": _ShortOnly()}
    drive(daemon, source, clock, frames, 30)
    f = daemon.funnel
    assert f["skipped_short_unsupported"] >= 20
    assert f["signals"] == 0 and f["orders_submitted"] == 0 and not store.positions     # not counted as executed
    assert broker.submit_calls == 0


def test_paper_loop_exits_on_net_profit_and_every_such_exit_is_net_positive():
    daemon, source, broker, store, clock, frames = build(rate=0.9, seed=5)
    daemon.costs = COSTS
    daemon.net_exit = NetProfitExit(COSTS, NetExitConfig(min_net_bps=5, give_back=0.3))
    drive(daemon, source, clock, frames, 400)
    closed = [p for p in store.positions.values() if p["status"] == "closed"]
    trail = [p for p in closed if p["exit_reason"] == "net_profit_trail"]
    assert len(closed) >= 5 and daemon.stats["manage_checks"] > 0
    assert trail, "expected at least one net-profit exit in 400 bars of synthetic data"
    assert all(p["realized_pnl"] > 0 for p in trail)          # a net-profit exit is never a net loss
    assert daemon.funnel["positions_closed"] == daemon.funnel["net_winners"] + daemon.funnel["net_losers"]


def test_entry_gate_in_the_trader_rejects_targets_that_cannot_beat_costs():
    daemon, source, broker, store, clock, frames = build(rate=0.9)
    daemon.costs = COSTS
    daemon.net_cfg = NetExitConfig(min_planned_bps=2000)       # impossible: refuse everything
    drive(daemon, source, clock, frames, 60)
    assert daemon.funnel["rejected_by_net_target"] > 0 and daemon.funnel["orders_submitted"] == 0


def test_time_stop_works_for_positions_stored_without_a_signal_object():
    daemon, source, broker, store, clock, frames = build(rate=0.9, seed=2)
    real = store.open_positions
    store.open_positions = lambda run_id: [{k: v for k, v in p.items() if k != "signal_obj"} for p in real(run_id)]
    drive(daemon, source, clock, frames, 300)
    closed = [p for p in store.positions.values() if p["status"] == "closed"]
    assert any(p["exit_reason"] == "time" for p in closed)       # the database store may have no signal: it still times out


def test_random_entry_import_is_available():                      # keeps the test helpers honest
    assert RandomEntry(RandomEntryParams()).code == "random" and SYMBOLS
    asyncio.run(asyncio.sleep(0))
