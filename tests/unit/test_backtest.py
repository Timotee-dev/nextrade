import json
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from adapters.binance.filters import instrument_from_symbol
from nextrade_core.backtest.engine import Backtester
from nextrade_core.backtest.experiments import write_record
from nextrade_core.backtest.metrics import bootstrap_mean_ci, max_drawdown, risk_of_ruin, sharpe, summarize
from nextrade_core.backtest.research import ResearchConfig, decision_gate, evaluate_strategy
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import with_features
from nextrade_core.ml.labels import simulate_exit
from nextrade_core.naia.scoring import MarketContext, score_signal
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits
from nextrade_core.strategies.base import Side, Signal
from nextrade_core.strategies.registry import make
from tests.helpers import T0, random_walk_frame

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
TF = Timeframe.M15
FREE = CostModel(fee_rate=0.0, spread_bps=0.0, slippage_bps=0.0)
COSTS = CostModel()
LOOSE = RiskEngine(RiskLimits(risk_per_trade=D("0.01"), max_open_risk=D("0.5"), max_cluster_risk=D("0.5"),
                              max_stress_loss=D("1"), daily_loss=D("0.9"), weekly_loss=D("0.9"),
                              max_drawdown=D("0.95"), max_consecutive_losses=1000))


def inst(symbol="BTCUSDT"):
    raw = dict(INFO["symbols"][0], symbol=symbol)
    return instrument_from_symbol(raw)


def frame(rows, start=T0):
    idx = pd.date_range(start, periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def sig(at=0, entry=100.0, stop=98.0, target=104.0, hold=5, symbol="BTCUSDT", strategy="t"):
    return Signal(strategy, f"{strategy}-v1", symbol, TF, T0 + TF.delta * at, Side.LONG, entry, stop, target,
                  hold, "", None, {"x": 1.0})


FLAT = [100, 100.5, 99.5, 100]


def run(rows, signals, costs=FREE, cash=D(10000), risk=LOOSE, **kw):
    f = {"BTCUSDT": frame(rows)}
    return Backtester(f, {"BTCUSDT": inst()}, costs, risk, cash, **kw).run(signals)


def test_target_trade_accounting_is_exact():
    res = run([FLAT, [100, 101, 99.5, 100.5], [100.5, 104.5, 100, 104], FLAT], [sig()])
    t = res.trades.iloc[0]
    assert t.reason == "target" and t.exit == D(104) and t.fill == D(100)
    assert t.pnl == t.qty * D(4) and t.r_net == pytest.approx(2.0)
    assert res.equity.iloc[-1] == pytest.approx(float(D(10000) + t.pnl))


def test_cash_is_conserved_with_costs():
    rows = [FLAT, [100, 101, 99.5, 100.5], [100.5, 104.5, 100, 104], FLAT]
    res = run(rows, [sig()], costs=COSTS)
    t = res.trades.iloc[0]
    assert t.fees > 0 and t.pnl < t.qty * D(4)
    assert res.equity.iloc[-1] == pytest.approx(float(D(10000) + t.pnl))


@pytest.mark.parametrize("rows,reason,exit_px", [
    ([FLAT, [100, 105, 97, 100]] + [FLAT] * 4, "stop", 98),                 # both touched: stop wins
    ([FLAT, FLAT, [96, 96.5, 95, 96]] + [FLAT] * 3, "stop", 96),             # gap through stop
    ([FLAT] + [[100, 100.5, 99.5, 100.2]] * 6, "time", 100.2),               # time stop at close
    ([FLAT] + [[100, 100.5, 99.5, 100.2]] * 3, "end_of_data", 100.2),
])
def test_exit_rules(rows, reason, exit_px):
    t = run(rows, [sig()]).trades.iloc[0]
    assert t.reason == reason and float(t.exit) == pytest.approx(exit_px)


def test_skips_when_plan_is_broken_at_the_open_or_already_in_position():
    res = run([FLAT, [97, 97, 96, 96]] + [FLAT] * 3, [sig()])
    assert res.trades.empty and res.rejections["skip:plan_invalid_at_open"] == 1
    rows = [FLAT] + [[100, 100.5, 99.5, 100.2]] * 8
    res = run(rows, [sig(at=0), sig(at=1)])
    assert len(res.trades) == 1 and res.rejections["skip:already_in_position"] == 1


def test_backtest_r_matches_label_simulation():
    """Backtester and label simulator must agree trade by trade (same fill rules)."""
    raw = random_walk_frame(3000, seed=21, minutes_per_bar=15)
    f = with_features(raw, TF)
    signals = make("trend").generate(f, "BTCUSDT", TF)[:25]
    for s in signals:
        lab = simulate_exit(f, s, COSTS)
        res = Backtester({"BTCUSDT": f}, {"BTCUSDT": inst()}, COSTS, LOOSE, D(100000)).run([s])
        if lab is None:
            continue
        t = res.trades.iloc[0]
        assert t.reason == lab.reason.value
        assert t.r_net == pytest.approx(lab.r_net, rel=1e-3, abs=1e-3)


def test_small_account_is_rejected_by_nara():
    res = run([FLAT, [100, 101, 99.5, 100.5], [100.5, 104.5, 100, 104], FLAT], [sig()], cash=D("13"),
              risk=RiskEngine())
    assert res.trades.empty and res.funnel["rejected_by_nara"] == 1 and res.rejections["nara:size_valid"] == 1


def test_naia_rejections_and_ranking_by_score():
    rows = [FLAT, [100, 101, 99.5, 100.5]] + [FLAT] * 6
    f = {"AAAUSDT": frame(rows), "BBBUSDT": frame(rows)}
    instruments = {"AAAUSDT": inst("AAAUSDT"), "BBBUSDT": inst("BBBUSDT")}
    a, b = sig(symbol="AAAUSDT"), sig(symbol="BBBUSDT")
    p = {"AAAUSDT": 0.40, "BBBUSDT": 0.60}
    scorer = lambda s: score_signal(s, p=p[s.symbol], p_source="test", uncertainty=0.0, cost_r=0.0,  # noqa: E731
                                    context=MarketContext())
    # only enough cash for one position: the higher score must win
    limited = RiskEngine(RiskLimits(risk_per_trade=D("0.5"), max_open_risk=D("1"), max_cluster_risk=D("1"),
                                    max_stress_loss=D("1")))
    res = Backtester(f, instruments, FREE, limited, D("150"), scorer=scorer).run([a, b])
    assert set(res.trades.symbol) == {"BBBUSDT"}
    reject_all = lambda s: score_signal(s, p=0.1, p_source="t", uncertainty=0, cost_r=0, context=MarketContext())  # noqa: E731
    res = Backtester(f, instruments, FREE, LOOSE, D(1000), scorer=reject_all).run([a])
    assert res.funnel["rejected_by_naia"] == 1 and res.rejections["naia:low_expected_value"] == 1


def test_consecutive_losses_pause_trading():
    loss_rows = []
    signals = []
    for k in range(10):
        loss_rows += [FLAT, [100, 100, 97, 97], [100, 100.5, 99.5, 100]]
        signals.append(sig(at=3 * k))
    engine = RiskEngine(RiskLimits(max_consecutive_losses=3, consecutive_loss_pause_hours=24,
                                   max_open_risk=D("0.5"), max_cluster_risk=D("0.5"), max_stress_loss=D(1),
                                   daily_loss=D("0.9"), weekly_loss=D("0.9"), max_drawdown=D("0.9")))
    res = run(loss_rows, signals, risk=engine)
    assert len(res.trades) == 3 and res.rejections["skip:halted"] >= 1
    assert res.events[0]["halt"] == "pause_after_consecutive_losses"


# ------------------------------------------------------------------ metrics
def test_drawdown_sharpe_ci_ruin():
    idx = pd.date_range(T0, periods=6, freq="1D", tz="UTC")
    eq = pd.Series([100, 120, 90, 95, 130, 125.0], index=idx)
    mdd, dur = max_drawdown(eq)
    assert mdd == pytest.approx(0.25) and dur == pd.Timedelta(days=2)
    assert sharpe(pd.Series([0.01] * 5)) is None
    lo, hi = bootstrap_mean_ci(np.r_[np.ones(50), -np.ones(50)] * 1.0 + 0.2)
    assert lo < 0.2 < hi
    assert risk_of_ruin(np.full(50, 1.0), 0.02) == 0.0
    assert risk_of_ruin(np.full(50, -1.0), 0.02, n_trades=100) == 1.0
    assert risk_of_ruin([1.0] * 5, 0.01) is None


def test_summarize_fields():
    res = run([FLAT, [100, 101, 99.5, 100.5], [100.5, 104.5, 100, 104], FLAT], [sig()])
    s = summarize(res.trades, res.equity, 10000)
    assert s["trades"] == 1 and s["win_rate"] == 1.0 and s["expectancy_r"] == pytest.approx(2.0)
    assert s["profit_factor"] is None and s["exit_reasons"] == {"target": 1}


# ------------------------------------------------------------------ gate + research
def trades_with(mean, n, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"r_net": rng.normal(mean, 1.0, n)})


def test_gate_logic():
    assert decision_gate(trades_with(0.3, 500), trades_with(0.0, 500, 1))["pass"]
    g = decision_gate(trades_with(0.3, 50), trades_with(0.0, 50, 1))
    assert not g["pass"] and any("need at least" in r for r in g["reasons"])
    assert not decision_gate(trades_with(0.0, 800), trades_with(0.0, 800, 1))["pass"]
    assert not decision_gate(trades_with(0.3, 800), trades_with(0.5, 800, 1))["pass"]


def test_gate_fails_on_a_market_with_no_edge():
    """Pure random walk: no strategy has an edge, so the gate must say no."""
    frames = {s: with_features(random_walk_frame(20000, seed=k, minutes_per_bar=15), TF)
              for k, s in enumerate(["AAAUSDT", "BBBUSDT"], start=30)}
    instruments = {s: inst(s) for s in frames}
    cfg = ResearchConfig(train=pd.Timedelta(days=60), test=pd.Timedelta(days=30), min_trades=50)
    res = evaluate_strategy(make("trend"), frames, instruments, COSTS, RiskEngine(), cfg)
    # NAIA sees a hit rate near 1/3 at 2:1, i.e. negative expected value, and trades nothing
    assert res.folds and all(f["funnel"].get("rejected_by_naia") == f["funnel"]["signals"] for f in res.folds)
    assert res.gate["pass"] is False and res.dataset_hash != "empty"

    # Forcing NAIA to accept everything: trades happen, and the gate still says no
    from nextrade_core.naia.scoring import ScoringConfig
    forced = ResearchConfig(train=cfg.train, test=cfg.test, min_trades=50,
                            scoring=ScoringConfig(hurdle_r=-100.0, max_uncertainty=10.0))
    res2 = evaluate_strategy(make("trend"), frames, instruments, COSTS, RiskEngine(), forced)
    assert res2.summary["trades"] > 50 and res2.baseline_summary["trades"] > 0
    assert res2.gate["pass"] is False


def test_experiment_record(tmp_path):
    path = write_record(tmp_path, "unit", {"metrics": {"x": 1}})
    data = json.loads(path.read_text())
    assert data["metrics"] == {"x": 1} and "git_commit" in data


def test_parameter_sweep_reports_each_value():
    from nextrade_core.backtest.research import parameter_sweep
    from nextrade_core.naia.scoring import ScoringConfig

    frames = {"AAAUSDT": with_features(random_walk_frame(9000, seed=40, minutes_per_bar=15), TF)}
    cfg = ResearchConfig(train=pd.Timedelta(days=40), test=pd.Timedelta(days=20), min_trades=10,
                         scoring=ScoringConfig(hurdle_r=-100.0, max_uncertainty=10.0))
    table = parameter_sweep("trend", "stop_atr", [1.5, 2.5], frames, {"AAAUSDT": inst("AAAUSDT")},
                            COSTS, RiskEngine(), cfg)
    assert list(table["stop_atr"]) == [1.5, 2.5] and (table["trades"] > 0).all()
