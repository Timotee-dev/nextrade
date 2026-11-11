from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import with_features
from nextrade_core.strategies.base import Side, Signal
from nextrade_core.strategies.baseline import RandomEntry, RandomEntryParams, bar_uniforms
from nextrade_core.strategies.breakout import Breakout, BreakoutParams
from nextrade_core.strategies.filters import (HigherTimeframeTrend, MomentumFilter, VolatilityBand,
                                              VolumeConfirmation)
from nextrade_core.strategies.mean_reversion import MeanReversion
from nextrade_core.strategies.registry import TRADABLE, make
from nextrade_core.strategies.trend import TrendFollowing, TrendParams
from tests.helpers import T0, random_walk_frame

TF = Timeframe.M15
DATA = {f"S{i}": with_features(random_walk_frame(3000, seed=i, minutes_per_bar=15), TF) for i in range(3)}


def frame(rows):
    base = dict(open=100.0, high=101.0, low=99.0, close=100.0, atr=1.0, ema_fast=99.5, ema_slow=98.0,
                adx=25.0, ema_fast_slope_atr=0.2, rsi=50.0, donchian_high=99.5, rel_volume=1.0, roc=1.0,
                atr_pct_percentile=0.5)
    idx = pd.date_range(T0, periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame([{**base, **r} for r in rows], index=idx)


class TestSignal:
    def test_properties(self):
        s = Signal("t", "t-v1-x", "BTCUSDT", TF, T0, Side.LONG, 100.0, 98.0, 104.0, 10, "rule", None)
        assert s.reward_risk == 2.0 and s.risk_per_unit == 2.0
        assert s.decision_time == T0 + TF.delta
        assert s.key == "t-v1-x:BTCUSDT:15m:20260105T0000"


class TestTrend:
    def test_fires_on_pullback_in_rising_uptrend(self):
        sig = TrendFollowing().generate(frame([dict(low=99.4)]), "X", TF)
        assert len(sig) == 1
        s = sig[0]
        assert (s.entry, s.stop, s.target) == (100.0, 98.0, 104.0)
        assert s.invalidation_level == 98.0 and s.max_hold_bars == 48

    @pytest.mark.parametrize("change", [dict(ema_fast=97.0), dict(adx=15.0), dict(ema_fast_slope_atr=-0.1),
                                        dict(close=99.0), dict(low=100.5)])
    def test_each_condition_is_required(self, change):
        assert TrendFollowing().generate(frame([{"low": 99.4, **change}]), "X", TF) == []

    def test_cooldown_blocks_repeat_signals(self):
        rows = [dict(low=99.4)] * 10
        sigs = TrendFollowing(replace(TrendParams(), cooldown_bars=3)).generate(frame(rows), "X", TF)
        assert [s.bar_time for s in sigs] == [T0, T0 + 4 * TF.delta, T0 + 8 * TF.delta]

    def test_missing_required_value_blocks(self):
        assert TrendFollowing().generate(frame([dict(low=99.4, adx=np.nan)]), "X", TF) == []


class TestMeanReversion:
    def test_fires_after_unusual_drop_in_range(self):
        rows = [dict(close=100.0 + (0.3 if i % 2 else -0.3), adx=15.0, rsi=25.0) for i in range(20)]
        rows.append(dict(close=97.0, adx=15.0, rsi=25.0, atr=1.0))
        sigs = MeanReversion().generate(frame(rows), "X", TF)
        assert len(sigs) == 1
        s = sigs[0]
        assert s.entry == 97.0 and s.stop == pytest.approx(95.5)
        assert s.target == pytest.approx(np.mean([r["close"] for r in rows[1:]]))

    def test_trend_blocks(self):
        rows = [dict(close=100.0 + (0.3 if i % 2 else -0.3), adx=35.0, rsi=25.0) for i in range(20)]
        rows.append(dict(close=97.0, adx=35.0, rsi=25.0))
        assert MeanReversion().generate(frame(rows), "X", TF) == []


class TestBreakout:
    def test_fires_on_volume_breakout(self):
        sig = Breakout().generate(frame([dict(close=100.0, donchian_high=99.5, rel_volume=2.0)]), "X", TF)
        s = sig[0]
        assert s.stop == pytest.approx(99.0)        # min(level - 0.5 ATR, close - 1 ATR)
        assert s.target == pytest.approx(102.0) and s.invalidation_level == 99.5

    def test_too_extended_or_quiet_blocks(self):
        assert Breakout().generate(frame([dict(close=101.0, donchian_high=99.5, rel_volume=2.0)]), "X", TF) == []
        assert Breakout().generate(frame([dict(close=100.0, donchian_high=99.5, rel_volume=1.0)]), "X", TF) == []

    def test_invalidation_rule(self):
        s = Breakout().generate(frame([dict(close=100.0, donchian_high=99.5, rel_volume=2.0)]), "X", TF)[0]
        assert Breakout().is_invalidated(s, pd.Series({"close": 99.4}))
        assert not Breakout().is_invalidated(s, pd.Series({"close": 99.6}))


class TestFilters:
    def test_filters_narrow_signals_and_change_version(self):
        base = Breakout()
        filtered = Breakout(filters=(VolumeConfirmation(3.0),))
        f = frame([dict(close=100.0, donchian_high=99.5, rel_volume=2.0)])
        assert len(base.generate(f, "X", TF)) == 1 and filtered.generate(f, "X", TF) == []
        assert base.version != filtered.version

    def test_each_filter(self):
        f = frame([dict(roc=-1.0, atr_pct_percentile=0.95, rel_volume=1.3)])
        assert not MomentumFilter().mask(f, f).iloc[0]
        assert not VolatilityBand().mask(f, f).iloc[0]
        assert VolumeConfirmation().mask(f, f).iloc[0]
        with pytest.raises(KeyError):
            HigherTimeframeTrend().mask(f, f)


class TestOnRealisticData:
    @pytest.mark.parametrize("code", TRADABLE)
    def test_signals_well_formed(self, code):
        strat = make(code)
        sigs = [s for sym, f in DATA.items() for s in strat.generate(f, sym, TF)]
        assert sigs, f"{code} produced no signals on 93 days of data"
        for s in sigs:
            assert s.stop < s.entry < s.target and s.strategy_version == strat.version
            assert s.features and all(np.isfinite(v) for v in s.features.values())

    @pytest.mark.parametrize("code", TRADABLE)
    @pytest.mark.parametrize("cut", [1500, 2999])
    def test_replay_gives_identical_signals(self, code, cut):
        """Spec Phase 4 gate: signals computed on data up to bar t equal the full run's up to t."""
        raw = random_walk_frame(3000, seed=0, minutes_per_bar=15)
        full = make(code).generate(with_features(raw, TF), "S0", TF)
        part = make(code).generate(with_features(raw.iloc[:cut + 1], TF), "S0", TF)
        horizon = raw.index[cut]
        assert part == [s for s in full if s.bar_time <= horizon]

    def test_version_tracks_parameters(self):
        assert make("trend").version == make("trend").version
        assert make("trend").version != make("trend", adx_min=25).version


class TestRandomBaseline:
    def test_draws_depend_only_on_time_seed_symbol(self):
        idx = DATA["S0"].index
        u = bar_uniforms(idx, 1, "S0")
        assert ((0 <= u) & (u < 1)).all() and 0.45 < u.mean() < 0.55
        np.testing.assert_array_equal(bar_uniforms(idx[100:], 1, "S0"), u[100:])
        assert not np.array_equal(bar_uniforms(idx, 1, "S1"), u)

    def test_rate_close_to_probability(self):
        sigs = RandomEntry(RandomEntryParams(probability=0.05)).generate(DATA["S0"], "S0", TF)
        assert 0.03 < len(sigs) / len(DATA["S0"]) < 0.07

    def test_matching_copies_the_strategy_exits(self):
        strat = make("breakout")
        base = RandomEntry.matching(strat, probability=0.2)
        f = DATA["S1"]
        plan = strat._plan(f)
        sigs = base.generate(f, "S1", TF)
        assert sigs and base.version.endswith("-breakout")
        for s in sigs[:50]:
            row = plan.loc[pd.Timestamp(s.bar_time)]
            assert s.stop == row["stop"] and s.target == row["target"] and s.max_hold_bars == row["max_hold"]
