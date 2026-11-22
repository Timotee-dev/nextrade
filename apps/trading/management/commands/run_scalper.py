"""NEXTRADE autonomous scalping engine: mode switch with hard guards.

    --mode research  scans and reports, places no orders (same as scan_scalp)
    --mode paper     runs the existing multi-position daemon (run_trader) with ONLY approved strategies
    --mode live      refused: needs NEXTRADE_LIVE_ENABLED=yes AND a live broker (Phase 14, not built)

Paper mode never starts without an approval from `approve_strategy`, which needs a passed gate.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from nextrade_core.scalping import approvals
from nextrade_core.scalping.approvals import Mode
from nextrade_core.strategies.registry import SCALP_PREREGISTERED


class Command(BaseCommand):
    help = "Scalping engine: research / paper / live (live disabled)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", default="research", choices=[m.value for m in Mode])
        parser.add_argument("--timeframe", default="5m")
        parser.add_argument("--capital", default="27")
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--max-steps", type=int, default=0)
        parser.add_argument("--venue", default="bybit")

    def handle(self, *args, **o):
        mode = Mode(o["mode"])
        try:
            names = approvals.require_mode(settings.BASE_DIR, mode)
        except PermissionError as exc:
            raise CommandError(str(exc)) from exc
        if mode is Mode.LIVE:
            raise CommandError("live trading is not available: no live broker adapter exists (Phase 14 not built).")
        if mode is Mode.RESEARCH:
            call_command("scan_scalp", venue=o["venue"], timeframe=o["timeframe"], capital=o["capital"],
                         symbols=o["symbols"], stdout=self.stdout)
            return
        scalp = [n for n in names if n in SCALP_PREREGISTERED]
        if not scalp:
            raise CommandError("no approved scalp strategy. Paper trading starts only after a gate pass and approval.")
        self.stdout.write(f"PAPER mode with approved strategies: {', '.join(scalp)}")
        call_command("run_trader", mode="paper", venue=o["venue"], strategies=",".join(scalp), timeframe=o["timeframe"],
                     capital=o["capital"], symbols=o["symbols"], max_steps=o["max_steps"], stdout=self.stdout)
