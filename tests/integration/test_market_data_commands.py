import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command

from adapters.binance.archive import BinanceArchive, daily_path, monthly_path
from adapters.binance.rest import BinanceRest
from apps.market.management.commands import data_quality_report as dq_cmd
from apps.market.management.commands import load_binance_history as load_cmd
from apps.market.models import Candle, DataQualityEvent
from nextrade_core.data.candles import Timeframe, utc_to_ms
from nextrade_core.data.resample import resample
from tests.helpers import minutes, to_row

pytestmark = pytest.mark.django_db
INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
START = datetime(2026, 2, 25, tzinfo=UTC)
NOW = datetime(2026, 3, 6, 0, 10, 30, tzinfo=UTC)
TRUTH = minutes(int((NOW - START).total_seconds() // 60) + 1, start=START)  # includes the forming bar
TRUTH_1H = resample([c for c in TRUTH if c.close_time <= NOW], Timeframe.H1).candles


def zip_of(bars):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("f.csv", "\n".join(",".join(to_row(c, micros=True)) for c in bars))
    return buf.getvalue()


def archive_files():
    files = {}

    def add(path, bars):
        data = zip_of(bars)
        files[path] = data
        files[path + ".CHECKSUM"] = f"{hashlib.sha256(data).hexdigest()}  x.zip".encode()

    add(monthly_path("BTCUSDT", Timeframe.M1, 2026, 2), [c for c in TRUTH if c.open_time.month == 2])
    for d in range(1, 6):
        add(daily_path("BTCUSDT", Timeframe.M1, date(2026, 3, d)),
            [c for c in TRUTH if c.open_time.date() == date(2026, 3, d)])
    return files


def rest_handler(request):
    if request.url.path == "/api/v3/exchangeInfo":
        return httpx.Response(200, json={"symbols": [s for s in INFO["symbols"] if s["symbol"] == "BTCUSDT"]})
    if request.url.path == "/api/v3/klines":
        series = TRUTH if request.url.params["interval"] == "1m" else TRUTH_1H
        start = int(request.url.params["startTime"])
        rows = [to_row(c) for c in series if utc_to_ms(c.open_time) >= start][: int(request.url.params["limit"])]
        return httpx.Response(200, json=rows)
    return httpx.Response(404)


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    files = archive_files()
    calls = {"archive": 0}

    def archive_handler(request):
        calls["archive"] += 1
        body = files.get(request.url.path)
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    rest = lambda: BinanceRest("https://api.test", transport=httpx.MockTransport(rest_handler))  # noqa: E731
    monkeypatch.setattr(load_cmd, "make_client", rest)
    monkeypatch.setattr(load_cmd, "make_archive", lambda d: BinanceArchive(
        tmp_path / "raw", base_url="https://x.test", transport=httpx.MockTransport(archive_handler)))
    monkeypatch.setattr(load_cmd, "now_utc", lambda: NOW)
    monkeypatch.setattr(dq_cmd, "make_client", rest)
    monkeypatch.setattr(dq_cmd, "now_utc", lambda: NOW)
    monkeypatch.setattr("django.conf.settings.BASE_DIR", tmp_path)
    return calls


def load(capsys):
    call_command("load_binance_history", symbols="BTCUSDT", start="2026-02-25")
    return capsys.readouterr().out


def test_history_load_is_complete_exact_and_resumable(mocked, capsys):
    out = load(capsys)
    closed = [c for c in TRUTH if c.close_time <= NOW]
    assert Candle.objects.count() == len(closed)
    assert "monthly file" in out and "daily files" in out and "today via REST: 10 bars" in out
    first = Candle.objects.order_by("open_time").first()
    assert first.open_time == START and first.close == closed[0].close  # µs timestamps parsed

    calls_before = mocked["archive"]
    out = load(capsys)
    assert "complete" in out and "skipped" in out
    assert mocked["archive"] == calls_before
    assert Candle.objects.count() == len(closed)


def test_quality_report_passes_then_catches_a_gap(mocked, capsys):
    load(capsys)
    call_command("data_quality_report", symbols="BTCUSDT", start="2026-02-25", hours_vs_binance=24)
    out = capsys.readouterr().out
    assert "PASS" in out and "match" in out

    victims = list(Candle.objects.order_by("open_time")[500:520])  # 20 bars = 0.15% missing
    Candle.objects.filter(pk__in=[v.pk for v in victims]).delete()
    call_command("data_quality_report", symbols="BTCUSDT", start="2026-02-25", hours_vs_binance=0)
    out = capsys.readouterr().out
    assert "FAIL" in out
    gap = DataQualityEvent.objects.filter(kind="gap").latest("detected_at")
    assert gap.at == victims[0].open_time and gap.detail["missing_bars"] == 20
    reports = list((Path(load_cmd.settings.BASE_DIR) / "reports").glob("data_quality_*.json"))
    assert reports


@pytest.mark.django_db(transaction=True)  # recorder writes from worker threads
def test_record_live_command_stores_stream_and_backfill(mocked, monkeypatch, capsys):
    import functools

    from apps.market.management.commands import record_live as rec_cmd
    from apps.market.models import QuoteSample
    from services.market_recorder import LiveRecorder
    from tests.unit.test_klines_stream import book_msg, kline_msg
    from tests.unit.test_market_recorder import fake_connector

    monkeypatch.setattr(rec_cmd, "make_rest", lambda: BinanceRest(
        "https://api.test", transport=httpx.MockTransport(rest_handler)))
    monkeypatch.setattr(rec_cmd, "LiveRecorder", functools.partial(LiveRecorder, max_reconnects=0))
    last_closed = [c for c in TRUTH if c.close_time <= NOW][-1]
    monkeypatch.setattr(rec_cmd, "WallClock", lambda: type("C", (), {"now": staticmethod(lambda: NOW)})())
    connector = fake_connector([[kline_msg(last_closed), book_msg()]])
    monkeypatch.setattr(rec_cmd, "ws_connect", lambda url, **kw: connector(url))

    call_command("record_live", symbols="BTCUSDT", stats_every=3600)
    out = capsys.readouterr().out
    assert "Final stats" in out
    assert Candle.objects.filter(source="rest").count() == 60  # 1h initial lookback
    assert QuoteSample.objects.count() == 1


def test_unreachable_binance_gives_a_clear_error(monkeypatch):
    from django.core.management.base import CommandError

    def refuse(request):
        raise httpx.ConnectError("blocked", request=request)

    monkeypatch.setattr(load_cmd, "make_client", lambda: BinanceRest(
        "https://api.test", transport=httpx.MockTransport(refuse)))
    with pytest.raises(CommandError, match="Cannot reach Binance"):
        call_command("load_binance_history", symbols="BTCUSDT")


def test_load_frame_resamples_from_stored_minutes(mocked, capsys):
    from apps.market.models import Instrument
    from apps.market.store import CandleStore

    load(capsys)
    store = CandleStore({"BTCUSDT": Instrument.objects.get(symbol="BTCUSDT")})
    frame = store.load_frame("BTCUSDT", Timeframe.H4, START, START + timedelta(days=2))
    assert len(frame) == 12
    first_4h = [c for c in TRUTH if c.open_time < START + timedelta(hours=4)]
    assert frame.iloc[0]["close"] == float(first_4h[-1].close)
    assert frame.iloc[0]["volume"] == pytest.approx(float(sum(c.volume for c in first_4h)))
