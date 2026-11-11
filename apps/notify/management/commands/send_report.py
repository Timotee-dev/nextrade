"""Queue a daily/weekly/monthly performance report for Telegram (schedule it with Windows
Task Scheduler or cron, e.g. daily at 00:05 UTC)."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.trading import reporting as rep
from apps.trading.models import Alert

DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


class Command(BaseCommand):
    help = "Create a performance report alert for the Telegram outbox."

    def add_arguments(self, parser):
        parser.add_argument("--period", choices=list(DAYS), default="daily")
        parser.add_argument("--mode", choices=["paper", "live"], default="paper")

    def handle(self, *args, period, mode, **o):
        run = rep.current_run(mode)
        if run is None:
            self.stdout.write(f"No {mode} run; nothing to report.")
            return
        r = rep.period_report(run, timezone.now() - timedelta(days=DAYS[period]))
        s = rep.summary(run)
        msg = (f"{period.title()} report ({mode} run {run.pk}): {r['trades']} trades closed, {r['wins']} winners, "
               f"P&L {float(r['pnl']):,.4f} {run.account_currency}. Capital {float(s['total_capital']):,.2f}, "
               f"max drawdown {s['max_drawdown'] * 100:.2f}%.")
        Alert.objects.create(level="info", kind=f"report_{period}", message=msg)
        self.stdout.write(msg)
