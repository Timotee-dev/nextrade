"""Show how the paper scalper is doing. Read-only.

    python manage.py paper_status
    python manage.py paper_status --watch 10        # refresh every 10 seconds, Ctrl+C to leave
    python manage.py paper_status --recent 15       # also list the last 15 closed trades
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from apps.trading import paper_status
from apps.trading.models import Position, TradingRun


class Command(BaseCommand):
    help = "Paper scalper dashboard (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--run", type=int, default=None)
        parser.add_argument("--venue", default="bybit")
        parser.add_argument("--watch", type=float, default=0.0)
        parser.add_argument("--recent", type=int, default=0)

    def handle(self, *args, **o):
        qs = TradingRun.objects.filter(mode="paper")
        run = qs.filter(pk=o["run"]).first() if o["run"] else qs.order_by("-id").first()
        if run is None:
            raise CommandError("No paper run yet. Start one with: python manage.py run_paper_scalper")
        try:
            while True:
                now = datetime.now(UTC)
                if o["watch"]:
                    self.stdout.write("\x1b[2J\x1b[H", ending="")
                self.stdout.write(paper_status.render(paper_status.collect(run, now, o["venue"])))
                if o["recent"]:
                    self.stdout.write("  last closed trades:")
                    for p in Position.objects.filter(run=run, status="closed").order_by("-closed_at")[:o["recent"]]:
                        self.stdout.write(f"    {p.closed_at:%m-%d %H:%M} {p.symbol:<12} {p.strategy:<15} {p.exit_reason:<17} "
                                          f"net {float(p.realized_pnl or 0):+.4f} fees {float(p.fees):.4f}")
                if not o["watch"]:
                    return
                time.sleep(o["watch"])
        except KeyboardInterrupt:
            pass
