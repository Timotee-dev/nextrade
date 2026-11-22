"""Operator command: authorise EXPLORATORY live-market paper trading of the pre-registered scalp strategies.

This is not a decision-gate pass. It records, in config/approved_strategies.json, that the operator chose to
paper-trade strategies whose validation is not complete, so the engine can collect real-time evidence. Such
approvals are marked `trial`, are PAPER ONLY (`live` is always false), and the paper process labels its results
"unvalidated". Real-money trading stays impossible: it needs the live flag, a live broker (not built) and a
separate gate-based approval.

    python manage.py authorise_paper_trial                 # all six pre-registered scalp strategies
    python manage.py authorise_paper_trial --strategies scalp_momentum,scalp_trend
    python manage.py authorise_paper_trial --revoke
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nextrade_core.scalping import approvals
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, STRATEGIES

NOTE = ("OPERATOR-AUTHORISED EXPLORATORY PAPER TRIAL: not a decision-gate pass; results are unvalidated; "
        "paper only, no real orders")


class Command(BaseCommand):
    help = "Authorise exploratory paper trading (not live, not a gate pass)."

    def add_arguments(self, parser):
        parser.add_argument("--strategies", default=",".join(SCALP_PREREGISTERED))
        parser.add_argument("--timeframe", default="", help="override; default: each strategy's own timeframe")
        parser.add_argument("--revoke", action="store_true", help="remove trial approvals (gate-based ones stay)")

    def handle(self, *args, **o):
        base = settings.BASE_DIR
        codes = [c.strip() for c in o["strategies"].split(",") if c.strip()]
        unknown = [c for c in codes if c not in STRATEGIES]
        if unknown:
            raise CommandError(f"Unknown strategies: {unknown}")
        current = approvals.load(base)
        if o["revoke"]:
            for c in codes:
                if c in current and current[c].trial:
                    current.pop(c)
            approvals.save(base, current)
            self.stdout.write("Trial approvals removed.")
            return
        for c in codes:
            if c in current and not current[c].trial:
                self.stdout.write(f"{c}: keeps its gate-based approval.")
                continue
            current[c] = approvals.Approval(c, paper=True, live=False, evidence=NOTE,
                                            approved_at=datetime.now(UTC).isoformat(timespec="seconds"),
                                            timeframe=o["timeframe"] or SCALP_PREREGISTERED.get(c, ""), trial=True)
            self.stdout.write(f"{c}: authorised for exploratory PAPER trading (live stays off).")
        path = approvals.save(base, current)
        self.stdout.write(f"Saved {path}. Live trading remains disabled and unreachable.")
