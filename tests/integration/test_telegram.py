import json
from datetime import timedelta

import httpx
import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.notify import telegram as tg
from apps.ops.models import AuditLog, record_transition
from apps.trading.models import Alert, ControlRequest, TelegramConfirmation
from apps.trading.store import ControlPoller
from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine

pytestmark = pytest.mark.django_db
ME, STRANGER = 111, 999


def bot():
    return tg.Bot({ME}, pin="246810", mode="paper")


def set_state(*states):
    m = TradingStateMachine(recorder=record_transition)
    for s in states:
        m.transition(s, Actor.OPERATOR, "test")


def confirm(reply):
    return bot().handle_callback(ME, f"confirm:{reply.confirm_token}")


def test_strangers_get_no_answer_and_are_audited():
    assert bot().handle_message(STRANGER, "/status") is None
    assert AuditLog.objects.filter(action="unauthorised_message").exists()
    assert bot().handle_callback(STRANGER, "confirm:x") == "Not authorised."


@pytest.mark.parametrize("cmd,expect", [
    ("/status", "State:"), ("/portfolio", "No paper run"), ("/crypto", "Crypto data"), ("/forex", "Forex market"),
    ("/opportunities", "No run"), ("/openpositions", "Open positions"), ("/history", "Last closed"),
    ("/daily", "No run"), ("/weekly", "No run"), ("/monthly", "No run"), ("/risk", "State:"),
    ("/report", "No paper run"), ("/help", "/status"), ("/nonsense", "Unknown command"),
])
def test_read_only_commands(cmd, expect):
    assert expect in bot().handle_message(ME, cmd).text


def test_pause_needs_confirmation_then_queues_a_request():
    reply = bot().handle_message(ME, "/pause")
    assert reply.confirm_token and not ControlRequest.objects.exists()
    assert "Requested: pause" in confirm(reply)
    req = ControlRequest.objects.get()
    assert req.target_state == "paused" and req.source == "telegram"
    assert "expired" in confirm(reply)                      # one use only


def test_cancel_and_expiry():
    reply = bot().handle_message(ME, "/shutdown")
    assert bot().handle_callback(ME, f"cancel:{reply.confirm_token}") == "Cancelled."
    assert not ControlRequest.objects.exists()
    reply = bot().handle_message(ME, "/shutdown")
    TelegramConfirmation.objects.filter(token=reply.confirm_token).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert "expired" in confirm(reply)


def test_resume_requires_pin_and_paused_state():
    assert "PIN" in bot().handle_message(ME, "/resume").text
    assert "PIN" in bot().handle_message(ME, "/resume 000000").text
    set_state(TradingState.DATA_INIT, TradingState.ANALYSIS, TradingState.PAPER)
    m = TradingStateMachine(recorder=record_transition, initial=TradingState.PAPER)
    m.transition(TradingState.RISK_SHUTDOWN, Actor.NARA, "weekly loss")
    assert "acknowledge_halt" in bot().handle_message(ME, "/resume 246810").text
    m.transition(TradingState.ANALYSIS, Actor.OPERATOR, "ack", acknowledged=True)
    m.transition(TradingState.PAPER, Actor.OPERATOR, "restart")
    m.transition(TradingState.PAUSED, Actor.OPERATOR, "pause")
    reply = bot().handle_message(ME, "/resume 246810")
    assert reply.confirm_token
    confirm(reply)
    assert ControlRequest.objects.get().target_state == "paper"
    assert not any("246810" in json.dumps(a.details) for a in AuditLog.objects.all())   # PIN never logged


def test_control_poller_hands_requests_and_watchdog_halts_to_the_trader():
    started = timezone.now()
    ControlRequest.objects.create(target_state="paused", source="telegram", reason="test")
    set_state(TradingState.DATA_INIT, TradingState.ANALYSIS, TradingState.PAPER)
    m = TradingStateMachine(recorder=record_transition, initial=TradingState.PAPER)
    m.transition(TradingState.EMERGENCY, Actor.WATCHDOG, "trader silent")
    poller = ControlPoller(started)
    got = poller.poll()
    assert [(t.value, a.value) for t, a, _ in got] == [("paused", "operator"), ("emergency", "watchdog")]
    assert ControlRequest.objects.get().processed_at is not None
    assert poller.poll() == []


def fake_telegram(sent, fail=False):
    def handler(request):
        if request.url.path.endswith("/sendMessage"):
            if fail:
                return httpx.Response(500, json={"ok": False})
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": ME}, "text": "/pause"}}]})
        return httpx.Response(200, json={"ok": True})
    return httpx.MockTransport(handler)


def test_outbox_delivery_levels_and_retries():
    Alert.objects.create(level="info", kind="x", message="trade opened")
    Alert.objects.create(level="critical", kind="y", message="halted")
    sent = []
    client = tg.TelegramClient("t", transport=fake_telegram(sent))
    assert tg.deliver_outbox(client, {ME}, min_level="warning") == 1
    assert [m["text"] for m in sent] == ["CRITICAL: halted"]
    assert Alert.objects.filter(delivered_at=None).count() == 0
    Alert.objects.create(level="warning", kind="z", message="retry me")
    broken = tg.TelegramClient("t", transport=fake_telegram([], fail=True))
    assert tg.deliver_outbox(broken, {ME}) == 0
    a = Alert.objects.get(kind="z")
    assert a.attempts == 1 and a.delivered_at is None


def test_bot_command_once(monkeypatch, capsys):
    from apps.notify.management.commands import run_telegram_bot as cmd
    sent = []
    monkeypatch.setattr(cmd, "settings_from_env", lambda: ("t", {ME}, "246810"))
    monkeypatch.setattr(cmd, "TelegramClient", lambda token: tg.TelegramClient(token, transport=fake_telegram(sent)))
    call_command("run_telegram_bot", once=True)
    assert sent and "Pause new entries" in sent[0]["text"]
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Confirm"


def test_send_report_queues_alert(capsys):
    call_command("send_report", period="weekly")
    assert "No paper run" in capsys.readouterr().out
    from tests.integration.test_dashboard import make_run
    make_run("paper")
    call_command("send_report", period="weekly")
    a = Alert.objects.get(kind="report_weekly")
    assert "3 trades closed" in a.message and "2 winners" in a.message
