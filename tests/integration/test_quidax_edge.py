from datetime import UTC, datetime, timedelta

import httpx
import pytest
from django.core.management import call_command

from adapters.gate.client import GateClient
from adapters.quidax.rest import QuidaxRest
from apps.market.management.commands import record_books as rec
from apps.market.models import BookSnapshot

pytestmark = pytest.mark.django_db
STATE = {"i": 0}


def qx_handler(request: httpx.Request) -> httpx.Response:
    market = request.url.path.split("/")[-2]
    i = STATE["i"]
    usd = {"btcngn": 100_000.0, "ethngn": 4_000.0, "usdtngn": 1.0}[market]
    rate = 1500.0
    dip = 0.02 if (i % 6 == 3 and market != "usdtngn") else 0.0   # every 6th poll coins dip 2% below world
    mid = usd * rate * (1 - dip)
    half = mid * 0.002
    return httpx.Response(200, json={"status": "success", "data": {
        "bids": [[str(mid - half), "1"], [str(mid - 2 * half), "2"]],
        "asks": [[str(mid + 2 * half), "2"], [str(mid + half), "1"]]}})


def gate_handler(request: httpx.Request) -> httpx.Response:
    pair = request.url.params["currency_pair"]
    px = {"BTC_USDT": 100_000.0, "ETH_USDT": 4_000.0}[pair]
    return httpx.Response(200, json=[{"currency_pair": pair, "highest_bid": str(px - 1), "lowest_ask": str(px + 1)}])


@pytest.fixture
def venues(monkeypatch):
    t0 = datetime.now(UTC) - timedelta(hours=1)

    def clock():
        STATE["i"] += 1
        return t0 + timedelta(seconds=10 * STATE["i"])
    STATE["i"] = 0
    monkeypatch.setattr(rec, "now_utc", clock)
    monkeypatch.setattr(rec, "sleep", lambda s: None)
    monkeypatch.setattr(rec, "make_quidax", lambda: QuidaxRest("https://q.test/api/v1",
                        transport=httpx.MockTransport(qx_handler), sleep=lambda s: None))
    monkeypatch.setattr(rec, "make_gate", lambda: GateClient("https://g.test",
                        transport=httpx.MockTransport(gate_handler), sleep=lambda s: None))


def test_record_books_then_analyze(venues, capsys):
    call_command("record_books", bases="BTC,ETH", max_polls=120)
    out = capsys.readouterr().out
    assert "Stopped after 120 polls" in out
    assert BookSnapshot.objects.filter(venue="quidax").count() == 360        # BTC, ETH, USDT x 120
    assert BookSnapshot.objects.filter(venue="gate").count() == 240
    snap = BookSnapshot.objects.filter(venue="quidax", symbol="BTCNGN").first()
    assert float(snap.asks[0][0]) < float(snap.asks[1][0]) and float(snap.bids[0][0]) > float(snap.bids[1][0])
    call_command("analyze_quidax_edge", days=1)
    out = capsys.readouterr().out
    assert "== BTCNGN" in out and "== ETHNGN" in out and "market making:" in out
    assert "gap: median" in out and "EXPLORATORY" in out
    import json
    from pathlib import Path

    from django.conf import settings
    rec_path = sorted((Path(settings.BASE_DIR) / "research" / "experiments").glob("*quidax_edge.json"))[-1]
    sweep = json.loads(rec_path.read_text())["markets"]["BTCNGN"]["gap"]["sweep"]
    k100 = next(r for r in sweep if r["k_bps"] == 100)
    assert k100["trades"] >= 15 and k100["mean_net_bps"] > 0       # 2% dips that close pay more than ~70 bps


def test_analyze_without_data_explains_what_to_run():
    from django.core.management import CommandError
    with pytest.raises(CommandError, match="record_books"):
        call_command("analyze_quidax_edge")
