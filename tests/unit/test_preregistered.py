"""ADR 0006 strategies: exact rules, no look-ahead in the cross-sectional rank, rule exits."""

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from adapters.binance.filters import instrument_from_symbol
from nextrade_core.backtest.research import ResearchConfig, evaluate_strategy
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import with_features
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import scaled_limits
from nextrade_core.strategies.registry import PREREGISTERED, make
from nextrade_core.strategies.rotation import add_cross_sectional_momentum
from tests.helpers import random_walk_frame
from tests.unit.test_strategies import frame as row_frame

TF = Timeframe.D1


def daily(n, seed, drift=0.0):
    df = random_walk_frame(n, seed=seed, minutes_per_bar=1440)
    if drift:
        trend = np.exp(np.linspace(0, drift, n))
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].mul(trend, axis=0)
    return df


class TestTrendFilter:
    def test_enters_only_on_the_cross_above(self):
        rows = [dict(close=99.0, sma_long=100.0), dict(close=101.0, sma_long=100.0),
                dict(close=102.0, sma_long=100.0), dict(close=99.0, sma_long=100.0), dict(close=101.0, sma_long=100.0)]
        sigs = make("trendfilter").generate(row_frame(rows), "X", TF)
        assert [s.entry for s in sigs] == [101.0, 101.0] and len(sigs) == 2
        s = sigs[0]
        assert s.stop == pytest.approx(98.0) and s.target == pytest.approx(101.0 + 10 * 3.0)
        assert s.max_hold_bars == 365

    def test_exit_rule_uses_the_current_average(self):
        strat = make("trendfilter")
        s = strat.generate(row_frame([dict(close=99.0, sma_long=100.0), dict(close=101.0, sma_long=100.0)]), "X", TF)[0]
        assert not strat.is_invalidated(s, pd.Series({"close": 104.0, "sma_long": 103.0}))
        assert strat.is_invalidated(s, pd.Series({"close": 102.0, "sma_long": 103.0}))   # average rose past price


class TestRotation:
    FRAMES = {s: with_features(daily(900, k, drift=d), TF)
              for k, (s, d) in enumerate([("AAAUSDT", 0.8), ("BBBUSDT", 0.0), ("CCCUSDT", -0.5), ("DDDUSDT", 0.3)])}

    def test_rank_has_no_look_ahead(self):
        full = add_cross_sectional_momentum(self.FRAMES, 30)
        cut = self.FRAMES["AAAUSDT"].index[600]
        part = add_cross_sectional_momentum({s: f[f.index <= cut] for s, f in self.FRAMES.items()}, 30)
        for s in self.FRAMES:
            assert part[s].loc[cut, "xs_rank"] == full[s].loc[cut, "xs_rank"]
            assert part[s].loc[cut, "xs_mom"] == pytest.approx(full[s].loc[cut, "xs_mom"])

    def test_entries_only_on_month_starts_top_n_positive_momentum(self):
        strat = make("rotation")
        frames = strat.prepare_frames(self.FRAMES)
        sigs = [s for sym, f in frames.items() for s in strat.generate(f, sym, TF)]
        assert sigs
        by_day = {}
        for s in sigs:
            row = frames[s.symbol].loc[pd.Timestamp(s.bar_time)]
            assert pd.Timestamp(s.bar_time).day == 1 or s.bar_time == frames[s.symbol].index[0]
            assert row["xs_rank"] <= 2 and row["xs_mom"] > 0
            by_day.setdefault(s.bar_time, []).append(s.symbol)
        assert max(len(v) for v in by_day.values()) <= 2

    def test_needs_prepared_frames(self):
        with pytest.raises(KeyError):
            make("rotation").generate(self.FRAMES["AAAUSDT"], "AAAUSDT", TF)


@pytest.mark.parametrize("code", PREREGISTERED)
def test_research_runs_raw_with_rule_exits(code):
    import json
    from pathlib import Path
    info = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
    frames = {s: with_features(daily(2200, k + 50, drift=0.6), TF) for k, s in enumerate(["AAAUSDT", "BBBUSDT", "CCCUSDT"])}
    inst = {s: instrument_from_symbol(dict(info["symbols"][1], symbol=s)) for s in frames}
    cfg = ResearchConfig(train=pd.Timedelta(days=500), test=pd.Timedelta(days=180), expanding=True,
                         initial_cash=Decimal(1000), min_trades=10, min_train_signals=5)
    res = evaluate_strategy(make(code), frames, inst, cost_model_for("quidax"), RiskEngine(scaled_limits(Decimal("0.01"))), cfg)
    assert res.diagnostics["judged_raw"] is True
    assert res.summary.get("trades", 0) == res.raw_summary.get("trades", 0) > 0     # NAIA filter off
    reasons = res.summary["exit_reasons"]
    if code == "trendfilter":
        assert reasons.get("invalidated", 0) > 0                                     # exits on the rule
    else:
        assert reasons.get("time", 0) > 0                                            # monthly time exit
