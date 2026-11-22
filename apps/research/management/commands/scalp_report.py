"""Trade analytics from the paper ledger: by strategy, symbol, session, regime; fees vs gross.

    python manage.py scalp_report [--run 1]
Paper results only; backtests are reported by run_backtest. They are never mixed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.trading.models import Position, TradingRun
from nextrade_core.scalping.analytics import from_positions, report


class Command(BaseCommand):
    help = "Post-trade analytics for the paper ledger."

    def add_arguments(self, parser):
        parser.add_argument("--run", type=int, default=None)

    def handle(self, *args, **o):
        run = TradingRun.objects.filter(pk=o["run"]).first() if o["run"] else TradingRun.objects.filter(mode="paper").last()
        if run is None:
            self.stdout.write("No paper run yet.")
            return
        r = report(from_positions(Position.objects.filter(run=run)))
        ov = r["overall"]
        self.stdout.write(f"PAPER run {run.pk} (simulated, not real money): {ov.get('trades', 0)} closed trades")
        if ov.get("trades"):
            self.stdout.write(f"  net {ov['net_profit']:+.4f}  gross before fees {ov['gross_before_fees']:+.4f}  "
                              f"fees {ov['fees']:.4f}  win rate {ov['win_rate']:.0%}  profit factor {ov['profit_factor']:.2f}  "
                              f"max drawdown {ov['max_drawdown']:+.4f}")
            for key in ("strategy", "symbol", "session", "regime"):
                for k, s in r[f"by_{key}"].items():
                    flag = "" if s["enough_data"] else "  (too few trades to judge)"
                    self.stdout.write(f"  {key:9s} {k:14s} n={s['trades']:<4d} net {s['net_profit']:+.4f}{flag}")
        for line in r["findings"]:
            self.stdout.write(f"  finding: {line}")
