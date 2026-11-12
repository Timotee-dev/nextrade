"""Forex mechanics (batch F2): shorts, leverage/margin, contract size, JPY conversion."""

from decimal import Decimal as D

import pandas as pd
import pytest

from nextrade_core.backtest.engine import Backtester, conversion_for
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.domain.instrument import AssetClass, Instrument
from nextrade_core.ml.labels import ExitReason, simulate_exit
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits
from nextrade_core.nara.portfolio import OpenPosition, stress_loss
from nextrade_core.nara.sizing import size_position
from nextrade_core.npei.exits import ExitConfig, assess
from nextrade_core.strategies.base import Side, Signal
from tests.helpers import T0

TF = Timeframe.H1
FREE = CostModel(fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0)
LOOSE = RiskEngine(RiskLimits(risk_per_trade=D("0.01"), max_open_risk=D("0.5"), max_cluster_risk=D("0.5"),
                              max_stress_loss=D("1"), daily_loss=D("0.9"), weekly_loss=D("0.9"),
                              max_drawdown=D("0.95"), max_consecutive_losses=1000))


def fx_inst(symbol="EURUSD", contract="100000", step="0.01"):
    base, quote = symbol[:3], symbol[3:]
    tick = D("0.001") if quote == "JPY" else D("0.00001")
    return Instrument("mt5", symbol, base, quote, AssetClass.FOREX, tick, D(step), D(step), D(0),
                      D(200), D(contract))


def frame(rows):
    idx = pd.date_range(T0, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def short(at=0, entry=1.1000, stop=1.1015, target=1.0970, symbol="EURUSD", hold=10):
    return Signal("fx", "fx-v1", symbol, TF, T0 + TF.delta * at, Side.SHORT, entry, stop, target, hold,
                  "", None, {})


# ------------------------------------------------------------------ sizing
def test_short_sizing_with_leverage():
    r = size_position(equity=D(1000), risk_fraction=D("0.01"), entry=D("1.1000"), stop=D("1.1015"),
                      instrument=fx_inst(), cost_fraction=D(0), free_cash=D(1000), sign=-1, leverage=D(200))
    assert r.ok and r.qty == D("0.06")                         # $10 / (15 pips x $10/pip/lot) = 0.066
    assert r.risk_amount == D("9.0000") and r.notional == D("6600.00000")


def test_short_rejects_stop_below_entry_and_spot_cash_cannot_carry_forex():
    bad = size_position(equity=D(1000), risk_fraction=D("0.01"), entry=D("1.1"), stop=D("1.09"),
                        instrument=fx_inst(), cost_fraction=D(0), free_cash=D(1000), sign=-1, leverage=D(200))
    assert not bad.ok and "above entry" in bad.reject_reason
    spot = size_position(equity=D(1000), risk_fraction=D("0.01"), entry=D("1.1000"), stop=D("1.0985"),
                         instrument=fx_inst(), cost_fraction=D(0), free_cash=D(1000))
    assert not spot.ok and "cash" in spot.reject_reason        # 0.01 lot = $1,100 > $1,000 without leverage


def test_fifteen_dollars_cannot_risk_one_percent_on_a_standard_lot():
    r = size_position(equity=D(15), risk_fraction=D("0.01"), entry=D("1.1000"), stop=D("1.0985"),
                      instrument=fx_inst(), cost_fraction=D(0), free_cash=D(15), leverage=D(200))
    assert not r.ok and "above the budget 0.15" in r.reject_reason      # 0.01 lot risks $1.50
    cent = size_position(equity=D(15), risk_fraction=D("0.01"), entry=D("1.1000"), stop=D("1.0985"),
                         instrument=fx_inst(contract="1000"), cost_fraction=D(0), free_cash=D(15),
                         leverage=D(200))
    assert cent.ok and cent.risk_amount <= D("0.15")                     # a cent-size contract fits


# ------------------------------------------------------------------ labels + exits
def test_short_label_target_and_stop():
    f = frame([[1.1000, 1.1005, 1.0995, 1.1000], [1.1000, 1.1003, 1.0990, 1.0992],
               [1.0992, 1.0995, 1.0965, 1.0968], [1.0968, 1.0970, 1.0960, 1.0962]])
    out = simulate_exit(f, short(), FREE)
    assert out.reason is ExitReason.TARGET and out.r_net == pytest.approx(2.0)
    f2 = frame([[1.1000, 1.1005, 1.0995, 1.1000], [1.1000, 1.1020, 1.0995, 1.1018], [1.1018] * 4])
    out2 = simulate_exit(f2, short(), FREE)
    assert out2.reason is ExitReason.STOP and out2.r_net == pytest.approx(-1.0)


def test_short_trailing_stop_only_moves_down():
    s = short()
    bar = pd.Series({"close": 1.0960, "atr": 0.0005})
    a = assess(s, _Never(), fill=1.1000, current_stop=1.1015, bars_held=3, bar=bar,
               config=ExitConfig(trail_after_r=1.0, trail_atr=2.0))
    assert a.r_now == pytest.approx(0.0040 / 0.0015) and a.new_stop == pytest.approx(1.0970)


class _Never:
    def is_invalidated(self, signal, bar):
        return False


# ------------------------------------------------------------------ backtester
def run(frames, insts, signals, cash=D(1000), lev=D(200), costs=FREE):
    return Backtester(frames, insts, costs, LOOSE, cash, leverage=lev).run(signals)


def test_short_trade_in_a_margin_account():
    rows = [[1.1000, 1.1005, 1.0995, 1.1000], [1.1000, 1.1003, 1.0990, 1.0992],
            [1.0992, 1.0995, 1.0965, 1.0968], [1.0968, 1.0970, 1.0960, 1.0962]]
    res = run({"EURUSD": frame(rows)}, {"EURUSD": fx_inst()}, [short()])
    t = res.trades.iloc[0]
    assert t.side == "short" and t.reason == "target" and t.qty == D("0.06")
    assert t.pnl == D("18.0000000000") and t.r_net == pytest.approx(2.0)   # 30 pips x 0.06 lot x $10
    assert res.equity.iloc[-1] == pytest.approx(1018.0)                     # cash moved only by the P&L


def test_jpy_profit_is_converted_to_dollars():
    rows = [[150.00, 150.05, 149.95, 150.00], [150.00, 150.02, 149.80, 149.85],
            [149.85, 149.90, 149.65, 149.70], [149.70] * 4]
    s = short(entry=150.00, stop=150.15, target=149.70, symbol="USDJPY")
    res = run({"USDJPY": frame(rows)}, {"USDJPY": fx_inst("USDJPY")}, [s])
    t = res.trades.iloc[0]
    # risk $10 -> 0.15 JPY x 100,000 x qty / ~150 JPY per $; profit 0.30 JPY per unit = 2R in dollars
    assert t.reason == "target" and t.r_net == pytest.approx(2.0, rel=0.01)
    assert float(t.risk) == pytest.approx(10.0, rel=0.1) and float(t.pnl) == pytest.approx(2 * float(t.risk), rel=0.01)


def test_cross_pair_conversion_and_missing_pair():
    frames = {"USDJPY": frame([[150] * 4]), "GBPUSD": frame([[1.3] * 4])}
    assert conversion_for("JPY", frames) == ("USDJPY", True)
    assert conversion_for("GBP", frames) == ("GBPUSD", False)
    assert conversion_for("USD", frames) is None
    with pytest.raises(ValueError, match="CHF"):
        conversion_for("CHF", frames)


def test_spot_account_refuses_shorts():
    from tests.unit.test_backtest import inst as crypto_inst
    rows = [[100, 100.5, 99.5, 100]] * 3
    s = Signal("t", "t-v1", "BTCUSDT", TF, T0, Side.SHORT, 100.0, 102.0, 96.0, 5, "", None, {})
    res = Backtester({"BTCUSDT": frame(rows)}, {"BTCUSDT": crypto_inst()}, FREE, LOOSE, D(1000)).run([s])
    assert res.rejections["skip:short_needs_margin_account"] == 1 and len(res.trades) == 0


# ------------------------------------------------------------------ stress
def test_forex_stress_uses_a_forex_shock_and_both_directions():
    long_ = OpenPosition("EURUSD", D("0.06"), D("1.1"), D("1.0985"), D("1.1"), "forex", sign=1, contract=D(100000))
    short_ = OpenPosition("GBPUSD", D("0.06"), D("1.3"), D("1.3015"), D("1.3"), "forex", sign=-1, contract=D(100000))
    loss = stress_loss([long_, short_])
    long_loss = (D("1.1") - D("1.1") * D("0.99") * D("0.9995")) * D(6000)       # 1% gap below, 0.05% slip
    short_loss = (D("1.3") * D("1.01") * D("1.0005") - D("1.3")) * D(6000)      # 1% gap above
    assert loss == pytest.approx(long_loss + short_loss)
    assert float(loss) == pytest.approx(151.21, abs=0.01)


# ------------------------------------------------------------------ pre-registered strategies (ADR 0007)
def _fx_frame(n=2400, minutes=60, seed=3, vol=1.0):
    """Random walk rescaled to EURUSD-like levels; `vol` < 1 shrinks moves toward forex size."""
    from nextrade_core.features.feature_set import with_features
    from tests.helpers import random_walk_frame
    raw = random_walk_frame(n, seed=seed, minutes_per_bar=minutes)
    px = raw[["open", "high", "low", "close"]] / raw["close"].iloc[0]
    raw[["open", "high", "low", "close"]] = 1.1 * (1 + (px - 1) * vol)
    return with_features(raw, Timeframe.H1 if minutes == 60 else Timeframe.M15)


@pytest.mark.parametrize("code,n", [("fx_trend", 2400), ("fx_meanrev", 8000)])
def test_hourly_forex_strategies_trade_both_ways(code, n):
    from nextrade_core.strategies.registry import make
    f = _fx_frame(n=n)
    sigs = make(code).generate(f, "EURUSD", Timeframe.H1)
    sides = {s.side for s in sigs}
    assert sides == {Side.LONG, Side.SHORT}
    for s in sigs:
        assert s.sign * (s.entry - s.stop) > 0 and s.sign * (s.target - s.entry) > 0
        assert s.reward_risk == pytest.approx(make(code).params.target_r)


def test_london_breakout_one_trade_per_day_inside_the_window():
    from nextrade_core.strategies.registry import make
    f = _fx_frame(n=96 * 60, minutes=15, seed=11, vol=0.25)
    sigs = make("fx_london").generate(f, "EURUSD", Timeframe.M15)
    assert sigs and {s.side for s in sigs} == {Side.LONG, Side.SHORT}
    days = [s.bar_time.date() for s in sigs]
    assert len(days) == len(set(days))                                  # at most one per day
    assert all(7 <= s.bar_time.hour < 10 for s in sigs)
    for s in sigs:                                                      # stop at the far side of the range
        day = f.loc[f.index.normalize() == pd.Timestamp(s.bar_time).normalize()]
        night = day[day.index.hour < 7]
        far = night["low"].min() if s.side is Side.LONG else night["high"].max()
        assert s.stop == pytest.approx(far)


def test_random_baseline_for_two_sided_strategy_draws_sides_and_keeps_distances():
    from nextrade_core.strategies.baseline import RandomEntry
    from nextrade_core.strategies.registry import make
    f = _fx_frame()
    tmpl = make("fx_trend")
    base = RandomEntry.matching(tmpl, probability=0.05).generate(f, "EURUSD", Timeframe.H1)
    assert len(base) > 50
    longs = sum(s.side is Side.LONG for s in base)
    assert 0.35 < longs / len(base) < 0.65
    for s in base[:20]:
        atr = f.loc[pd.Timestamp(s.bar_time), "atr"]
        assert abs(s.entry - s.stop) == pytest.approx(2.0 * atr) and s.reward_risk == pytest.approx(3.0)


def test_gate_requires_both_halves_positive():
    from nextrade_core.backtest.research import decision_gate
    t = pd.DataFrame({"r_net": [1.0] * 60 + [-0.2] * 60,
                      "exit_time": pd.date_range(T0, periods=120, freq="1D", tz="UTC")})
    b = pd.DataFrame({"r_net": [-0.1] * 50})
    g = decision_gate(t, b, min_trades=100, stable_halves=True)
    assert not g["pass"] and "both halves" in g["reasons"][-1] and g["halves_mean_r"] == pytest.approx((1.0, -0.2))
    assert decision_gate(t, b, min_trades=100)["pass"]                  # old rule alone would pass


def test_timeframe_inferred_despite_weekend_gap_at_the_start():
    from nextrade_core.backtest.research import _tf
    idx = pd.DatetimeIndex([pd.Timestamp("2021-11-14 22:00", tz="UTC")]
                           + list(pd.date_range("2021-11-15 20:00", periods=50, freq="1h", tz="UTC")))
    assert _tf(pd.DataFrame({"close": 1.0}, index=idx)) is Timeframe.H1


# ------------------------------------------------------------------ F5 strategies (ADR 0008)
def test_liquidity_sweep_short_after_false_break_of_yesterdays_high():
    from nextrade_core.strategies.registry import make
    f = _fx_frame(n=2400)
    sigs = make("fx_sweep").generate(f, "EURUSD", Timeframe.H1)
    assert {s.side for s in sigs} == {Side.LONG, Side.SHORT}
    day = f.index.normalize()
    prev_high = f.groupby(day)["high"].max().shift(1)
    for s in [x for x in sigs if x.side is Side.SHORT][:10]:
        bar = f.loc[pd.Timestamp(s.bar_time)]
        ph = prev_high[pd.Timestamp(s.bar_time).normalize()]
        assert bar["high"] > ph > bar["close"] and s.stop > bar["high"]


def _daily_frames(n=900, seeds=(1, 2, 3, 4, 5, 6)):
    from nextrade_core.features.feature_set import with_features
    from tests.helpers import random_walk_frame
    out = {}
    for k, sd in enumerate(seeds):
        raw = random_walk_frame(n, seed=sd, minutes_per_bar=1440)
        out[f"P{k}USD"] = with_features(raw, Timeframe.D1)
    return out


def test_tsmom_enters_once_a_month_in_the_trend_direction():
    from nextrade_core.strategies.registry import make
    f = _daily_frames()["P0USD"]
    sigs = make("fx_tsmom").generate(f, "P0USD", Timeframe.D1)
    months = [(s.bar_time.year, s.bar_time.month) for s in sigs]
    assert len(months) == len(set(months)) > 20
    for s in sigs:
        i = f.index.get_loc(pd.Timestamp(s.bar_time))
        rose = f["close"].iloc[i] > f["close"].iloc[i - 126]
        assert (s.side is Side.LONG) == rose and s.max_hold_bars == 21


def test_xsmom_buys_two_strongest_and_sells_two_weakest_each_month():
    from nextrade_core.strategies.registry import make
    strat = make("fx_xsmom")
    frames = strat.prepare_frames(_daily_frames())
    sigs = [s for sym, f in frames.items() for s in strat.generate(f, sym, Timeframe.D1)]
    by_month = {}
    for s in sigs:
        by_month.setdefault(s.bar_time, []).append(s)
    full = [v for v in by_month.values() if len(v) == 4]
    assert len(full) > 15
    for group in full:
        assert sum(s.side is Side.LONG for s in group) == 2
        t = pd.Timestamp(group[0].bar_time)
        moms = {sym: frames[sym].loc[t, "xs_mom"] for sym in frames}
        ranked = sorted(moms, key=moms.get, reverse=True)
        assert {s.symbol for s in group if s.side is Side.LONG} == set(ranked[:2])
        assert {s.symbol for s in group if s.side is Side.SHORT} == set(ranked[-2:])


def test_same_bar_forex_batch_uses_margin_and_counts_full_risk():
    """Regression: the allocator spent the FULL notional per approved forex trade (spot logic), so
    one trade looked like it used the whole account; and it recorded batch-mates with contract
    size 1 and no direction, understating their risk ~100,000x for the cluster/open-risk limits."""
    from nextrade_core.nara.engine import TradeProposal
    from nextrade_core.nara.portfolio import PortfolioSnapshot
    from nextrade_core.nctpi.allocation import Candidate, allocate
    insts = {s: fx_inst(s) for s in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "EURGBP", "USDCAD")}
    insts["EURGBP"] = fx_inst("EURUSD")
    snap = PortfolioSnapshot(D(10000), D(10000), D(10000), D(10000), D(10000), clusters={s: "forex" for s in insts})
    risk = RiskEngine(RiskLimits(risk_per_trade=D("0.01"), max_open_risk=D("0.08"), max_cluster_risk=D("0.06"),
                                 max_stress_loss=D("1")))
    cands = [Candidate(f"k{i}", s, 1.0, TradeProposal(s, D("1.1000"), D("1.0800"), D(0), asset_class="forex",
                                                      sign=1, leverage=D(200)))
             for i, s in enumerate(insts)]
    out = allocate(cands, snap, risk, insts, entries_allowed=True)
    assert len(out.approved) == 4                                  # 4 x (1% + 25% gap) = 5% <= 6%; a 5th breaks it
    assert out.reasons["nara:cluster_risk_within_limit"] == 2
    assert not out.reasons.get("nara:size_valid")
