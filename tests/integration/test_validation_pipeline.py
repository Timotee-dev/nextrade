"""Data probe, Bybit client/loader, REST recorder and the validation command (synthetic data, no network)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pandas as pd
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from adapters.base import RejectedError
from adapters.bybit.client import BybitClient, pick_top_usdt
from apps.market.management.commands import load_bybit_history, probe_venues, record_rest
from apps.research.management.commands import validate_scalping
from apps.market.models import Candle as CandleRow, Instrument, QuoteSample
from apps.market.store import CandleStore
from apps.research.models import ExperimentRun
from nextrade_core.data.candles import Candle, Timeframe
from nextrade_core.scalping import approvals
from nextrade_core.scalping.validation import concurrency, covered_days, max_drawdown, measure, sustains_frequency
from tests.helpers import T0, random_walk_frame

pytestmark = pytest.mark.django_db


def kline_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    q = dict(request.url.params)
    if p.endswith("/market/kline"):
        start, end = int(q["start"]), int(q["end"])
        rows = [[str(t), "100", "101", "99", "100.5", "10", "1000"] for t in range(start, end + 1, 60_000)]
        return httpx.Response(200, json={"retCode": 0, "result": {"list": rows[::-1]}})
    if p.endswith("/market/tickers"):
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "bid1Price": "100", "ask1Price": "100.1", "turnover24h": "900"},
            {"symbol": "ETHUSDT", "bid1Price": "10", "ask1Price": "10.01", "turnover24h": "500"},
            {"symbol": "USDCUSDT", "bid1Price": "1", "ask1Price": "1", "turnover24h": "9999"},
            {"symbol": "BTC3LUSDT", "bid1Price": "1", "ask1Price": "1.1", "turnover24h": "9999"},
            {"symbol": "DEADUSDT", "bid1Price": "", "ask1Price": "", "turnover24h": "9999"}]}})
    if p.endswith("/instruments-info"):
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [
            {"symbol": s, "baseCoin": s[:-4], "quoteCoin": "USDT", "status": "Trading",
             "lotSizeFilter": {"basePrecision": "0.0001", "minOrderQty": "0.0001", "minOrderAmt": "1"},
             "priceFilter": {"tickSize": "0.01"}} for s in ("BTCUSDT", "ETHUSDT")]}})
    return httpx.Response(404, json={})


def test_bybit_client_parses_pages_and_dedupes():
    c = BybitClient(transport=httpx.MockTransport(kline_handler), min_interval_s=0)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = c.candles("BTCUSDT", Timeframe.M1, start, start + timedelta(minutes=2500))
    assert len(bars) == 2500 and bars[0].open_time == start                    # three pages, no gaps or duplicates
    assert all(b.open_time < start + timedelta(minutes=2500) for b in bars)
    assert c.quotes()["BTCUSDT"] == ("100", "100.1")
    assert {i.symbol for i in c.instruments(["BTCUSDT"])} == {"BTCUSDT"}


def test_top_pairs_exclude_stablecoins_leveraged_and_dead_books():
    c = BybitClient(transport=httpx.MockTransport(kline_handler), min_interval_s=0)
    assert [t["symbol"] for t in pick_top_usdt(c.tickers(), 10)] == ["BTCUSDT", "ETHUSDT"]


def test_bybit_region_block_is_a_clear_error():
    c = BybitClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="blocked")), min_interval_s=0)
    with pytest.raises(RejectedError, match="blocked"):
        c.tickers()


def test_load_bybit_history_and_resume(monkeypatch, settings, tmp_path):
    settings.BASE_DIR = tmp_path
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(load_bybit_history, "now_utc", lambda: now)
    monkeypatch.setattr(load_bybit_history, "make_bybit",
                        lambda: BybitClient(transport=httpx.MockTransport(kline_handler), min_interval_s=0))
    call_command("load_bybit_history", top=2, days=0.5)
    assert set(Instrument.objects.filter(venue="bybit").values_list("symbol", flat=True)) == {"BTCUSDT", "ETHUSDT"}
    assert CandleRow.objects.filter(instrument__symbol="BTCUSDT").count() == 720
    assert json.loads((tmp_path / "research" / "universe_bybit.json").read_text())["symbols"] == ["BTCUSDT", "ETHUSDT"]
    monkeypatch.setattr(load_bybit_history, "now_utc", lambda: now + timedelta(minutes=30))
    call_command("load_bybit_history", top=2, days=0.5)
    assert CandleRow.objects.filter(instrument__symbol="BTCUSDT").count() == 750    # only the new bars were added


def test_record_rest_poll_stores_bars_and_valid_quotes_only():
    inst = Instrument.objects.create(venue="bybit", symbol="BTCUSDT", base="BTC", quote="USDT", asset_class="crypto",
                                     tick_size=Decimal("0.01"), step_size=Decimal("0.0001"), min_qty=Decimal("0.0001"),
                                     min_notional=Decimal("1"))
    store = CandleStore({"BTCUSDT": inst})
    now = datetime(2026, 3, 1, 12, 7, 30, tzinfo=UTC)
    client = BybitClient(transport=httpx.MockTransport(kline_handler), min_interval_s=0)
    stats = {"bars": 0, "quotes": 0, "errors": 0}
    record_rest.poll_once(client, store, {"BTCUSDT": inst}, now, stats)
    assert stats["bars"] == 15 and stats["quotes"] == 1 and stats["errors"] == 0
    assert QuoteSample.objects.get().ask > QuoteSample.objects.get().bid
    record_rest.poll_once(client, store, {"BTCUSDT": inst}, now, stats)             # same minute: nothing new
    assert stats["bars"] == 15
    bad = BybitClient(transport=httpx.MockTransport(lambda r: httpx.Response(403)), min_interval_s=0)
    record_rest.poll_once(bad, store, {"BTCUSDT": inst}, now + timedelta(minutes=5), stats)
    assert stats["errors"] >= 1                                                      # failures are counted, not hidden


def test_probe_venues_reports_blocked_and_recommends(monkeypatch, capsys):
    def fake(client, url):
        return ("BLOCKED (HTTP 403)", 0.01) if "binance.com" in url else ("reachable", 0.02)
    monkeypatch.setattr(probe_venues, "probe", fake)
    call_command("probe_venues")
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "Use Bybit" in out
    monkeypatch.setattr(probe_venues, "probe", lambda c, u: ("ERROR (ConnectError)", 0.0) if "bybit" in u else ("reachable", 0.0))
    call_command("probe_venues")
    assert "Bybit unreachable" in capsys.readouterr().out


# ---- measurements --------------------------------------------------------------------------------

def _trades(pnls, starts, hold_min=10):
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    rows = [{"symbol": f"S{i % 3}", "entry_time": t0 + pd.Timedelta(minutes=m), "exit_time": t0 + pd.Timedelta(minutes=m + hold_min),
             "pnl": p, "fees": 0.1, "r_net": p / 10} for i, (p, m) in enumerate(zip(pnls, starts, strict=True))]
    return pd.DataFrame(rows)


def test_measure_known_numbers():
    tr = _trades([10, -5, 10, -20, 5], [0, 20, 40, 60, 80])
    m = measure(tr, days=2.0, initial_cash=1000)
    assert m["trades"] == 5 and m["trades_per_day"] == 2.5 and m["net_pnl"] == 0
    assert m["win_rate"] == 0.6 and m["profit_factor"] == pytest.approx(25 / 25)
    assert m["max_drawdown"] == pytest.approx(((1015 - 995) / 1015) )        # peak 1015 -> trough 995
    assert m["peak_open"] == 1 and m["symbols_traded"] == 3


def test_concurrency_counts_overlap_and_exits_before_entries_at_same_instant():
    tr = _trades([1, 1, 1], [0, 5, 10], hold_min=10)          # [0,10], [5,15], [10,20]
    c = concurrency(tr)
    assert c["peak_open"] == 2                                 # third opens exactly as the first closes
    assert concurrency(pd.DataFrame(columns=tr.columns))["peak_open"] == 0


def test_covered_days_and_empty_measure():
    folds = [{"test_start": "2026-01-01 00:00:00+00:00", "test_end": "2026-01-04 00:00:00+00:00"},
             {"test_start": "2026-01-04 00:00:00+00:00", "test_end": "2026-01-07 00:00:00+00:00"}]
    assert covered_days(folds) == 6
    assert covered_days(folds, pd.Timestamp("2026-01-05", tz="UTC")) == 2
    assert measure(pd.DataFrame(), 3.0, 1000)["trades"] == 0
    assert max_drawdown(pd.DataFrame(), 1000) == 0


def test_sustains_frequency_requires_everything():
    dev = {"net_pnl": 5, "trades_per_day": 30}
    hold = {"net_pnl": 2, "trades_per_day": 25}
    assert sustains_frequency(True, dev, hold, True, 20)[0]
    assert not sustains_frequency(True, dev, None, None, 20)[0]
    assert not sustains_frequency(True, {**dev, "trades_per_day": 3}, hold, True, 20)[0]
    assert not sustains_frequency(True, dev, {**hold, "net_pnl": -1}, True, 20)[0]
    assert not sustains_frequency(False, dev, hold, True, 20)[0]


# ---- validate_scalping end to end -----------------------------------------------------------------

def seed_bybit(symbol, days, seed):
    inst = Instrument.objects.create(venue="bybit", symbol=symbol, base=symbol[:-4], quote="USDT", asset_class="crypto",
                                     tick_size=Decimal("0.0001"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                                     min_notional=Decimal("1"))
    df = random_walk_frame(days * 1440, seed=seed)
    q = lambda x: Decimal(f"{x:.4f}")  # noqa: E731
    CandleStore({symbol: inst}).save_candles(
        [Candle(symbol, Timeframe.M1, t.to_pydatetime(), q(r.open), q(r.high), q(r.low), q(r.close), q(r.volume),
                q(r.quote_volume), int(r.trades)) for t, r in df.iterrows()], "rest")
    return T0 + timedelta(days=days)


def test_validate_scalping_reports_every_measure_and_never_calls_noise_profitable(monkeypatch, settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    end = seed_bybit("AAAUSDT", 24, 3)
    seed_bybit("BBBUSDT", 24, 4)
    monkeypatch.setattr(validate_scalping, "now_utc", lambda: end)
    call_command("validate_scalping", venue="bybit", timeframes="5m", strategies="scalp_momentum,scalp_short",
                 holdout_days=6, train_days=5, test_days=3, min_trades=10)
    out = capsys.readouterr().out
    assert "scalp_momentum" in out and "development:" in out and "trades" in out and "maxDD" in out and "PF" in out
    assert "scalp_short" in out and "cannot short" in out
    assert "signals" in out and "/day" in out
    assert "No strategy passed" in out                                  # random walk: no edge after costs
    reports = list((tmp_path / "research" / "validation").glob("scalping-*"))
    assert {p.suffix for p in reports} == {".json", ".md", ".csv"}
    assert list((tmp_path / "research" / "validation").glob("data-quality-*.md"))
    md = next(p for p in reports if p.suffix == ".md").read_text()
    assert "survivorship" in md and "tests were run" in md
    row = ExperimentRun.objects.get(kind="scalp_validation")
    assert row.gate_pass is False and row.timeframe == "5m"


def test_validate_scalping_needs_enough_history(monkeypatch, settings, tmp_path):
    settings.BASE_DIR = tmp_path
    end = seed_bybit("AAAUSDT", 5, 3)
    monkeypatch.setattr(validate_scalping, "now_utc", lambda: end)
    with pytest.raises(CommandError, match="enough clean data"):
        call_command("validate_scalping", venue="bybit", timeframes="5m")
    with pytest.raises(CommandError, match="No stored gate"):
        call_command("validate_scalping", venue="gate")


# ---- approvals with timeframe, paper venue ----------------------------------------------------------

def test_approval_records_timeframe_and_uses_that_timeframes_evidence(settings, tmp_path):
    settings.BASE_DIR = tmp_path
    mk = lambda tf, ok: ExperimentRun.objects.create(  # noqa: E731
        strategy_version="scalp_trend-v1-abcd1234", model_kind="rule", timeframe=tf, symbols=["X"],
        period_start="2026-01-01T00:00:00Z", period_end="2026-02-01T00:00:00Z", dataset_hash="h", gate_pass=ok, gate={})
    mk("5m", True)
    mk("1m", False)
    with pytest.raises(CommandError, match="did NOT pass"):
        call_command("approve_strategy", "scalp_trend", timeframe="1m")
    call_command("approve_strategy", "scalp_trend", timeframe="5m")
    assert approvals.load(tmp_path)["scalp_trend"].timeframe == "5m"


def test_old_approval_files_without_timeframe_still_load(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "approved_strategies.json").write_text(json.dumps(
        {"scalp_trend": {"strategy": "scalp_trend", "paper": True, "live": False, "evidence": "e", "approved_at": "t"}}))
    assert approvals.load(tmp_path)["scalp_trend"].timeframe == ""


def test_run_trader_accepts_bybit_venue_and_marks_scalp_rule_based(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    with pytest.raises(CommandError, match="authorise_paper_trial"):             # the approvals file is the only door
        call_command("run_trader", venue="bybit", strategies="scalp_trend", timeframe="5m", max_steps=1)
    call_command("authorise_paper_trial")
    with pytest.raises(CommandError, match="No instruments"):
        call_command("run_trader", venue="bybit", strategies="scalp_trend", timeframe="5m", max_steps=1)


def test_bybit_retries_transient_failures_then_succeeds_and_gives_up():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("getaddrinfo failed")
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
    c = BybitClient(transport=httpx.MockTransport(flaky), min_interval_s=0, sleep=lambda s: None)
    assert c.tickers() == [] and calls["n"] == 3
    dead = BybitClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))),
                       min_interval_s=0, sleep=lambda s: None)
    from adapters.base import RetryableError
    with pytest.raises(RetryableError):
        dead.tickers()


def test_load_reports_failed_pairs_honestly(monkeypatch, settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path

    def handler(request):
        if request.url.path.endswith("/market/kline") and request.url.params["symbol"] == "ETHUSDT":
            return httpx.Response(403)
        return kline_handler(request)
    monkeypatch.setattr(load_bybit_history, "now_utc", lambda: datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(load_bybit_history, "make_bybit",
                        lambda: BybitClient(transport=httpx.MockTransport(handler), min_interval_s=0, sleep=lambda s: None))
    call_command("load_bybit_history", top=2, days=0.1)
    out = capsys.readouterr().out
    assert "pairs loaded with sufficient valid data" in out and "FAILED downloads: ETHUSDT" in out and "All 2 pairs" not in out


def test_pegged_tokens_are_not_in_the_universe_and_validation_skips_them(monkeypatch, settings, tmp_path, capsys):
    from adapters.bybit.client import STABLES
    assert {"USDY", "XAUT", "BBSOL"} <= STABLES
    settings.BASE_DIR = tmp_path
    end = seed_bybit("AAAUSDT", 24, 3)
    seed_bybit("USDYUSDT", 24, 4)
    monkeypatch.setattr(validate_scalping, "now_utc", lambda: end)
    call_command("validate_scalping", venue="bybit", timeframes="5m", strategies="scalp_momentum",
                 holdout_days=6, train_days=5, test_days=3, min_trades=10)
    assert "1 symbols on bybit" in capsys.readouterr().out


def test_loader_does_not_call_a_thin_pair_loaded(monkeypatch, settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    monkeypatch.setattr(load_bybit_history, "now_utc", lambda: datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(load_bybit_history, "make_bybit",
                        lambda: BybitClient(transport=httpx.MockTransport(kline_handler), min_interval_s=0, sleep=lambda s: None))
    call_command("load_bybit_history", top=2, days=2)          # only 2 days downloaded: far below the 21-day minimum
    out = capsys.readouterr().out
    assert "NOT sufficient" in out and "only 2 days of history" in out and "All 2 pairs loaded" not in out
    assert "0 of 2 pairs loaded with sufficient valid data" in out


def test_validate_scalping_excludes_incomplete_pairs_and_says_so(monkeypatch, settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    end = seed_bybit("AAAUSDT", 24, 3)
    seed_bybit("THINUSDT", 5, 4)                                # five days only
    inst = Instrument.objects.get(symbol="AAAUSDT")
    CandleRow.objects.filter(instrument=inst, open_time__gte=T0 + timedelta(days=10),
                             open_time__lt=T0 + timedelta(days=10, hours=6)).delete()   # a 6-hour hole
    seed_bybit("HOLEUSDT", 24, 5)
    hole = Instrument.objects.get(symbol="HOLEUSDT")
    CandleRow.objects.filter(instrument=hole, open_time__gte=T0 + timedelta(days=9),
                             open_time__lt=T0 + timedelta(days=9, hours=5)).delete()
    monkeypatch.setattr(validate_scalping, "now_utc", lambda: end)
    monkeypatch.setattr("apps.market.management.commands.check_data.now_utc", lambda: end)
    with pytest.raises(CommandError, match="No pair has enough clean data|Load more"):
        call_command("validate_scalping", venue="bybit", timeframes="5m", strategies="scalp_momentum",
                     holdout_days=6, train_days=5, test_days=3, symbols="THINUSDT,HOLEUSDT,AAAUSDT")
    out = capsys.readouterr().out
    assert "EXCLUDED THINUSDT" in out and "EXCLUDED HOLEUSDT" in out and "EXCLUDED AAAUSDT" in out
    assert "a gap of 360 minutes" in out and "only 5 days of history" in out


def test_check_data_report_counts_gaps_duplicates_invalid_and_sufficiency(monkeypatch, settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    end = seed_bybit("AAAUSDT", 24, 3)
    inst = Instrument.objects.get(symbol="AAAUSDT")
    CandleRow.objects.filter(instrument=inst, open_time__gte=T0 + timedelta(days=10),
                             open_time__lt=T0 + timedelta(days=10, minutes=30)).delete()
    bad = CandleRow.objects.filter(instrument=inst).order_by("open_time")[500]
    bad.high, bad.low = Decimal("1"), Decimal("2")                 # high below low: invalid
    bad.save()
    monkeypatch.setattr("apps.market.management.commands.check_data.now_utc", lambda: end)
    call_command("check_data", venue="bybit", min_days=21)
    out = capsys.readouterr().out
    assert "gaps   1 (max 30 min)" in out and "invalid 1" in out and "dupes 0" in out and "USABLE" in out
    rep = json.loads(next((tmp_path / "research" / "validation").glob("data-quality-*.json")).read_text())
    p = rep["pairs"][0]
    assert p["missing"] == 30 and p["invalid"] == {"high_below_low": 1, "open_or_close_outside_range": 1}
    assert p["bars_by_tf"]["5m"] > 0 and p["ok_by_tf"] == {"1m": True, "3m": True, "5m": True}
    call_command("check_data", venue="bybit", min_days=40)
    assert "EXCLUDED" in capsys.readouterr().out


def test_stablecoins_and_pegged_tokens_are_recognised():
    from adapters.bybit.client import is_pegged
    for base in ("USD1", "RLUSD", "USDC", "USDE", "FDUSD", "USDY", "XAUT", "BBSOL", "ETH3L", "PYUSD", "TUSD"):
        assert is_pegged(base), base
    for base in ("BTC", "ETH", "SOL", "SUI", "UNI", "ZRO", "PEPE", "LINK", "USDT0X"[:0] or "AAVE"):
        assert not is_pegged(base), base


def test_validation_skips_stablecoin_pairs_even_if_stored(monkeypatch, settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    end = seed_bybit("AAAUSDT", 24, 3)
    seed_bybit("USD1USDT", 24, 4)
    seed_bybit("RLUSDUSDT", 24, 5)
    monkeypatch.setattr(validate_scalping, "now_utc", lambda: end)
    call_command("validate_scalping", venue="bybit", timeframes="5m", strategies="scalp_momentum",
                 holdout_days=6, train_days=5, test_days=3, min_trades=10, no_record=True)
    assert "1 symbols on bybit" in capsys.readouterr().out
