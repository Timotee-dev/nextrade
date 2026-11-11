from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nextrade_core.data.candles import Timeframe, ms_to_utc, utc_to_ms
from nextrade_core.data.resample import resample
from tests.helpers import T0, minute, minutes


class TestTimeframe:
    @pytest.mark.parametrize("tf,when,expected", [
        (Timeframe.H1, datetime(2026, 1, 5, 13, 59, tzinfo=UTC), datetime(2026, 1, 5, 13, tzinfo=UTC)),
        (Timeframe.H4, datetime(2026, 1, 5, 7, 30, tzinfo=UTC), datetime(2026, 1, 5, 4, tzinfo=UTC)),
        (Timeframe.D1, datetime(2026, 1, 5, 23, 59, tzinfo=UTC), datetime(2026, 1, 5, tzinfo=UTC)),
        (Timeframe.M15, datetime(2026, 1, 5, 0, 44, 59, tzinfo=UTC), datetime(2026, 1, 5, 0, 30, tzinfo=UTC)),
    ])
    def test_floor_anchored_at_utc_midnight(self, tf, when, expected):
        assert tf.floor(when) == expected

    def test_minutes(self):
        assert Timeframe.H4.minutes == 240 and Timeframe.D1.minutes == 1440


class TestTimestamps:
    def test_ms_round_trip(self):
        assert ms_to_utc(utc_to_ms(T0)) == T0

    def test_microseconds_detected(self):
        assert ms_to_utc(utc_to_ms(T0) * 1000) == T0

    def test_bar_closed_only_at_close_time(self):
        bar = minute(0)
        assert not bar.is_closed(T0 + timedelta(seconds=59))
        assert bar.is_closed(T0 + timedelta(minutes=1))


class TestResample:
    def test_complete_hours(self):
        result = resample(minutes(120), Timeframe.H1)
        assert len(result.candles) == 2 and result.incomplete == []
        h = result.candles[0]
        src = minutes(60)
        assert h.open_time == T0
        assert h.open == src[0].open and h.close == src[-1].close
        assert h.high == max(c.high for c in src) and h.low == min(c.low for c in src)
        assert h.volume == Decimal("90.0") and h.trade_count == 600

    def test_incomplete_bucket_reported_not_built(self):
        result = resample(minutes(120, skip={75}), Timeframe.H1)
        assert len(result.candles) == 1
        assert result.incomplete == [(T0 + timedelta(hours=1), 59)]

    def test_rejects_unsorted_or_wrong_input(self):
        bars = minutes(3)
        with pytest.raises(ValueError):
            resample([bars[1], bars[0]], Timeframe.H1)
        with pytest.raises(ValueError):
            resample(bars, Timeframe.M1)
        hourly = resample(minutes(60), Timeframe.H1).candles
        with pytest.raises(ValueError):
            resample(hourly, Timeframe.H4)

    def test_volume_sum_is_exact(self):
        bars = [minute(i, volume="0.1") for i in range(60)]
        assert resample(bars, Timeframe.H1).candles[0].volume == Decimal("6.0")
