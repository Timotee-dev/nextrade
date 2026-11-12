from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from adapters.mt5.local import MT5Local, detect_server_offset
from apps.market.management.commands import load_mt5_history as cmd
from apps.market.models import Candle as CandleRow
from apps.market.models import Instrument, QuoteSample
from tests.mt5_fake import FakeMT5

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 12, 10, 7, tzinfo=UTC)          # a Thursday, market open


@pytest.fixture
def fake(monkeypatch):
    f = FakeMT5(NOW)
    monkeypatch.setattr(cmd, "now_utc", lambda: NOW)
    monkeypatch.setattr(cmd, "make_mt5", lambda **kw: MT5Local(f, **kw))
    return f


def test_detect_server_offset():
    assert detect_server_offset(int((NOW + timedelta(hours=2)).timestamp()), NOW) == 2
    assert detect_server_offset(int((NOW - timedelta(hours=1)).timestamp()), NOW) == -1
    stale = NOW + timedelta(hours=2) - timedelta(minutes=40)      # weekend: last tick is old
    assert detect_server_offset(int(stale.timestamp()), NOW) is None


def test_load_history_stores_utc_bars_instrument_and_spreads(fake, capsys):
    call_command("load_mt5_history", symbols="EURUSD,USDJPY", timeframes="1h,1d", years=0.1, suffix="c")
    out = capsys.readouterr().out
    assert "DEMO, 1000.0 USD, leverage 1:500" in out and "UTC+2" in out
    inst = Instrument.objects.get(venue="mt5", symbol="EURUSD")
    assert inst.asset_class == "forex" and inst.contract_size == Decimal(1000) and inst.min_qty == Decimal("0.01")
    bars = CandleRow.objects.filter(instrument=inst, timeframe="1h").order_by("open_time")
    assert bars.count() > 400
    times = list(bars.values_list("open_time", flat=True))
    assert all(t.weekday() < 5 or (t + timedelta(hours=2)).weekday() < 5 for t in times)
    assert times[-1] + timedelta(hours=1) <= NOW                            # only closed bars, real UTC
    assert times[-1] == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    q = QuoteSample.objects.filter(instrument=inst).first()
    assert q.ask - q.bid == Decimal("0.00012")                              # 12 points of 0.00001
    assert "typical spread 1.09 bps" in out                                 # 0.00012 / 1.10
    # resumable: a second run adds nothing
    call_command("load_mt5_history", symbols="EURUSD,USDJPY", timeframes="1h,1d", years=0.1, suffix="c")
    assert "EURUSD 1h: 0 new bars" in capsys.readouterr().out


def test_wrong_suffix_is_a_clear_error(fake):
    with pytest.raises(CommandError, match="EURUSD.*suffix"):
        call_command("load_mt5_history", symbols="EURUSD", timeframes="1h", years=0.01)


def test_terminal_not_running_is_a_clear_error(monkeypatch):
    f = FakeMT5(NOW, ok=False)
    monkeypatch.setattr(cmd, "make_mt5", lambda **kw: MT5Local(f, **kw))
    with pytest.raises(CommandError, match="Open MetaTrader 5"):
        call_command("load_mt5_history", symbols="EURUSD", timeframes="1h")


def test_real_account_warning(monkeypatch, capsys):
    f = FakeMT5(NOW, mode=2)
    monkeypatch.setattr(cmd, "now_utc", lambda: NOW)
    monkeypatch.setattr(cmd, "make_mt5", lambda **kw: MT5Local(f, **kw))
    call_command("load_mt5_history", symbols="EURUSD", timeframes="1d", years=0.05, suffix="c")
    assert "REAL account" in capsys.readouterr().out


def test_run_backtest_on_mt5_forex_data(fake, capsys):
    call_command("load_mt5_history", symbols="EURUSD,USDJPY", timeframes="1h", years=0.25, suffix="c")
    capsys.readouterr()
    call_command("run_backtest", venue="mt5", timeframe="1h", strategies="breakout",
                 train_days=20, test_days=10, min_trades=1, expanding=True, risk_per_trade="0.01")
    out = capsys.readouterr().out
    assert "Forex margin account: 10000 USD at 1:200" in out
    assert "spread 2 bps default" in out and "'EURUSD': 2.18" in out and "(measured x2)" in out
    assert "decision gate:" in out


def test_run_backtest_forex_strategy_two_sided(fake, capsys):
    call_command("load_mt5_history", symbols="EURUSD,USDJPY", timeframes="1h", years=0.25, suffix="c")
    capsys.readouterr()
    call_command("run_backtest", venue="mt5", timeframe="1h", strategies="fx_trend,fx_meanrev",
                 train_days=20, test_days=10, min_trades=1, risk_per_trade="0.01")
    out = capsys.readouterr().out
    assert out.count("decision gate:") == 2 and "judged on raw trades" in out
