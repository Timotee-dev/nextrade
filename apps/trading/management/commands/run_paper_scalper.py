"""Start NEXTRADE's LIVE-MARKET PAPER scalper: real prices, simulated money, no real orders.

    python manage.py run_paper_scalper                  # start (or resume) continuous paper scalping
    python manage.py run_paper_scalper --check          # run every pre-flight check, start nothing
    python manage.py paper_status --watch 10            # watch it (another window)
    python manage.py stop_paper_scalper                 # stop it safely (or press Ctrl+C here)

One command starts and supervises three processes: the market recorder (public Bybit REST: 1m candles and
bid/ask), the paper trader (six pre-registered scalp strategies on 5m bars, spot, long only) and, when
Telegram is configured, the notifier. A crashed process is restarted with backoff. Positions are stored
in the database, so a restart reconciles with them and never opens the same signal twice.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from adapters.bybit.client import BybitClient
from apps.market.store import CandleStore
from apps.trading import paper_runner, paper_status, supervisor
from apps.trading.models import TradingRun
from nextrade_core.scalping import paper_safety
from nextrade_core.strategies.registry import SCALP_PREREGISTERED


class Command(BaseCommand):
    help = "Start and supervise the live-market paper scalper (simulated money only)."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default="bybit", choices=["bybit"])
        parser.add_argument("--timeframe", default="5m")
        parser.add_argument("--capital-ngn", default="40000")
        parser.add_argument("--ngn-per-usdt", default="1480", help="ASSUMED rate, only used to size the paper account")
        parser.add_argument("--risk-per-trade", default="0.001")
        parser.add_argument("--max-positions", default="5")
        parser.add_argument("--max-exposure", default="0.80")
        parser.add_argument("--max-market-exposure", default="0.30")
        parser.add_argument("--check", action="store_true", help="pre-flight only: start nothing")
        parser.add_argument("--status-every", type=float, default=60.0)
        parser.add_argument("--max-seconds", type=float, default=0, help="stop by itself after N seconds (testing)")
        parser.add_argument("--new-run", action="store_true", help="start a fresh paper account instead of resuming")

    # ------------------------------------------------------------------ helpers
    def _specs(self, o, base: Path, symbols: list[str], strategies: list[str], telegram_ok: bool):
        py, stop = sys.executable, str(supervisor.stop_path(base))
        specs = [
            supervisor.ChildSpec("recorder", [py, "manage.py", "record_rest", "--venue", o["venue"], "--interval", "20",
                                              "--symbols", ",".join(symbols), "--stop-file", stop]),
            supervisor.ChildSpec("trader", [py, "manage.py", "run_trader", "--venue", o["venue"], "--timeframe", o["timeframe"],
                                            "--strategies", ",".join(strategies), "--capital-ngn", o["capital_ngn"],
                                            "--ngn-per-usdt", o["ngn_per_usdt"], "--risk-per-trade", o["risk_per_trade"],
                                            "--max-positions", o["max_positions"], "--max-exposure", o["max_exposure"],
                                            "--max-market-exposure", o["max_market_exposure"], "--poll", "5",
                                            "--stop-file", stop] + (["--new-run"] if o["new_run"] else [])),
        ]
        if telegram_ok:
            specs.append(supervisor.ChildSpec("telegram", [py, "manage.py", "run_telegram_bot"], optional=True))
        return specs

    def _spawner(self, base: Path):
        logs = base / "logs" / "paper"
        logs.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        def spawn(spec):
            f = open(logs / f"{spec.name}.log", "ab", buffering=0)
            f.write(f"\n--- {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC start ---\n".encode())
            return subprocess.Popen(spec.argv, cwd=base, env=env, stdout=f, stderr=subprocess.STDOUT)
        return spawn

    # ------------------------------------------------------------------ main
    def handle(self, *args, **o):
        base = Path(settings.BASE_DIR)
        logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per request would drown the log
        paper_safety.install_write_guard()
        now = datetime.now(UTC)
        self.stdout.write("NEXTRADE live-market PAPER scalper. Real prices, simulated money. No real orders can be sent.")
        call_command("migrate", interactive=False, verbosity=0)
        client = BybitClient()
        try:
            checks, info = paper_runner.preflight(base, o["venue"], client, now)
            self.stdout.write("Pre-flight:\n" + paper_runner.report(checks))
            if not all(ok for _, ok, _ in checks):
                raise CommandError("Pre-flight failed: nothing was started. Fix the FAIL lines above.")
            inst = info["instruments"]
            self.stdout.write("Catching up missing 1-minute candles from the live feed ...")
            bf = paper_runner.backfill(client, CandleStore(inst, run_label="paper_launcher"), inst, now)
            self.stdout.write(f"  fetched {bf['bars']} bars; {bf['failed']} markets failed {bf['errors'] or ''}")
            fr = paper_runner.freshness(inst, datetime.now(UTC))
            self.stdout.write(f"  data fresh and complete for {fr['fresh']} of {fr['total']} markets right now"
                              + (f" (waiting on {', '.join(fr['stale'][:6])})" if fr["stale"] else ""))
        finally:
            client.close()

        tg_ok, tg_msg = paper_runner.telegram_status()
        self.stdout.write(f"  Telegram notifications: {tg_msg}")
        strategies = [c for c in info["strategies"] if SCALP_PREREGISTERED[c] == o["timeframe"]]
        skipped = [c for c in info["strategies"] if c not in strategies]
        self.stdout.write(f"Strategies running on {o['timeframe']}: {', '.join(strategies)}")
        for c in skipped:
            self.stdout.write(self.style.WARNING(f"  not running: {c} (designed for {SCALP_PREREGISTERED[c]} bars; needs its own process)"))
        self.stdout.write(self.style.WARNING("These strategies have NOT passed validation. This is an exploratory paper trial: "
                                             "its results are evidence-gathering, not proof of profit."))
        if o["check"]:
            self.stdout.write("Check complete. Nothing started. Start with: python manage.py run_paper_scalper")
            return

        stop = supervisor.stop_path(base)
        try:
            supervisor.acquire_lock(base)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        stop.unlink(missing_ok=True)
        n = paper_runner.clear_old_alerts(datetime.now(UTC))
        if n:
            self.stdout.write(f"  cleared {n} old undelivered alerts so your phone is not flooded")
        sup = supervisor.Supervisor(self._specs(o, base, sorted(inst), strategies, tg_ok), self._spawner(base),
                                    on_event=lambda m: self.stdout.write(f"[{datetime.now(UTC):%H:%M:%S}] {m}"))
        started, last_status = time.monotonic(), 0.0
        self.stdout.write("Running. Watch it:  python manage.py paper_status --watch 10   Stop it:  Ctrl+C here, or  "
                          "python manage.py stop_paper_scalper")
        try:
            while True:
                sup.tick()
                supervisor.refresh_lock(base)
                if stop.exists():
                    self.stdout.write("Stop requested.")
                    break
                if o["max_seconds"] and time.monotonic() - started > o["max_seconds"]:
                    break
                if time.monotonic() - last_status >= o["status_every"]:
                    last_status = time.monotonic()
                    self._print_status(o)
                time.sleep(2)
        except KeyboardInterrupt:
            self.stdout.write("Ctrl+C: stopping safely ...")
        finally:
            stop.parent.mkdir(parents=True, exist_ok=True)
            stop.write_text("stop")                     # tells the recorder and the trader to exit cleanly
            sup.shutdown()
            supervisor.release_lock(base)
            stop.unlink(missing_ok=True)
        self.stdout.write("Stopped. Open positions stay recorded and are reconciled on the next start.")

    def _print_status(self, o) -> None:
        try:
            run = TradingRun.objects.filter(mode="paper").order_by("-id").first()
            if run is not None:
                self.stdout.write(paper_status.render(paper_status.collect(run, datetime.now(UTC), o["venue"])))
        except Exception as exc:       # noqa: BLE001  monitoring must never stop trading
            self.stdout.write(f"(status unavailable: {exc})")
