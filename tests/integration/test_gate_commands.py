import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command

from adapters.gate.client import GateClient
from apps.market.management.commands import load_gate_history as gate_cmd
from apps.market.models import Candle, Instrument
from apps.research.models import ExperimentRun
from nextrade_core.data.candles import EPOCH
from tests.helpers import random_walk_frame

pytestmark = pytest.mark.django_db
FX = Path(__file__).parent.parent / "fixtures" / "gate"
NOW = datetime(2026, 11, 11, 19, 30, tzinfo=UTC)
SERIES = {}


def series(pair, interval):
    key = (pair, interval)
    if key not in SERIES:
        minutes = {"1h": 60, "4h": 240}[interval]
        start = datetime(2026, 1, 1, tzinfo=UTC) if interval == "1h" else datetime(2024, 1, 1, tzinfo=UTC)
        n = int((NOW - start).total_seconds() // (minutes * 60))
        df = random_walk_frame(n, seed=len(SERIES) + 11, start=start, minutes_per_bar=minutes)
        df[["open", "high", "low", "close"]] *= 20
        SERIES[key] = df
    return SERIES[key]


def handler(request):
    path = request.url.path
    if "/spot/currency_pairs/" in path:
        pair = path.rsplit("/", 1)[1]
        base, quote = pair.split("_")
        return httpx.Response(200, json=dict(json.loads((FX / "pair.json").read_text()), id=pair, base=base, quote=quote))
    if path.endswith("/spot/candlesticks"):
        p = request.url.params
        df = series(p["currency_pair"], p["interval"])
        f, t = int(p["from"]), int(p["to"])
        rows = []
        for ts, r in df.iterrows():
            s = int((ts.to_pydatetime() - EPOCH).total_seconds())
            if f <= s <= t:
                rows.append([str(s), f"{r.quote_volume:.4f}", f"{r.close:.4f}", f"{r.high:.4f}", f"{r.low:.4f}",
                             f"{r.open:.4f}", f"{r.volume:.4f}", "true"])
        return httpx.Response(200, json=rows)
    return httpx.Response(404, json={"label": "NOT_FOUND", "message": "no"})


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(gate_cmd, "make_gate", lambda: GateClient("https://g.test",
                                                                  transport=httpx.MockTransport(handler),
                                                                  sleep=lambda s: None))
    monkeypatch.setattr(gate_cmd, "now_utc", lambda: NOW)


def test_gate_history_loads_and_resumes(gate, capsys):
    call_command("load_gate_history", symbols="ETHUSDT", timeframes="4h")
    inst = Instrument.objects.get(venue="gate", symbol="ETHUSDT")
    n = Candle.objects.filter(instrument=inst, timeframe="4h").count()
    assert 6000 < n < 10000          # capped at Gate's 10,000-bars-back window, from 2024-01-01 here
    first = Candle.objects.filter(instrument=inst, timeframe="4h").order_by("open_time").first()
    assert float(first.open) == pytest.approx(float(series("ETH_USDT", "4h").iloc[0].open), abs=1e-3)
    call_command("load_gate_history", symbols="ETHUSDT", timeframes="4h")
    assert "0 new bars" in capsys.readouterr().out


def test_backtest_on_gate_prices_with_quidax_costs(gate, capsys, settings, tmp_path):
    call_command("load_gate_history", symbols="ETHUSDT,BTCUSDT", timeframes="1h")
    settings.BASE_DIR = tmp_path
    call_command("run_backtest", venue="gate", timeframe="1h", strategies="trend", train_days=60, test_days=20,
                 min_trades=10, ngn_per_usdt="1400")
    out = capsys.readouterr().out
    assert "gate prices, Quidax costs: capital 14.29 USDT (NGN 20,000 at NGN 1400/USDT), minimum order 0.89 USDT" in out
    assert "Costs: fee 0.15%/side, spread 40 bps default" in out and "decision gate" in out
    assert ExperimentRun.objects.count() == 1


def test_sweep_prints_a_table_with_raw_results(gate, capsys, settings, tmp_path):
    call_command("load_gate_history", symbols="ETHUSDT", timeframes="1h")
    settings.BASE_DIR = tmp_path
    call_command("run_backtest", venue="gate", timeframe="1h", strategies="trend", train_days=60, test_days=20,
                 min_trades=10, ngn_per_usdt="1400", sweep="reward_risk=2,3")
    out = capsys.readouterr().out
    assert "Sweep of trend.reward_risk" in out and "raw_mean_r" in out
    assert len(list((tmp_path / "research" / "experiments").glob("*sweep*.json"))) == 1


def test_sweep_rejects_unknown_parameter(gate, settings, tmp_path):
    from django.core.management.base import CommandError
    call_command("load_gate_history", symbols="ETHUSDT", timeframes="1h")
    with pytest.raises(CommandError, match="no parameter"):
        call_command("run_backtest", venue="gate", timeframe="1h", strategies="trend", sweep="nonsense=1,2",
                     ngn_per_usdt="1400", train_days=60, test_days=20)


def test_risk_per_trade_option_scales_limits(gate, capsys, settings, tmp_path):
    call_command("load_gate_history", symbols="ETHUSDT", timeframes="1h")
    settings.BASE_DIR = tmp_path
    call_command("run_backtest", venue="gate", timeframe="1h", strategies="trend", train_days=60, test_days=20,
                 min_trades=10, ngn_per_usdt="1400", risk_per_trade="0.01")
    out = capsys.readouterr().out
    assert "Risk: 1.00% per trade, open risk 8.0%, cluster 6.0%, stress 16%, daily loss 2%, drawdown stop 10%" in out


def test_expanding_window_tests_more_of_the_history(gate, capsys, settings, tmp_path):
    import re
    call_command("load_gate_history", symbols="ETHUSDT", timeframes="1h")
    settings.BASE_DIR = tmp_path
    common = dict(venue="gate", timeframe="1h", strategies="breakout", train_days=90, test_days=20,
                  min_trades=5, ngn_per_usdt="1400")
    call_command("run_backtest", **common)
    rolling = int(re.search(r"folds: (\d+)", capsys.readouterr().out).group(1))
    call_command("run_backtest", expanding=True, **common)
    expanding = int(re.search(r"folds: (\d+)", capsys.readouterr().out).group(1))
    assert expanding >= rolling and expanding > 0
