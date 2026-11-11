"""Operator acknowledgement after a RISK_SHUTDOWN or EMERGENCY (the only way out of one)."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.ops.models import AuditLog, record_transition
from apps.trading.bootstrap import LATCHED, last_state
from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine


class Command(BaseCommand):
    help = "Acknowledge a risk shutdown or emergency after reviewing what happened."

    def add_arguments(self, parser):
        parser.add_argument("--reason", required=True, help="what you reviewed and why trading may resume")

    def handle(self, *args, reason: str, **o):
        state = last_state()
        if state not in LATCHED:
            raise CommandError(f"Nothing to acknowledge (state: {state or 'none'}).")
        m = TradingStateMachine(recorder=record_transition, initial=TradingState(state))
        m.transition(TradingState.ANALYSIS, Actor.OPERATOR, f"acknowledged: {reason}", acknowledged=True)
        AuditLog.objects.create(actor="operator", action="acknowledge_halt", target=state,
                                details={"reason": reason, "at": timezone.now().isoformat()})
        self.stdout.write(self.style.SUCCESS(f"{state} acknowledged. Trading may be restarted."))
