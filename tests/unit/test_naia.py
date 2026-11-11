from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import with_features
from nextrade_core.ml.dataset import build_dataset, dataset_hash
from nextrade_core.ml.labels import ExitReason, simulate_exit
from nextrade_core.ml.models import HitRateModel, evaluate, train_calibrated
from nextrade_core.ml.splits import walk_forward
from nextrade_core.ml.walkforward import walk_forward_predict
from nextrade_core.naia.scoring import MarketContext, Reason, ScoringConfig, expected_net_r, score_signal
from nextrade_core.regime.classifier import Regime, classify, transitions
from nextrade_core.strategies.base import Side, Signal
from nextrade_core.strategies.registry import make
from tests.helpers import T0, random_walk_frame

TF = Timeframe.M15
FREE = CostModel(fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0)


# ------------------------------------------------------------------ regimes
def regime_row(**kw):
    base = dict(atr_pct_percentile=0.5, adx=15, ema_spread_atr=0.5, bb_width_percentile=0.8,
                rel_volume=1.0, breakout_up=0, breakout_down=0)
    base.update(kw)
    return base


@pytest.mark.parametrize("row,label", [
    (regime_row(atr_pct_percentile=0.95, adx=40), Regime.HIGH_VOLATILITY),
    (regime_row(breakout_up=1, rel_volume=2.0, adx=40), Regime.BREAKOUT),
    (regime_row(adx=35), Regime.STRONG_BULL),
    (regime_row(adx=35, ema_spread_atr=-1), Regime.STRONG_BEAR),
    (regime_row(adx=25), Regime.WEAK_BULL),
    (regime_row(adx=25, ema_spread_atr=-1), Regime.WEAK_BEAR),
    (regime_row(atr_pct_percentile=0.05), Regime.LOW_VOLATILITY),
    (regime_row(bb_width_percentile=0.3), Regime.RANGE),
    (regime_row(), Regime.UNCERTAIN),
    (regime_row(breakout_up=1, rel_volume=1.0, adx=35), Regime.STRONG_BULL),  # no volume, no breakout
])
def test_regime_decision_table(row, label):
    assert classify(pd.DataFrame([row]))[0] == label


def test_regime_missing_features_is_none_and_transitions_count():
    labels = classify(pd.DataFrame([regime_row(adx=np.nan), regime_row(adx=35), regime_row(adx=25), regime_row(adx=35)]))
    assert labels[0] is None
    t = transitions(labels)
    assert t.loc["strong_bull", "weak_bull"] == 1 and t.loc["weak_bull", "strong_bull"] == 1


# ------------------------------------------------------------------ costs
def test_cost_model():
    c = CostModel(fee_rate=0.001, spread_bps=4, slippage_bps=1, spread_bps_by_symbol={"DOGEUSDT": 10})
    assert c.entry_fill("BTCUSDT", 100.0) == pytest.approx(100 * (1 + 0.0002 + 0.0001))
    assert c.exit_fill("BTCUSDT", 100.0, slippage=False) == pytest.approx(100 * (1 - 0.0002))
    assert c.round_trip_fraction("DOGEUSDT") == pytest.approx(0.002 + 0.001 + 0.0002)
    assert c.cost_in_r("BTCUSDT", 100.0, 99.0) == pytest.approx((0.002 + 0.0004 + 0.0002) * 100 / 1)
    with pytest.raises(ValueError):
        c.cost_in_r("BTCUSDT", 100.0, 101.0)


# ------------------------------------------------------------------ labels
def bars(rows):
    idx = pd.date_range(T0, periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def plan(entry=100.0, stop=98.0, target=104.0, hold=5):
    return Signal("t", "t-v1", "BTCUSDT", TF, T0, Side.LONG, entry, stop, target, hold, "", None)


SIGNAL_BAR = [100, 100.5, 99.5, 100]


def test_target_hit():
    f = bars([SIGNAL_BAR, [100, 101, 99.5, 100.5], [100.5, 104.5, 100, 104]])
    out = simulate_exit(f, plan(), FREE)
    assert out.reason is ExitReason.TARGET and out.r_gross == pytest.approx(2.0) and out.bars_held == 2
    assert out.mfe_r == pytest.approx(2.25) and out.mae_r == pytest.approx(-0.25)


def test_stop_beats_target_in_the_same_bar():
    f = bars([SIGNAL_BAR, [100, 105, 97, 100]] + [[100, 100, 100, 100]] * 4)
    out = simulate_exit(f, plan(), FREE)
    assert out.reason is ExitReason.STOP and out.r_gross == pytest.approx(-1.0)


def test_gap_through_stop_exits_at_open():
    f = bars([SIGNAL_BAR, [100, 100.5, 99.5, 100], [96, 96.5, 95, 96]] + [[96, 96, 96, 96]] * 3)
    out = simulate_exit(f, plan(), FREE)
    assert out.reason is ExitReason.STOP and out.exit_price == 96 and out.r_gross == pytest.approx(-2.0)


def test_time_exit_and_unresolved():
    flat = [[100, 100.5, 99.5, 100.2]] * 5
    out = simulate_exit(bars([SIGNAL_BAR] + flat), plan(), FREE)
    assert out.reason is ExitReason.TIME and out.r_gross == pytest.approx(0.1) and out.bars_held == 5
    assert simulate_exit(bars([SIGNAL_BAR] + flat[:4]), plan(), FREE) is None   # path incomplete


def test_entry_gap_skips_trade():
    assert simulate_exit(bars([SIGNAL_BAR, [97, 97, 96, 96]] + [[96] * 4] * 4), plan(), FREE) is None
    assert simulate_exit(bars([SIGNAL_BAR, [105, 106, 104, 105]] + [[105] * 4] * 4), plan(), FREE) is None


def test_costs_reduce_r():
    f = bars([SIGNAL_BAR, [100, 101, 99.5, 100.5], [100.5, 104.5, 100, 104]])
    costly = CostModel(fee_rate=0.001, spread_bps=2, slippage_bps=2)
    out = simulate_exit(f, plan(), costly)
    assert out.r_net < out.r_gross < 2.0 and out.cost_r > 0


# ------------------------------------------------------------------ dataset + splits
@pytest.fixture(scope="module")
def market():
    raw = random_walk_frame(6000, seed=5, minutes_per_bar=15)
    frame = with_features(raw, TF)
    sigs = make("trend").generate(frame, "BTCUSDT", TF) + make("random", probability=0.1).generate(frame, "BTCUSDT", TF)
    return frame, sigs


def test_dataset_build_and_hash(market):
    frame, sigs = market
    ds = build_dataset({"BTCUSDT": frame}, sigs, CostModel())
    assert len(ds) > 100 and set(ds["y"].unique()) <= {0, 1}
    assert (ds["t_end"] > ds["t"]).all() and ds["t"].is_monotonic_increasing
    assert dataset_hash(ds) == dataset_hash(ds.copy())
    assert dataset_hash(ds) != dataset_hash(ds.iloc[1:])


def test_walk_forward_purges_overlapping_labels():
    t = pd.Series(pd.date_range(T0, periods=400, freq="1h", tz="UTC"))
    t_end = t + pd.Timedelta(hours=10)
    folds = list(walk_forward(t, t_end, pd.Timedelta(days=5), pd.Timedelta(days=2), embargo=pd.Timedelta(hours=2)))
    assert len(folds) >= 5
    for f in folds:
        assert t_end.iloc[f.train_idx].max() <= f.test_start - pd.Timedelta(hours=2)
        assert t.iloc[f.test_idx].min() >= f.test_start
        assert not set(f.train_idx) & set(f.test_idx)


# ------------------------------------------------------------------ models
def synthetic(n=3000, signal=1.5, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({"x1": rng.normal(size=n), "x2": rng.normal(size=n), "noise": rng.normal(size=n)})
    p = 1 / (1 + np.exp(-(signal * X.x1 - 0.5 * X.x2 - 0.7)))
    y = (rng.random(n) < p).astype(int)
    t = pd.Series(pd.date_range(T0, periods=n, freq="2h", tz="UTC"))
    return X.assign(y=y, t=t, t_end=t + pd.Timedelta(hours=6))


@pytest.mark.parametrize("kind", ["logreg", "lgbm"])
def test_models_learn_real_signal_out_of_sample(kind):
    ds = synthetic()
    res = walk_forward_predict(ds, kind, ["x1", "x2", "noise"], train=pd.Timedelta(days=60),
                               test=pd.Timedelta(days=20), prune=False)
    m = res.metrics(ds)
    assert m["auc"] > 0.7 and m["brier_skill"] > 0.05
    assert res.predictions.index.min() > 0             # nothing predicted in-sample
    first = res.predictions[res.predictions.fold == 0]
    assert (first.uncertainty == 0).all()               # one model so far: no disagreement


def test_hit_rate_has_no_skill_and_models_find_none_in_noise():
    ds = synthetic(signal=0.0)
    res = walk_forward_predict(ds, "logreg", ["noise"], train=pd.Timedelta(days=60), test=pd.Timedelta(days=20))
    assert res.metrics(ds)["brier_skill"] < 0.02
    hr = walk_forward_predict(ds, "hitrate", [], train=pd.Timedelta(days=60), test=pd.Timedelta(days=20))
    assert abs(hr.metrics(ds)["brier_skill"]) < 0.02


def test_calibration_matches_observed_frequencies():
    ds = synthetic(n=6000)
    model = train_calibrated(ds.iloc[:4000], ds.y.iloc[:4000].to_numpy(), "lgbm", ["x1", "x2"])
    test = ds.iloc[4000:]
    p = model.predict(test)
    for row in evaluate(test.y.to_numpy(), p, bins=5)["calibration"]:
        if row["n"] > 150:
            assert abs(row["mean_p"] - row["hit_rate"]) < 0.08


def test_tiny_data_falls_back_to_hit_rate_and_explain():
    ds = synthetic(n=40)
    model = train_calibrated(ds, ds.y.to_numpy(), "lgbm", ["x1"])
    assert model.kind == "hitrate" and model.notes
    big = synthetic()
    lr = train_calibrated(big, big.y.to_numpy(), "logreg", ["x1", "x2", "noise"])
    top = lr.explain(big.iloc[[0]], top=2)
    assert len(top) == 2 and "x1" in top or "x2" in top


def test_hit_rate_shrinks_small_samples():
    m = HitRateModel(prior=0.5, prior_weight=10).fit(None, np.array([1, 1]))
    assert m.rate == pytest.approx(7 / 12)


# ------------------------------------------------------------------ scoring
def test_expected_value_formula():
    assert expected_net_r(0.5, 2.0, 0.1) == pytest.approx(0.4)
    assert expected_net_r(1 / 3, 2.0, 0.0) == pytest.approx(0.0)


def test_scoring_accepts_good_and_explains():
    opp = score_signal(plan(), p=0.5, p_source="model:x", uncertainty=0.02, cost_r=0.1, context=MarketContext())
    assert opp.accepted and opp.expected_net_r == pytest.approx(0.4) and opp.score == pytest.approx(0.38)
    assert opp.explanation["p_source"] == "model:x"


@pytest.mark.parametrize("kwargs,reason", [
    (dict(context=MarketContext(data_ok=False)), Reason.DATA_UNRELIABLE),
    (dict(context=MarketContext(market_open=False)), Reason.MARKET_CLOSED),
    (dict(context=MarketContext(blackout=True)), Reason.NEWS_BLACKOUT),
    (dict(context=MarketContext(spread_bps=50)), Reason.SPREAD_TOO_WIDE),
    (dict(context=MarketContext(quote_volume_24h=10)), Reason.LOW_LIQUIDITY),
    (dict(uncertainty=0.3), Reason.HIGH_UNCERTAINTY),
    (dict(p=0.3), Reason.LOW_EXPECTED_VALUE),
    (dict(p=None), Reason.LOW_EXPECTED_VALUE),
    (dict(cost_r=1.95), Reason.COST_HURDLE),
    (dict(context=MarketContext(regime="range"),
          config=ScoringConfig(allowed_regimes={"t": frozenset({"strong_bull"})})), Reason.REGIME_INCOMPATIBLE),
])
def test_scoring_rejections(kwargs, reason):
    args = dict(p=0.5, p_source="hit_rate", uncertainty=0.0, cost_r=0.1, context=MarketContext())
    args.update(kwargs)
    opp = score_signal(plan(), **args)
    assert not opp.accepted and reason in opp.reasons


def test_naia_engine_end_to_end(market):
    from nextrade_core.naia.engine import Naia

    frame, sigs = market
    trend = [s for s in sigs if s.strategy == "trend"]
    ds = build_dataset({"BTCUSDT": frame}, trend, CostModel())
    feats = [c for c in trend[0].features]
    naia = Naia(CostModel())
    first = naia.evaluate(trend[-1], MarketContext())
    assert not first.accepted and first.p is None and first.p_source == "none"

    m1 = train_calibrated(ds, ds.y.to_numpy(), "logreg", feats)
    m2 = train_calibrated(ds.iloc[: len(ds) // 2], ds.y.iloc[: len(ds) // 2].to_numpy(), "logreg", feats)
    naia.set_models("trend", [m2, m1], "trend-logreg-1")
    opp = naia.evaluate(trend[-1], MarketContext())
    assert opp.p is not None and 0 <= opp.p <= 1 and opp.p_source == "model:trend-logreg-1"
    assert opp.uncertainty >= 0 and opp.explanation["top_features"]
    ranked = naia.rank([naia.evaluate(s, MarketContext()) for s in trend[-30:]])
    assert all(a.score >= b.score for a, b in zip(ranked, ranked[1:]))
    with pytest.raises(ValueError):
        naia.set_models("trend", [], "x")


# ------------------------------------------------------------------ rule-based strategies (ADR 0006)
def _trend_signal(strategy="trendfilter"):
    return Signal(strategy, "trendfilter-v1-test", "ETHUSDT", "1d", T0, Side.LONG,
                  entry=2000.0, stop=1840.0, target=3600.0, max_hold_bars=365,
                  invalidation="close below SMA200", invalidation_level=1900.0)


def test_rule_based_signal_accepted_without_a_model():
    from nextrade_core.naia.engine import Naia
    naia = Naia(CostModel(), ScoringConfig(max_spread_bps=60.0))
    naia.mark_rule_based(["trendfilter"])
    opp = naia.evaluate(_trend_signal(), MarketContext(spread_bps=40.0))
    assert opp.accepted and opp.p is None and opp.p_source == "rule-based"


def test_rule_based_signal_still_obeys_the_venue_spread_limit():
    from nextrade_core.naia.engine import Naia
    quidax = Naia(CostModel(), ScoringConfig(max_spread_bps=60.0))
    quidax.mark_rule_based(["trendfilter"])
    assert not quidax.evaluate(_trend_signal(), MarketContext(spread_bps=75.0)).accepted
    default = Naia(CostModel())                       # 15 bps default: a Quidax spread is rejected
    default.mark_rule_based(["trendfilter"])
    opp = default.evaluate(_trend_signal(), MarketContext(spread_bps=40.0))
    assert not opp.accepted and Reason.SPREAD_TOO_WIDE in opp.reasons


def test_unmarked_strategy_without_a_model_is_not_waved_through():
    from nextrade_core.naia.engine import Naia
    naia = Naia(CostModel(), ScoringConfig(max_spread_bps=60.0))
    assert naia.evaluate(_trend_signal(), MarketContext(spread_bps=10.0)).p_source != "rule-based"
