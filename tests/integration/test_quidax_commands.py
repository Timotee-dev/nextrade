import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from adapters.quidax.rest import QuidaxRest
from apps.market.management.commands import load_quidax_history as load_cmd
from apps.market.management.commands import record_quidax as rec_cmd
from apps.market.models import Candle, Instrument, QuoteSample
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe, utc_to_ms
from tests.helpers import random_walk_frame

pytestmark = pytest.mark.django_db
FX = Path(__file__).parent.parent / "fixtures" / "quidax"
START = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 3, 1, 0, 30, tzinfo=UTC)
PAGE_CAP = 500  # server returns at most this many rows, so paging is exercised


def truth(minutes: int, symbol_seed: int):
    n = int((NOW - START).total_seconds() // (minutes * 60)) + 1
    df = random_walk_frame(n, seed=symbol_seed, start=START, minutes_per_bar=minutes)
    df[["open", "high", "low", "close"]] *= 25000   # ETH/NGN-like prices
    return df


SERIES = {(m, p): truth(p, seed) for seed, m in enumerate(["ethngn", "usdtngn"]) for p in (1, 60, 240)}


def quidax_handler(calls):
    def handler(request):
        path = request.url.path.removeprefix("/api/v1")
        calls.append(path)
        if path == "/markets":
            return httpx.Response(200, content=(FX / "markets.json").read_bytes())
        if path == "/markets/tickers":
            return httpx.Response(200, content=(FX / "tickers.json").read_bytes())
        if path.endswith("/depth"):
            return httpx.Response(200, content=(FX / "depth_btcngn.json").read_bytes())
        if path.endswith("/k"):
            market = path.split("/")[2]
            period = int(request.url.params["period"])
            limit = min(int(request.url.params.get("limit", 30)), PAGE_CAP)
            df = SERIES[(market, period)]
            rows = [[utc_to_ms(t.to_pydatetime()), f"{r.open:.2f}", f"{r.close:.2f}", f"{r.high:.2f}",
                     f"{r.low:.2f}", f"{r.volume:.6f}"] for t, r in df.iterrows()]
            if "timestamp" in request.url.params:      # paging mode (kept to test the loader's paging)
                since_ms = int(request.url.params["timestamp"]) * 1000
                rows = [r for r in rows if r[0] >= since_ms][:limit]
            else:                                      # real Quidax: the newest `limit` bars
                rows = rows[-limit:]
            return httpx.Response(200, json={"status": "success", "data": list(reversed(rows))})  # newest first
        return httpx.Response(404, content=(FX / "not_found.json").read_bytes())
    return handler


@pytest.fixture
def quidax(monkeypatch):
    calls = []
    make = lambda: QuidaxRest("https://q.test/api/v1", transport=httpx.MockTransport(quidax_handler(calls)),  # noqa: E731
                              sleep=lambda s: None)
    monkeypatch.setattr(load_cmd, "make_quidax", make)
    monkeypatch.setattr(load_cmd, "now_utc", lambda: NOW)
    monkeypatch.setattr(rec_cmd, "make_quidax", make)
    monkeypatch.setattr(rec_cmd, "now_utc", lambda: NOW)
    monkeypatch.setattr(rec_cmd, "sleep", lambda s: None)
    return calls


def closed_count(market, period):
    df = SERIES[(market, period)]
    return int(sum(t + timedelta(minutes=period) <= NOW for t in df.index))


def test_history_pages_resumes_and_stores_native_timeframes(quidax, capsys):
    call_command("load_quidax_history", symbols="ETHNGN,USDTNGN", timeframes="1h,4h", start="2026-01-01")
    out = capsys.readouterr().out
    for sym, market in (("ETHNGN", "ethngn"), ("USDTNGN", "usdtngn")):
        inst = Instrument.objects.get(venue="quidax", symbol=sym)
        assert inst.min_notional == Decimal(1250) and inst.quote == "NGN"
        assert Candle.objects.filter(instrument=inst, timeframe="1h").count() == closed_count(market, 60)
        assert Candle.objects.filter(instrument=inst, timeframe="4h").count() == closed_count(market, 240)
    assert closed_count("ethngn", 60) > PAGE_CAP  # really paged
    eth = Candle.objects.filter(instrument__symbol="ETHNGN", timeframe="1h").order_by("open_time").first()
    ref = SERIES[("ethngn", 60)].iloc[0]
    assert float(eth.high) == pytest.approx(ref.high, abs=0.01) and float(eth.close) == pytest.approx(ref.close, abs=0.01)
    assert "1416 bars" in out

    call_command("load_quidax_history", symbols="ETHNGN,USDTNGN", timeframes="1h,4h", start="2026-01-01")
    assert capsys.readouterr().out.count("up to date") == 4


def test_load_frame_prefers_native_bars(quidax, capsys):
    call_command("load_quidax_history", symbols="ETHNGN", timeframes="4h", start="2026-01-01")
    store = CandleStore({"ETHNGN": Instrument.objects.get(symbol="ETHNGN")})
    frame = store.load_frame("ETHNGN", Timeframe.H4, START, NOW)
    assert len(frame) == closed_count("ethngn", 240)


def test_recorder_polls_candles_and_spreads(quidax, capsys):
    call_command("record_quidax", symbols="ETHNGN", max_polls=2)
    inst = Instrument.objects.get(symbol="ETHNGN")
    assert Candle.objects.filter(instrument=inst, timeframe="1m").count() == 120   # 2h backfill on first poll
    assert QuoteSample.objects.filter(instrument=inst).count() == 2
    hours = Candle.objects.filter(instrument=inst, timeframe="1h").order_by("open_time")
    assert hours.count() == 299                       # newest 300 minus the still-forming hour
    assert hours.last().open_time == datetime(2026, 2, 28, 23, tzinfo=UTC)
    q = QuoteSample.objects.first()
    assert (q.bid, q.ask) == (Decimal("104037527"), Decimal("104547199"))


def test_unknown_market_is_a_clear_error(quidax):
    with pytest.raises(CommandError, match="not listed on quidax: XYZNGN"):
        call_command("load_quidax_history", symbols="XYZNGN")


def test_backtest_runs_on_quidax_with_quidax_costs(quidax, capsys, settings, tmp_path):
    call_command("load_quidax_history", symbols="ETHNGN,USDTNGN", timeframes="1h", start="2026-01-01")
    settings.BASE_DIR = tmp_path
    call_command("run_backtest", venue="quidax", timeframe="1h", strategies="trend", train_days=21, test_days=7,
                 min_trades=10)
    out = capsys.readouterr().out
    assert "Costs: fee 0.15%/side, spread 40 bps default" in out and "decision gate" in out


def test_quidax_4h_frame_built_from_stored_hours(quidax, capsys):
    call_command("load_quidax_history", symbols="ETHNGN", timeframes="1h", start="2026-01-01")
    store = CandleStore({"ETHNGN": Instrument.objects.get(symbol="ETHNGN")})
    frame = store.load_frame("ETHNGN", Timeframe.H4, START, NOW)
    assert len(frame) == closed_count("ethngn", 60) // 4
    hours = SERIES[("ethngn", 60)]
    assert frame.iloc[0]["close"] == pytest.approx(hours.iloc[3]["close"], abs=0.01)
