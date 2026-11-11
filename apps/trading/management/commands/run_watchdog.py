"""Run the independent watchdog. Start it in its own window next to run_trader."""

from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ops.models import record_transition
from apps.trading.bootstrap import last_state
from apps.trading.models import Alert, Heartbeat
from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine, TransitionError
from services.watchdog import check_heartbeat


def watch_once(max_silence_s: int = 60) -> str | None:
    beat = Heartbeat.objects.filter(process="trader").first()
    state = last_state()
    verdict = check_heartbeat(beat.at if beat else None, timezone.now(), state, timedelta(seconds=max_silence_s))
    if not verdict.escalate:
        Heartbeat.objects.update_or_create(process="watchdog", defaults={"at": timezone.now(), "detail": {}})
        return None
    m = TradingStateMachine(recorder=record_transition, initial=TradingState(state))
    try:
        m.transition(TradingState.EMERGENCY, Actor.WATCHDOG, verdict.reason)
    except TransitionError:
        return None
    Alert.objects.create(level="critical", kind="watchdog", message=f"EMERGENCY: {verdict.reason}")
    return verdict.reason


class Command(BaseCommand):
    help = "Watch the trader's heartbeat and halt trading if it goes silent."

    def add_arguments(self, parser):
        parser.add_argument("--max-silence", type=int, default=60)
        parser.add_argument("--every", type=float, default=10.0)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **o):
        while True:
            reason = watch_once(o["max_silence"])
            if reason:
                self.stdout.write(self.style.ERROR(f"EMERGENCY declared: {reason}"))
            if o["once"]:
                return
            time.sleep(o["every"])
