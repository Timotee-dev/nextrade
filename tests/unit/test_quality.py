from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from nextrade_core.data.candles import Timeframe
from nextrade_core.data.quality import IssueKind, QualityScanner
from tests.helpers import T0, minute, minutes

TF = Timeframe.M1


def kinds(scanner):
    return [i.kind for i in scanner.issues]


def test_clean_data_has_no_issues():
    s = QualityScanner("BTCUSDT", TF)
    s.feed(minutes(500))
    assert s.issues == [] and s.completeness() == 1.0


def test_gap_counted_with_missing_bars():
    s = QualityScanner("BTCUSDT", TF)
    s.feed(minutes(100, skip={10, 11, 12}))
    assert kinds(s) == [IssueKind.GAP]
    assert s.issues[0].at == T0 + timedelta(minutes=10)
    assert s.issues[0].detail["missing_bars"] == 3
    assert s.missing_bars == 3 and round(s.completeness(), 4) == 0.97


def test_duplicates_and_out_of_order_are_dropped():
    bars = minutes(5)
    s = QualityScanner("BTCUSDT", TF)
    s.feed([bars[0], bars[1], bars[1], bars[3], bars[2], bars[4]])
    assert IssueKind.DUPLICATE in kinds(s) and IssueKind.OUT_OF_ORDER in kinds(s)
    assert s.bars_ok == 4  # 0, 1, 3, 4


def test_bad_bars():
    good = minute(0)
    s = QualityScanner("BTCUSDT", TF)
    s.feed([replace(good, high=good.low - 1),
            replace(minute(1), low=Decimal(0)),
            replace(minute(2), volume=Decimal(-1))])
    assert kinds(s) == [IssueKind.OHLC_INCONSISTENT, IssueKind.NON_POSITIVE_PRICE, IssueKind.NEGATIVE_VOLUME]


def test_spike_detected_and_small_moves_ignored():
    bars = minutes(400)
    spike_at = 300
    p = bars[spike_at].close * Decimal("1.08")
    bars[spike_at] = replace(bars[spike_at], close=p, high=p)
    s = QualityScanner("BTCUSDT", TF)
    s.feed(bars)
    spikes = [i for i in s.issues if i.kind == IssueKind.SPIKE]
    assert spikes and spikes[0].at == bars[spike_at].open_time


def test_chunk_boundaries_do_not_hide_gaps_or_spikes():
    bars = minutes(400, skip={250})
    p = bars[300].close * Decimal("1.08")
    bars[300] = replace(bars[300], close=p, high=p)
    whole, chunked = QualityScanner("X", TF), QualityScanner("X", TF)
    whole.feed(bars)
    for i in range(0, len(bars), 70):
        chunked.feed(bars[i:i + 70])
    assert [(i.kind, i.at) for i in whole.issues] == [(i.kind, i.at) for i in chunked.issues]


def test_stale():
    s = QualityScanner("BTCUSDT", TF)
    s.feed(minutes(10))
    last_close = T0 + timedelta(minutes=10)
    assert not s.check_stale(last_close + timedelta(minutes=2))
    assert s.check_stale(last_close + timedelta(minutes=4))
    assert QualityScanner("Y", TF).check_stale(T0)


def test_completeness_against_expected_window():
    s = QualityScanner("BTCUSDT", TF)
    s.feed(minutes(90))
    assert s.completeness(T0, T0 + timedelta(minutes=100)) == 0.9
    assert s.summary()["rows"] == 90
