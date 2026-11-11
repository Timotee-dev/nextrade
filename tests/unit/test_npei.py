import json
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest

from adapters.binance.filters import instrument_from_symbol
from nextrade_core.backtest.engine import Backtester
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import with_features
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits
from nextrade_core.npei.exits import ExitConfig, Health, assess, compare_exit_policies, make_exit_policy, thesis
from nextrade_core.strategies.base import Side, Signal
from nextrade_core.strategies.registry import make
from tests.helpers import T0, random_walk_frame

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
TF = Timeframe.M15
BREAKOUT = make("breakout")
SIG = Signal("breakout", "b-v1", "BTCUSDT", TF, T0, Side.LONG, 100.0, 98.0, 104.0, 10,
             "close back below the breakout level", 99.5, {"x": 1.0})
LOOSE = RiskEngine(RiskLimits(risk_per_trade=D("0.01"), max_open_risk=D("0.5"), max_cluster_risk=D("0.5"),
                              max_stress_loss=D(1), daily_loss=D("0.9"), weekly_loss=D("0.9"), max_drawdown=D("0.9"),
                              max_consecutive_losses=1000))


def bar(close, atr=1.0, **kw):
    return pd.Series({"close": close, "atr": atr, **kw})


@pytest.mark.parametrize("close,bars,health", [
    (101.5, 6, Health.HEALTHY),
    (100.3, 2, Health.DEVELOPING),
    (98.9, 3, Health.DETERIORATING),
])
def test_health_labels(close, bars, health):
    a = assess(SIG, BREAKOUT, fill=100.0, current_stop=98.0, bars_held=bars, bar=bar(close),
               config=ExitConfig(use_invalidation=False))   # labels alone, without the exit rule
    assert a.health is health and a.exit_reason is None


def test_invalidation_and_mandatory_exit():
    a = assess(SIG, BREAKOUT, fill=100.0, current_stop=98.0, bars_held=2, bar=bar(99.4))
    assert a.health is Health.EXIT_RECOMMENDED and a.exit_reason == "invalidated"
    off = assess(SIG, BREAKOUT, fill=100.0, current_stop=98.0, bars_held=2, bar=bar(99.4),
                 config=ExitConfig(use_invalidation=False))
    assert off.exit_reason is None
    m = assess(SIG, BREAKOUT, fill=100.0, current_stop=98.0, bars_held=2, bar=bar(101), mandatory=True)
    assert m.health is Health.MANDATORY_EXIT and m.exit_reason == "risk_exit"


def test_regime_change_marks_deteriorating():
    a = assess(SIG, BREAKOUT, fill=100.0, current_stop=98.0, bars_held=2, bar=bar(101, regime="range"),
               entry_regime="breakout")
    assert a.health is Health.DETERIORATING and any("regime" in n for n in a.notes)


def test_trailing_only_moves_up():
    cfg = ExitConfig(trail_after_r=1.0, trail_atr=1.0)
    a = assess(SIG, BREAKOUT, fill=100.0, current_stop=98.0, bars_held=3, bar=bar(103.0), config=cfg)
    assert a.new_stop == pytest.approx(102.0)
    b = assess(SIG, BREAKOUT, fill=100.0, current_stop=102.5, bars_held=4, bar=bar(103.0), config=cfg)
    assert b.new_stop is None


def test_thesis_record():
    t = thesis(SIG, p=0.45, expected_r=0.3, regime="breakout")
    assert t["invalidation_level"] == 99.5 and t["p"] == 0.45 and t["features"] == {"x": 1.0}


def frame(rows):
    idx = pd.date_range(T0, periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "atr"], index=idx)


def test_backtester_applies_invalidation_and_trailing():
    inst = {"BTCUSDT": instrument_from_symbol(INFO["symbols"][0])}
    rows = [[100, 100.5, 99.6, 100, 1]] + [[100, 100.5, 99.6, 100.2, 1], [100.2, 100.4, 99.6, 99.3, 1]] + \
        [[99.3, 99.6, 99.1, 99.4, 1]] * 8
    policy = make_exit_policy({"breakout": BREAKOUT})
    res = Backtester({"BTCUSDT": frame(rows)}, inst, CostModel(0, 0, 0), LOOSE, D(10000),
                     exit_policy=policy).run([SIG])
    assert res.trades.iloc[0].reason == "invalidated"

    rows = [[100, 100.5, 99.6, 100, 1], [100, 102.6, 99.8, 102.5, 1], [102.5, 103.0, 101.0, 101.2, 1]] + \
        [[101.2, 101.5, 100.5, 101, 1]] * 8
    trail = make_exit_policy({"breakout": BREAKOUT}, ExitConfig(use_invalidation=False, trail_after_r=1.0,
                                                                 trail_atr=1.0))
    res = Backtester({"BTCUSDT": frame(rows)}, inst, CostModel(0, 0, 0), LOOSE, D(10000),
                     exit_policy=trail).run([SIG])
    t = res.trades.iloc[0]
    assert t.reason == "stop" and float(t.exit) == pytest.approx(101.5) and t.r_net > 0


def test_compare_exit_policies_runs_all_variants():
    f = with_features(random_walk_frame(5000, seed=8, minutes_per_bar=15), TF)
    inst = {"BTCUSDT": instrument_from_symbol(INFO["symbols"][0])}
    signals = BREAKOUT.generate(f, "BTCUSDT", TF)
    table = compare_exit_policies(signals, {"BTCUSDT": f}, inst, CostModel(), LOOSE, {"breakout": BREAKOUT},
                                  {"invalidation": ExitConfig(), "trailing": ExitConfig(trail_after_r=1.0)})
    assert list(table.variant) == ["plain", "invalidation", "trailing"]
    assert (table.trades > 0).all()
    assert table.set_index("variant").loc["invalidation", "avg_bars"] <= table.set_index("variant").loc["plain", "avg_bars"]
