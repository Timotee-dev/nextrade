"""Paper trading T1 on Quidax quotes with Gate.io daily signals, end to end (fake venues)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from adapters.gate.client import GateClient, instrument_from_pair
from adapters.quidax.rest import QuidaxRest
from apps.market.models import Instrument
from apps.market.services import save_instrument
from apps.market.store import CandleStore
from apps.trading.management.commands import run_paper_quidax as paper_cmd
from apps.trading.models import Position, TradingRun
from nextrade_core.data.candles import Candle, Timeframe

pytestmark = pytest.mark.django_db(transaction=True)   # the daemon writes from worker threads
GFX = Path(__file__).parent.parent / "fixtures" / "gate"
QFX = Path(__file__).parent.parent / "fixtures" / "quidax"
RATE = Decimal(1400)
LAST_CLOSE = {"ETHUSDT": 2000.0, "BTCUSDT": 60000.0}


def seed_daily(symbol: str, cross: bool) -> None:
    pair = json.loads((GFX / "pair.json").read_text())
    base = symbol.removesuffix("USDT")
    save_instrument(instrument_from_pair(dict(pair, id=f"{base}_USDT", base=base)))
    inst = Instrument.objects.get(venue="gate", symbol=symbol)
    today = Timeframe.D1.floor(datetime.now(UTC))
    n, last = 700, LAST_CLOSE[symbol]
    rng = np.random.default_rng(len(symbol))
    level = np.linspace(last * 0.95, last * 0.90, n)            # drifting down: closes stay below the average
    level[-1] = last if cross else last * 0.89                  # ETH: last close jumps above it
    candles = []
    for i in range(n):
        t = today - timedelta(days=n - i)
        c = float(level[i]) * (1 + rng.normal(0, 0.002))
        o = float(level[i - 1]) if i else c
        hi, lo = max(o, c) * 1.01, min(o, c) * 0.99
        q = lambda x: Decimal(f"{x:.4f}")  # noqa: E731
        candles.append(Candle(symbol, Timeframe.D1, t, q(o), q(hi), q(lo), q(c), Decimal(100), Decimal(100) * q(c), 0))
    CandleStore({symbol: inst}).save_candles(candles, "rest")


def quidax_handler(request):
    path = request.url.path.removeprefix("/api/v1")
    if path == "/markets":
        return httpx.Response(200, content=(QFX / "markets.json").read_bytes())
    if path == "/markets/tickers":
        return httpx.Response(200, content=(QFX / "tickers.json").read_bytes())
    if path.endswith("/depth"):
        market = path.split("/")[2]
        mid = {"usdtngn": 1400.0, "ethngn": LAST_CLOSE["ETHUSDT"] * 1400, "btcngn": LAST_CLOSE["BTCUSDT"] * 1400}[market]
        return httpx.Response(200, json={"status": "success", "data": {
            "bids": [[mid * 0.999, 5]], "asks": [[mid * 1.001, 5]], "timestamp": 0}})
    return httpx.Response(404, content=(QFX / "not_found.json").read_bytes())


@pytest.fixture
def venues(monkeypatch):
    monkeypatch.setattr(paper_cmd, "make_quidax", lambda: QuidaxRest(
        "https://q.test/api/v1", transport=httpx.MockTransport(quidax_handler), sleep=lambda s: None))
    monkeypatch.setattr(paper_cmd, "make_gate", lambda: GateClient(
        "https://g.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
        sleep=lambda s: None))


def test_trend_filter_entry_on_quidax_prices(venues, capsys):
    seed_daily("ETHUSDT", cross=True)
    seed_daily("BTCUSDT", cross=False)
    call_command("run_paper_quidax", new_run=True, symbols="ETHUSDT,BTCUSDT", poll=0, max_steps=2)
    out = capsys.readouterr().out
    assert "NGN 20000 = 14.29 USDT at NGN 1400.00/USDT" in out

    run = TradingRun.objects.get(config__tag="gate-signals/quidax-fills")
    positions = list(Position.objects.filter(run=run))
    assert [p.symbol for p in positions] == ["ETHUSDT"] and positions[0].strategy == "trendfilter"
    p = positions[0]
    assert float(p.entry_price) == pytest.approx(2000 * 1.001, rel=1e-3)        # Quidax ask, in USDT
    assert p.stop < p.entry_price < p.target
    risk_fraction = float(p.initial_risk) / float(run.initial_equity)
    assert 0.005 < risk_fraction <= 0.0101                                         # ~1% of NGN 20,000
    assert p.qty * p.entry_price >= Decimal("0.89")                                # Quidax minimum, in USDT

    # resuming finds the same run and does not open a second ETH position on the same bar
    call_command("run_paper_quidax", symbols="ETHUSDT,BTCUSDT", poll=0, max_steps=1)
    assert Position.objects.filter(run=run).count() == 1 and TradingRun.objects.count() == 1


def test_refuses_strategies_that_need_naia_models(venues):
    with pytest.raises(CommandError, match="only rule-based strategies"):
        call_command("run_paper_quidax", strategies="breakout", max_steps=1)


def test_missing_gate_history_is_explained(venues):
    with pytest.raises(CommandError, match="load_gate_history"):
        call_command("run_paper_quidax", symbols="ETHUSDT", max_steps=1)
