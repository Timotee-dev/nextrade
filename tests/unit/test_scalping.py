"""Scalping layer: multi-timeframe context, strategies, net-profit exits, capital, re-entry,
approvals, analytics and the scanner."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.domain.instrument import AssetClass, Instrument
from nextrade_core.features.feature_set import with_features
from nextrade_core.scalping import approvals
from nextrade_core.scalping.analytics import report, session_of
from nextrade_core.scalping.capital import check_instrument
from nextrade_core.scalping.mtf import HTF_COLUMNS, add_htf_context
from nextrade_core.scalping.profit import (ExitKind, OpenTrade, ProfitPolicy, breakeven_mid,
                                           net_if_closed_now, should_take_profit)
from nextrade_core.scalping.reentry import ReentryGuard
from nextrade_core.scalping.scanner import scan
from nextrade_core.strategies.base import Side
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, STRATEGIES, make
from tests.helpers import T0, random_walk_frame

COSTS = CostModel(fee_rate=0.002, spread_bps=2.0, slippage_bps=2.0)


def fast_frame(n=6000, seed=5, minutes=5):
    tf = Timeframe.M5 if minutes == 5 else Timeframe.M15
    return with_features(random_walk_frame(n, seed=seed, minutes_per_bar=minutes), tf)


# ------------------------------------------------------------------ multi-timeframe, no look-ahead
def test_htf_columns_present_and_only_closed_hours_are_used():
    raw = random_walk_frame(3000, seed=2, minutes_per_bar=5)
    out = add_htf_context(raw, Timeframe.M5)
    assert set(HTF_COLUMNS) <= set(out.columns)
    # Changing the FUTURE must not change the past context (no look-ahead).
    cut = 1800
    altered = raw.copy()
    altered.iloc[cut:, altered.columns.get_loc("close")] *= 1.5
    altered.iloc[cut:, altered.columns.get_loc("high")] *= 1.5
    out2 = add_htf_context(altered, Timeframe.M5)
    safe = out.index[cut - 13]       # more than one hour (12 bars) before the change
    pd.testing.assert_series_equal(out["htf_trend"].loc[:safe], out2["htf_trend"].loc[:safe])


def test_htf_is_neutral_without_enough_history():
    out = add_htf_context(random_walk_frame(100, minutes_per_bar=5), Timeframe.M5)
    assert (out["htf_trend"] == 0).all()


# ------------------------------------------------------------------ strategies
def test_all_six_scalp_strategies_registered_and_preregistered():
    assert set(SCALP_PREREGISTERED) == {"scalp_momentum", "scalp_trend", "scalp_meanrev", "scalp_breakout",
                                        "scalp_vol", "scalp_short"}
    assert set(SCALP_PREREGISTERED) <= set(STRATEGIES)


@pytest.mark.parametrize("code", sorted(SCALP_PREREGISTERED))
def test_scalp_signals_have_valid_geometry(code):
    f = fast_frame(minutes=15 if code == "scalp_vol" else 5)
    tf = Timeframe.M15 if code == "scalp_vol" else Timeframe.M5
    strat = make(code)
    signals = []
    for seed in (1, 2, 3, 4, 5, 9):          # rare setups: pool several random markets
        signals += strat.generate(fast_frame(n=12000, seed=seed, minutes=15 if code == "scalp_vol" else 5),
                                  "XUSDT", tf)
    assert len(signals) > 5                  # the check below must not be vacuous
    for s in signals:
        assert s.sign * (s.entry - s.stop) > 0 and s.sign * (s.target - s.entry) > 0
        assert s.reward_risk == pytest.approx(strat.params.target_r)
        assert s.max_hold_bars == strat.params.max_hold


def test_short_scalp_only_shorts_and_needs_shorting():
    strat = make("scalp_short")
    assert strat.needs_shorting and strat.side is Side.SHORT
    sides = {s.side for s in strat.generate(fast_frame(n=12000, seed=9), "XUSDT", Timeframe.M5)}
    assert sides <= {Side.SHORT}


def test_scalp_strategies_are_judged_raw_with_stable_halves():
    for code in SCALP_PREREGISTERED:
        s = make(code)
        assert s.judged_raw and s.require_stable_halves and not s.exit_on_invalidation


def test_momentum_needs_the_hourly_trend_to_agree():
    f = fast_frame(n=12000, seed=4)
    base = make("scalp_momentum").generate(f, "X", Timeframe.M5)
    neutral = f.copy()
    neutral["htf_trend"] = 0.0
    assert make("scalp_momentum").generate(neutral, "X", Timeframe.M5) == []
    assert len(base) > 0 and {s.side for s in base} == {Side.LONG, Side.SHORT}
    up_only = f.copy()
    up_only["htf_trend"] = 1.0
    assert {s.side for s in make("scalp_momentum").generate(up_only, "X", Timeframe.M5)} == {Side.LONG}


# ------------------------------------------------------------------ net-profit exits
def long_trade(entry=100.0, qty=10.0, bars=0, peak=0.0):
    fill = COSTS.entry_fill("X", entry, 1)
    return OpenTrade("X", 1, fill, qty, COSTS.fees(fill * qty), bars, peak)


def test_unrealised_profit_is_not_net_profit():
    t = long_trade()
    gross = (100.3 - t.entry_fill) * t.qty
    assert gross > 0
    assert net_if_closed_now(t, 100.3, COSTS) < gross            # costs of getting out and in


def test_breakeven_is_above_entry_for_longs_below_for_shorts():
    t = long_trade()
    assert breakeven_mid(t, COSTS) > t.entry_fill
    fill = COSTS.entry_fill("X", 100.0, -1)
    s = OpenTrade("X", -1, fill, 10.0, COSTS.fees(fill * 10.0))
    assert breakeven_mid(s, COSTS) < fill
    assert net_if_closed_now(t, breakeven_mid(t, COSTS), COSTS) == pytest.approx(0, abs=1e-6)


def test_no_profit_exit_below_minimum_net_even_if_target_hit():
    t = long_trade()
    pol = ProfitPolicy(ExitKind.FIXED, min_net=5.0)
    ok, why = should_take_profit(pol, t, 100.45, COSTS, target=100.4)
    assert not ok and "minimum" in why
    ok, _ = should_take_profit(ProfitPolicy(ExitKind.FIXED, min_net=0.0), t, 101.5, COSTS, target=101.0)
    assert ok


@pytest.mark.parametrize("kind,kw,ok_args,no_args", [
    (ExitKind.PERCENT, dict(pct=0.01), dict(mid=101.5), dict(mid=100.7)),
    (ExitKind.ATR, dict(atr_mult=2.0), dict(mid=101.5, atr=0.5), dict(mid=101.5, atr=1.5)),
    (ExitKind.MOMENTUM, dict(momentum_floor=0.0), dict(mid=101.5, momentum=-0.1), dict(mid=101.5, momentum=0.4)),
    (ExitKind.TIME, dict(max_bars=10), dict(mid=101.5, bars=12), dict(mid=101.5, bars=3)),
])
def test_each_profit_exit_kind(kind, kw, ok_args, no_args):
    pol = ProfitPolicy(kind, **kw)
    for args, expected in ((ok_args, True), (no_args, False)):
        a = dict(args)
        t = long_trade(bars=a.pop("bars", 0))
        mid = a.pop("mid")
        assert should_take_profit(pol, t, mid, COSTS, **a)[0] is expected


def test_trailing_profit_gives_back_only_the_allowed_share():
    pol = ProfitPolicy(ExitKind.TRAILING, give_back=0.35)
    t0 = long_trade()
    peak = net_if_closed_now(t0, 102.0, COSTS)
    near_peak = long_trade(peak=peak)
    assert not should_take_profit(pol, near_peak, 101.9, COSTS)[0]
    assert should_take_profit(pol, near_peak, 101.0, COSTS)[0]


def test_short_take_profit_uses_net_too():
    fill = COSTS.entry_fill("X", 100.0, -1)
    s = OpenTrade("X", -1, fill, 10.0, COSTS.fees(fill * 10.0))
    assert not should_take_profit(ProfitPolicy(ExitKind.PERCENT, pct=0.005), s, 99.9, COSTS)[0]
    assert should_take_profit(ProfitPolicy(ExitKind.PERCENT, pct=0.005), s, 98.5, COSTS)[0]


# ------------------------------------------------------------------ capital feasibility
def spot(symbol="BTCUSDT", min_qty="0.00001", min_notional="1", step="0.00001"):
    return Instrument("gate", symbol, symbol[:-4], "USDT", AssetClass.CRYPTO, D("0.01"), D(step), D(min_qty), D(min_notional))


def test_small_capital_cannot_size_a_forex_standard_lot():
    fx = Instrument("mt5", "EURUSD", "EUR", "USD", AssetClass.FOREX, D("0.00001"), D("0.01"), D("0.01"),
                    D(0), contract_size=D(100000))
    r = check_instrument(fx, capital=D(27), price=D("1.10"), stop_pct=0.0005, target_pct=0.001,
                         risk_fraction=D("0.0025"), costs=CostModel(fee_rate=0, spread_bps=2))
    assert not r.available and any("minimum" in x or "margin" in x for x in r.reasons)


def test_cost_share_of_target_makes_quidax_unsuitable_for_scalping():
    quidax = CostModel(fee_rate=0.0015, spread_bps=40, slippage_bps=5)
    r = check_instrument(spot(), capital=D(100), price=D(90000), stop_pct=0.004, target_pct=0.006,
                         risk_fraction=D("0.01"), costs=quidax)
    assert not r.available and any("round-trip cost" in x for x in r.reasons)


def test_feasible_when_costs_small_and_size_above_minimum():
    r = check_instrument(spot(), capital=D(1000), price=D(90000), stop_pct=0.01, target_pct=0.03,
                         risk_fraction=D("0.01"), costs=COSTS)
    assert r.available and r.qty > 0 and r.notional <= D(1000)


def test_short_needed_but_not_allowed_and_leverage_cap():
    r = check_instrument(spot(), capital=D(1000), price=D(100), stop_pct=0.01, target_pct=0.05,
                         risk_fraction=D("0.01"), costs=COSTS, needs_short=True, can_short=False,
                         leverage=D(5), max_leverage=D(2))
    assert not r.available
    text = " ".join(r.reasons)
    assert "shorting" in text and "leverage" in text


# ------------------------------------------------------------------ re-entry guard
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_no_reentry_during_cooldown_and_after_loss_longer():
    g = ReentryGuard()
    g.record_close("X", "s", 1, 1.0, NOW, NOW - timedelta(minutes=30))
    ok, why = g.check("X", "s", 1, NOW + timedelta(minutes=1), NOW + timedelta(minutes=5))
    assert not ok and "cooling" in why
    assert g.check("X", "s", 1, NOW + timedelta(minutes=1), NOW + timedelta(minutes=16))[0]
    g.record_close("Y", "s", 1, -1.0, NOW, NOW - timedelta(minutes=30))
    assert not g.check("Y", "s", 1, NOW + timedelta(minutes=1), NOW + timedelta(minutes=20))[0]
    assert g.check("Y", "s", 1, NOW + timedelta(minutes=1), NOW + timedelta(minutes=50))[0]


def test_same_old_signal_cannot_reopen_the_trade():
    g = ReentryGuard()
    g.record_close("X", "s", 1, 1.0, NOW, NOW - timedelta(minutes=5))
    ok, why = g.check("X", "s", 1, NOW - timedelta(minutes=5), NOW + timedelta(hours=3))
    assert not ok and "newer" in why


def test_side_flip_waits_longer_and_loss_streak_pauses_strategy():
    g = ReentryGuard()
    g.record_close("X", "s", 1, 1.0, NOW, NOW - timedelta(minutes=30))
    assert not g.check("X", "s", -1, NOW + timedelta(minutes=1), NOW + timedelta(minutes=30))[0]
    assert g.check("X", "s", -1, NOW + timedelta(minutes=1), NOW + timedelta(minutes=61))[0]
    for i, sym in enumerate("ABC"):
        g.record_close(sym, "bad", 1, -1.0, NOW, NOW - timedelta(hours=1))
    ok, why = g.check("Z", "bad", 1, NOW, NOW + timedelta(hours=1))
    assert not ok and "paused" in why
    assert g.check("Z", "bad", 1, NOW, NOW + timedelta(hours=5))[0]


# ------------------------------------------------------------------ approvals and modes
def test_research_places_no_orders_and_live_is_off_by_default(tmp_path):
    assert approvals.require_mode(tmp_path, approvals.Mode.RESEARCH) == []
    with pytest.raises(PermissionError, match="live mode is disabled"):
        approvals.require_mode(tmp_path, approvals.Mode.LIVE, env={})
    with pytest.raises(PermissionError, match="no strategy is approved"):
        approvals.require_mode(tmp_path, approvals.Mode.PAPER)


def test_paper_only_gets_approved_strategies_and_live_needs_live_approval(tmp_path):
    approvals.save(tmp_path, {"scalp_momentum": approvals.Approval("scalp_momentum", True, False, "exp #1", "t")})
    assert approvals.require_mode(tmp_path, approvals.Mode.PAPER) == ["scalp_momentum"]
    with pytest.raises(PermissionError, match="no strategy is approved"):
        approvals.require_mode(tmp_path, approvals.Mode.LIVE, env={"NEXTRADE_LIVE_ENABLED": "yes"})


# ------------------------------------------------------------------ analytics
def trades_frame(n=40, win=0.5, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        t0 = T0 + timedelta(hours=3 * i)
        net = 1.0 if rng.random() < win else -1.2
        rows.append({"strategy": "a" if i % 2 else "b", "symbol": "BTCUSDT" if i % 3 else "ETHUSDT",
                     "opened_at": t0, "closed_at": t0 + timedelta(minutes=20), "net_pnl": net, "fees": 0.1,
                     "risk": 1.0, "regime": "range", "exit_reason": "target" if net > 0 else "stop"})
    return pd.DataFrame(rows)


def test_report_separates_net_gross_and_fees_and_flags_small_samples():
    r = report(trades_frame())
    o = r["overall"]
    assert o["gross_before_fees"] == pytest.approx(o["net_profit"] + o["fees"])
    assert {"by_strategy", "by_symbol", "by_session", "by_regime"} <= set(r)
    small = report(trades_frame(n=10))
    assert not small["overall"]["enough_data"]
    assert small["findings"] == ["no pattern with enough trades to act on"]


def test_findings_name_negative_expectancy_only_with_enough_trades():
    r = report(trades_frame(n=90, win=0.2, seed=3))
    assert any("negative expectancy" in f for f in r["findings"])
    assert report(pd.DataFrame(columns=trades_frame().columns))["findings"] == ["no closed trades yet"]


def test_session_buckets():
    assert [session_of(pd.Timestamp(f"2026-11-20 {h:02d}:00", tz="UTC")) for h in (3, 9, 15, 22)] == \
        ["asia", "london", "new_york", "late"]


# ------------------------------------------------------------------ scanner
def test_scanner_skips_stale_data_and_can_decide_to_wait():
    f = fast_frame(n=3000)
    inst = {"X": spot("XUSDT")}
    strategies = {c: make(c) for c in ("scalp_momentum", "scalp_trend")}
    later = f.index[-1].to_pydatetime() + timedelta(hours=5)
    res = scan({"X": f}, inst, strategies, timeframe=Timeframe.M5, costs=COSTS, now=later, capital=D(1000),
               risk_fraction=D("0.01"))
    assert res.stale == ["X"] and not res.candidates
    fresh = scan({"X": f}, inst, strategies, timeframe=Timeframe.M5, costs=COSTS,
                 now=f.index[-1].to_pydatetime() + timedelta(minutes=5), capital=D(1000), risk_fraction=D("0.01"))
    assert fresh.stale == []          # fresh data is scanned; whether anything qualifies is up to the rules


def test_scanner_rejects_with_reasons_when_costs_eat_the_target():
    f = fast_frame(n=12000, seed=11)
    inst = {"X": spot("XUSDT")}
    heavy = CostModel(fee_rate=0.01, spread_bps=100, slippage_bps=50)
    # force a signal on the last bar by scanning at every historical end point until one appears
    for end in range(len(f) - 1, len(f) - 4000, -1):
        part = f.iloc[: end + 1]
        res = scan({"X": part}, inst, {c: make(c) for c in SCALP_PREREGISTERED}, timeframe=Timeframe.M5,
                   costs=heavy, now=part.index[-1].to_pydatetime() + timedelta(minutes=5), capital=D(1000),
                   risk_fraction=D("0.01"), can_short=True)
        if res.candidates:
            assert not res.accepted
            assert all(c.reasons for c in res.rejected)
            return
    pytest.skip("no signal on this random data")
