import numpy as np
import pandas as pd
import pytest
import ta

from nextrade_core.data.candles import Timeframe
from nextrade_core.features import indicators as ind
from nextrade_core.features.feature_set import (LEVEL_COLUMNS, MODEL_COLUMNS, FeatureParams,
                                                build_features, prune_correlated, version_hash)
from nextrade_core.features.frame import align_higher_timeframe, candles_to_frame
from nextrade_core.features.structure import confirmed_swings
from tests.helpers import minutes, random_walk_frame

DF = random_walk_frame(1500)
WARM = 300  # compare after Wilder/EMA seeds have converged


def close_enough(ours: pd.Series, ref: pd.Series, rtol=1e-6, atol=1e-8):
    a, b = ours.iloc[WARM:].to_numpy(float), ref.iloc[WARM:].to_numpy(float)
    assert not np.isnan(a).any(), "indicator has gaps after warm-up"
    np.testing.assert_allclose(a, b, rtol=rtol, atol=atol)


class TestAgainstTaLibrary:
    """`ta` is an independent implementation; matching it guards against formula slips."""

    def test_sma_ema(self):
        close_enough(ind.sma(DF.close, 20), ta.trend.sma_indicator(DF.close, 20))
        close_enough(ind.ema(DF.close, 20), ta.trend.ema_indicator(DF.close, 20))

    def test_macd(self):
        m = ind.macd(DF.close)
        ref = ta.trend.MACD(DF.close)
        close_enough(m["macd"], ref.macd())
        close_enough(m["macd_signal"], ref.macd_signal())

    def test_rsi(self):
        close_enough(ind.rsi(DF.close, 14), ta.momentum.rsi(DF.close, 14), rtol=1e-4)

    def test_atr(self):
        ref = ta.volatility.AverageTrueRange(DF.high, DF.low, DF.close, 14).average_true_range()
        close_enough(ind.atr(DF, 14), ref, rtol=1e-4)

    def test_adx(self):
        ours = ind.adx(DF, 14)
        ref = ta.trend.ADXIndicator(DF.high, DF.low, DF.close, 14)
        close_enough(ours["plus_di"], ref.adx_pos(), rtol=1e-3)
        close_enough(ours["minus_di"], ref.adx_neg(), rtol=1e-3)
        close_enough(ours["adx"], ref.adx(), rtol=1e-3)

    def test_bollinger(self):
        b = ind.bollinger(DF.close, 20)
        ref = ta.volatility.BollingerBands(DF.close, 20, 2)
        close_enough(b["bb_upper"], ref.bollinger_hband())
        close_enough(b["bb_lower"], ref.bollinger_lband())
        close_enough(b["bb_pct_b"], ref.bollinger_pband())

    def test_stochastic(self):
        s = ind.stochastic(DF, 14, 3)
        ref = ta.momentum.StochasticOscillator(DF.high, DF.low, DF.close, 14, 3)
        close_enough(s["stoch_k"], ref.stoch())
        close_enough(s["stoch_d"], ref.stoch_signal())

    def test_roc_cci(self):
        close_enough(ind.roc(DF.close, 12), ta.momentum.roc(DF.close, 12))
        close_enough(ind.cci(DF, 20), ta.trend.cci(DF.high, DF.low, DF.close, 20))

    def test_obv_and_rolling_vwap(self):
        # OBV's level is arbitrary (ta starts at the first bar's volume, we start at 0);
        # only its changes are used, so compare those.
        ref_obv = ta.volume.on_balance_volume(DF.close, DF.volume)
        close_enough(ind.obv(DF).diff(), ref_obv.diff(), rtol=1e-9, atol=1e-6)
        ref = ta.volume.VolumeWeightedAveragePrice(DF.high, DF.low, DF.close, DF.volume, 14).volume_weighted_average_price()
        close_enough(ind.rolling_vwap(DF, 14), ref)


class TestOwnDefinitions:
    def test_wilder_seed_is_simple_mean(self):
        s = pd.Series([np.nan, 1.0, 2.0, 3.0, 4.0])
        out = ind.wilder(s, 3)
        assert np.isnan(out.iloc[2]) and out.iloc[3] == 2.0
        assert out.iloc[4] == pytest.approx((2.0 * 2 + 4.0) / 3)

    def test_rsi_all_gains_is_100(self):
        rising = pd.Series(np.arange(1.0, 40.0))
        assert ind.rsi(rising, 14).iloc[-1] == 100.0

    def test_session_vwap_resets_daily(self):
        df = random_walk_frame(3000)
        v = ind.session_vwap(df)
        first_of_day = df.index[df.index.floor("D") != df.index.floor("D").to_series().shift(1).to_numpy()][1]
        row = df.loc[first_of_day]
        assert v.loc[first_of_day] == pytest.approx((row.high + row.low + row.close) / 3)

    def test_relative_volume_uses_previous_bars(self):
        vol = pd.Series([1.0] * 20 + [5.0])
        assert ind.relative_volume(vol, 20).iloc[-1] == 5.0

    def test_percentile_bounds(self):
        p = ind.rolling_percentile(DF.close, 50).dropna()
        assert p.between(0, 1).all()


class TestStructure:
    def test_swing_is_stamped_at_confirmation_not_pivot(self):
        highs = [10, 11, 12, 15, 12, 11, 10, 9, 9, 9]
        df = pd.DataFrame({"high": highs, "low": [h - 1 for h in highs], "close": highs})
        sw = confirmed_swings(df, k=3)
        assert np.isnan(sw["swing_high"].iloc[3])           # pivot bar: not known yet
        assert sw["swing_high"].iloc[6] == 15                # confirmed 3 bars later
        assert sw["swing_high"].notna().sum() == 1

    def test_equal_highs_count_once(self):
        highs = [1, 2, 5, 5, 2, 1, 0, 0, 0]
        df = pd.DataFrame({"high": highs, "low": [h - 1 for h in highs], "close": highs})
        assert confirmed_swings(df, k=2)["swing_high"].notna().sum() == 1


class TestFeatureSet:
    def test_all_columns_defined_after_warmup(self):
        f = build_features(DF, Timeframe.M1)
        assert list(f.columns) == LEVEL_COLUMNS + MODEL_COLUMNS
        after = f.iloc[FeatureParams().warmup_bars:]
        assert after.isna().sum().sum() == 0

    @pytest.mark.parametrize("cut", [400, 777, 1023, 1499])
    def test_no_lookahead_in_any_column(self, cut):
        """Computing on data up to t must give exactly the row t of the full computation."""
        full = build_features(DF, Timeframe.M1)
        partial = build_features(DF.iloc[:cut + 1], Timeframe.M1)
        pd.testing.assert_series_equal(partial.iloc[-1], full.iloc[cut], check_names=False,
                                       rtol=1e-9, atol=1e-12)

    def test_version_hash_tracks_parameters(self):
        assert version_hash(FeatureParams()) == version_hash(FeatureParams())
        assert version_hash(FeatureParams()) != version_hash(FeatureParams(rsi=10))

    def test_prune_correlated_keeps_priority_order(self):
        f = pd.DataFrame({"a": DF.close.to_numpy(), "b": DF.close.to_numpy() * 2 + 1,
                          "c": DF.volume.to_numpy()})
        assert prune_correlated(f, ["a", "b", "c"]) == ["a", "c"]

    def test_feature_set_prunes_on_real_shapes(self):
        f = build_features(DF, Timeframe.M1).iloc[FeatureParams().warmup_bars:]
        kept = prune_correlated(f)
        assert "stoch_k" in kept and len(kept) < len(MODEL_COLUMNS)


class TestFrames:
    def test_candles_to_frame(self):
        df = candles_to_frame(minutes(5))
        assert list(df.columns)[:5] == ["open", "high", "low", "close", "volume"]
        assert df["close"].dtype == float

    def test_rejects_unsorted(self):
        bars = minutes(3)
        with pytest.raises(ValueError):
            candles_to_frame([bars[1], bars[0]])

    def test_higher_timeframe_only_after_it_closes(self):
        base = random_walk_frame(16 * 6, minutes_per_bar=15)          # 15m bars, 24 hours
        hourly = random_walk_frame(24, minutes_per_bar=60)
        out = align_higher_timeframe(base, Timeframe.M15, hourly, Timeframe.H1)
        t = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
        # 15m bar 10:45 closes 11:00 -> the 10:00 hourly bar (closed 11:00) is available
        assert out.loc[t("2026-01-05 10:45"), "1h_close"] == hourly.loc[t("2026-01-05 10:00"), "close"]
        # 15m bar 10:30 closes 10:45 -> only the 09:00 hourly bar is complete
        assert out.loc[t("2026-01-05 10:30"), "1h_close"] == hourly.loc[t("2026-01-05 09:00"), "close"]
        assert np.isnan(out.loc[t("2026-01-05 00:30"), "1h_close"])  # no hour closed yet
