"""Operator command: approve a strategy for PAPER trading, only if its latest recorded decision
gate PASSED. Nothing else writes config/approved_strategies.json. Live approval is refused: there
is no live broker (Phase 14 is not built) and live needs a separate explicit authorisation.

    python manage.py approve_strategy scalp_momentum
    python manage.py approve_strategy scalp_momentum --revoke
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.research.models import ExperimentRun
from nextrade_core.scalping import approvals
from nextrade_core.strategies.registry import STRATEGIES


class Command(BaseCommand):
    help = "Approve (or revoke) a strategy for paper trading after it passed the decision gate."

    def add_arguments(self, parser):
        parser.add_argument("strategy")
        parser.add_argument("--revoke", action="store_true")
        parser.add_argument("--timeframe", default="", help="timeframe whose validation result to use, e.g. 5m")

    def handle(self, *args, **o):
        code, base = o["strategy"], settings.BASE_DIR
        if code not in STRATEGIES:
            raise CommandError(f"Unknown strategy {code!r}.")
        current = approvals.load(base)
        if o["revoke"]:
            current.pop(code, None)
            approvals.save(base, current)
            self.stdout.write(f"{code}: approval removed.")
            return
        runs = ExperimentRun.objects.filter(strategy_version__startswith=f"{code}-v")
        if o["timeframe"]:
            runs = runs.filter(timeframe=o["timeframe"])
        run = runs.first()
        if run is None:
            raise CommandError(f"{code} has no recorded backtest. Run run_backtest first.")
        if not run.gate_pass:
            reasons = "; ".join(run.gate.get("reasons", [])) or "see the record"
            raise CommandError(f"{code} did NOT pass the decision gate ({reasons}). Not approved.")
        current[code] = approvals.Approval(code, paper=True, live=False,
                                           evidence=f"{run.strategy_version} experiment #{run.pk} on {run.dataset_hash}",
                                           approved_at=datetime.now(UTC).isoformat(timespec="seconds"),
                                           timeframe=o["timeframe"])
        path = approvals.save(base, current)
        self.stdout.write(f"{code}: approved for PAPER trading on evidence #{run.pk}. Live stays disabled. ({path.name})")
