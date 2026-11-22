import json
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from apps.trading.management.commands import hft_status, run_hft_scalper
from apps.trading.models import Alert
from nextrade_core.hft.events import Quote, Trade
from services.hft import runtime


def test_ws_guard_check_passes():
    assert run_hft_scalper.ws_guard_check()[1] is True


def test_status_render_has_every_requested_metric():
    from nextrade_core.hft.engine import HftConfig
    from services.hft.runtime import build_engine
    e = build_engine(HftConfig(), ["X"], {}, 27.0, lambda r: None)
    text = hft_status.render({**e.summary(), "symbols": 1, "ts": 1.0, "feed": {}}, now=1.5)
    for word in ("trades/hour", "Win rate", "Profit factor", "GROSS", "FEES", "NET", "Max drawdown", "max simultaneous", "Avg hold", "Funnel"):
        assert word in text


def test_replay_runs_recorded_tape(tmp_path, settings):
    settings.BASE_DIR = tmp_path
    rec = runtime.TickRecorder(tmp_path / "data" / "ticks", min_gap_s=0)
    t, p = 1_700_000_000.0, 100.0
    for i in range(3000):
        t += 0.05
        p *= 1.00003 if i > 1500 else 1.0
        rec.write(Quote("BTCUSDT", t, p * 0.9999, p * 1.0001, 400, 200))
        rec.write(Trade("BTCUSDT", t, p, 1, True))
    rec.close()
    out = StringIO()
    call_command("replay_hft", stdout=out)
    s = json.loads(out.getvalue())
    assert s["quotes"] == 3000 and s["trades_seen"] == 3000 and "net" in s


@pytest.mark.django_db
def test_launcher_check_mode_with_fake_exchange(monkeypatch, tmp_path, settings):
    settings.BASE_DIR = tmp_path

    class Fake:
        def tickers(self):
            return [{"symbol": f"C{i}USDT", "bid1Price": "1", "ask1Price": "1.001", "turnover24h": str(1000 - i)} for i in range(8)]
        def instruments(self, syms):
            from nextrade_core.domain.instrument import AssetClass, Instrument
            return [Instrument("bybit", s, s[:-4], "USDT", AssetClass.CRYPTO, Decimal("0.0001"), Decimal("0.01"), Decimal("0.01"),
                               Decimal("1")) for s in syms]
        def close(self): pass
    monkeypatch.setattr(run_hft_scalper, "BybitClient", Fake)
    out = StringIO()
    call_command("run_hft_scalper", "--check", stdout=out)
    text = out.getvalue()
    assert "Check complete" in text and "FAIL" not in text and "8 markets" in text
    assert not runtime.lock_path(tmp_path).exists()


@pytest.mark.django_db
def test_launcher_refuses_when_5m_scalper_running(monkeypatch, tmp_path, settings):
    settings.BASE_DIR = tmp_path
    from apps.trading import supervisor
    supervisor.acquire_lock(tmp_path)
    class Fake:
        def tickers(self): return [{"symbol": f"C{i}USDT", "bid1Price": "1", "ask1Price": "1.001", "turnover24h": "9"} for i in range(8)]
        def instruments(self, syms): return []
        def close(self): pass
    monkeypatch.setattr(run_hft_scalper, "BybitClient", Fake)
    with pytest.raises(Exception, match="Pre-flight failed"):
        call_command("run_hft_scalper", "--check", stdout=StringIO())
