import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command

from adapters.binance.rest import BinanceRest
from apps.market.management.commands import sync_binance_instruments as sync_cmd
from apps.market.models import Instrument
from apps.ops.models import AppendOnlyError, AuditLog, SystemStateTransition, record_transition
from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_binance(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=INFO))
    monkeypatch.setattr(sync_cmd, "make_client", lambda: BinanceRest("https://api.test", transport=transport))


def test_sync_creates_then_updates_instruments(fake_binance):
    call_command("sync_binance_instruments", symbols="BTCUSDT,DOGEUSDT,OLDUSDT")
    assert Instrument.objects.count() == 3
    btc = Instrument.objects.get(symbol="BTCUSDT")
    assert btc.min_notional == Decimal("5") and btc.oco_allowed
    assert not Instrument.objects.get(symbol="OLDUSDT").tradable

    call_command("sync_binance_instruments", symbols="BTCUSDT,DOGEUSDT,OLDUSDT")
    assert Instrument.objects.count() == 3
    audit = AuditLog.objects.filter(action="sync_instruments").first()
    assert audit.details["updated"] == 3


def test_instrument_round_trips_to_domain(fake_binance):
    call_command("sync_binance_instruments", symbols="BTCUSDT")
    domain = Instrument.objects.get(symbol="BTCUSDT").to_domain()
    assert domain.order_violations(Decimal("0.00010"), Decimal("60000.00")) == []


def test_state_transitions_persist_and_are_append_only():
    m = TradingStateMachine(recorder=record_transition)
    m.transition(TradingState.DATA_INIT, Actor.SYSTEM, "boot")
    row = SystemStateTransition.objects.get()
    assert (row.from_state, row.to_state, row.actor) == ("stopped", "data_init", "system")
    row.reason = "edited"
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.delete()
