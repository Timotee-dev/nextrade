"""Live monitor of the HFT paper scalper (reads its status file; the scalper keeps running)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from services.hft import runtime


def render(s: dict, now: float | None = None) -> str:
    now = now or time.time()
    age = now - s.get("ts", 0)
    def f(x, nd=2):
        return "-" if x is None else f"{x:.{nd}f}"
    feed = s.get("feed", {})
    lines = [
        f"HFT paper scalper | status file {age:.0f}s old{'  (STALE: is it running?)' if age > 10 else ''} | {s['symbols']} markets",
        f"Feed: {feed.get('messages', 0)} msgs, {feed.get('connects', 0)} connects, {feed.get('reconnects', 0)} reconnects"
        + (f", last error: {feed['last_error']}" if feed.get("last_error") else ""),
        f"Events: {s['quotes']} quotes, {s['trades_seen']} trades | avg handling {f(s['avg_event_us'], 1)} us, worst {f(s['max_event_us'], 0)} us",
        f"Funnel: scans {s['scans']} -> signals {s['signals']} -> entries {s['entries']} -> closed {s['closed']}",
        f"Open now {s['open']} (max simultaneous {s['max_open']})",
        f"Closed {s['closed']} in {f(s['hours'])} h  =>  {f(s['trades_per_hour'], 1)} trades/hour   (target ~100; measured, not forced)",
        f"Wins {s['wins']}  Losses {s['losses']}  Win rate {f(None if s['win_rate'] is None else s['win_rate'] * 100, 0)}%  "
        f"Profit factor {f(s['profit_factor'])}  Avg hold {f(s['avg_hold_s'], 1)} s",
        f"GROSS {s['gross']:+.4f}  FEES {s['fees']:.4f}  est. spread+slippage {s['friction_est']:.4f}  NET {s['net']:+.4f} USDT"
        f"  (avg net {f(s['avg_net_bps'], 1)} bps/trade)",
        f"Equity {s['equity']:.4f} (start {s['initial_equity']:.4f})  Max drawdown {s['max_drawdown']:.2%}",
        f"Exits: {s['exits']}",
        f"Signals by strategy: {s['signals_by']}",
        f"Rejected: {s['rejects']}",
    ]
    for k, v in s.get("by_strategy", {}).items():
        lines.append(f"  {k}: entries {v.get('entries', 0)} closed {v.get('closed', 0)} wins {v.get('wins', 0)} net {v.get('net_x1e8', 0) / 1e8:+.4f}")
    if s.get("halt"):
        lines.append(f"*** HALTED: {s['halt']} ***")
    if s.get("pause_day"):
        lines.append("*** entries paused by the daily-loss limit until the next UTC day ***")
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Show the HFT paper scalper's live statistics."

    def add_arguments(self, parser):
        parser.add_argument("--watch", type=float, default=0)

    def handle(self, *args, **o):
        p = runtime.status_path(Path(settings.BASE_DIR))
        while True:
            if not p.exists():
                self.stdout.write("No status yet: start it with  python manage.py run_hft_scalper")
            else:
                self.stdout.write(render(json.loads(p.read_text())) + "\n")
            if not o["watch"]:
                return
            time.sleep(o["watch"])
