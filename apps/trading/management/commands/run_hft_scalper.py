"""Start NEXTRADE's HIGH-FREQUENCY live-market PAPER scalper: real Bybit WebSocket prices, simulated money, no real orders.

    python manage.py run_hft_scalper --check        # pre-flight only
    python manage.py run_hft_scalper                # start
    python manage.py hft_status --watch 5           # watch (another window)
    python manage.py stop_hft_scalper               # stop (or Ctrl+C here)

One process, event driven: every order-book and trade update of every market is handled the moment it arrives. Many positions are
open at once, each managed on every tick. It does NOT promise 100 trades an hour: it is built for that rate and MEASURES what it gets.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from django import db
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from adapters.bybit.client import BybitClient, pick_top_usdt
from adapters.bybit.ws import ALLOWED_OPS, ForbiddenWsOperation, check_op
from apps.trading import paper_runner, supervisor
from apps.trading.models import Alert
from nextrade_core.hft.engine import HftConfig
from nextrade_core.hft.exchange import InstrumentRules
from nextrade_core.scalping import paper_safety
from services.hft import runtime


def ws_guard_check() -> tuple[str, bool, str]:
    try:
        check_op({"op": "order.create"})
        return ("websocket allow-list blocks order operations", False, "order.create was NOT blocked")
    except ForbiddenWsOperation:
        return ("websocket allow-list blocks order operations", True, f"only {sorted(ALLOWED_OPS)} can ever be sent")


_alert_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hft-alert")


def _write_alert(level: str, kind: str, msg: str) -> None:
    try:
        Alert.objects.create(level=level, kind=kind, message=msg)
    except Exception as exc:             # noqa: BLE001  a notification problem must never stop trading
        logging.getLogger("hft").warning("could not queue alert: %s", exc)
    finally:
        db.connections.close_all()


def notify(level: str, kind: str, msg: str) -> None:
    """Queue an alert. Django's ORM may not be called from the asyncio thread, so the write runs on its own worker thread."""
    _alert_pool.submit(_write_alert, level, kind, msg)


class Command(BaseCommand):
    help = "Start the high-frequency live-market paper scalper (simulated money only)."

    def add_arguments(self, parser):
        parser.add_argument("--capital-ngn", type=float, default=40000)
        parser.add_argument("--ngn-per-usdt", type=float, default=1480, help="ASSUMED rate, only sizes the paper account")
        parser.add_argument("--markets", type=int, default=40, help="most-traded USDT spot markets to scan")
        parser.add_argument("--max-positions", type=int, default=12)
        parser.add_argument("--pos-frac", type=float, default=0.08)
        parser.add_argument("--max-exposure", type=float, default=0.90)
        parser.add_argument("--fee", type=float, default=0.001, help="fee per side (taker and maker), Bybit spot default 0.1%%")
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--max-seconds", type=float, default=0)
        parser.add_argument("--summary-every", type=float, default=600.0)
        parser.add_argument("--digest-every", type=float, default=5.0, help="seconds between Telegram status lines (0 = off)")
        parser.add_argument("--target-mult", type=float, default=1.0, help="scale profit targets and stops (from tune_hft)")
        parser.add_argument("--hold-mult", type=float, default=1.0)
        parser.add_argument("--min-vol-bps", type=float, default=0.0, help="skip quiet markets")
        parser.add_argument("--enabled", default="", help="comma list of strategies allowed to trade (default all)")
        parser.add_argument("--max-entries-per-day", type=int, default=0, help="daily cap on new positions (0 = none)")
        parser.add_argument("--clear-halt", action="store_true", help="release a latched drawdown halt (keeps cash and history)")
        parser.add_argument("--new-account", action="store_true", help="ignore the saved state and start from the full capital")

    @staticmethod
    def _disable_quickedit() -> None:
        """Windows console 'QuickEdit' freezes the program whenever the window is clicked. Turn it off for this window."""
        if os.name != "nt":
            return
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
        except Exception:                         # noqa: BLE001
            pass

    def handle(self, *args, **o):
        base = Path(settings.BASE_DIR)
        self._disable_quickedit()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        paper_safety.install_write_guard()
        self.stdout.write("NEXTRADE high-frequency PAPER scalper. Real prices, simulated money. No real orders can be sent.")
        call_command("migrate", interactive=False, verbosity=0)
        checks = list(paper_safety.safety_checks(base)) + [ws_guard_check()]
        if supervisor.is_running(base):
            checks.append(("the 5-minute paper scalper is stopped", False, "run: python manage.py stop_paper_scalper first"))
        try:
            import websockets  # noqa: F401
            checks.append(("websockets library installed", True, "ok"))
        except ImportError:
            checks.append(("websockets library installed", False, "run: pip install websockets"))
        client = BybitClient()
        try:
            tickers = client.tickers()
            top = [t["symbol"] for t in pick_top_usdt(tickers, o["markets"])]
            checks.append(("live feed reachable (public REST) and markets found", len(top) >= 5, f"{len(top)} markets"))
            rules = {i.symbol: InstrumentRules(float(i.step_size), float(i.min_qty), float(i.min_notional))
                     for i in client.instruments(top)}
            top = [s for s in top if s in rules]
        except Exception as exc:       # noqa: BLE001
            checks.append(("live feed reachable (public REST)", False, f"{exc}"))
            top, rules = [], {}
        finally:
            client.close()
        self.stdout.write("Pre-flight:\n" + paper_runner.report(checks))
        if not all(ok for _, ok, _ in checks):
            raise CommandError("Pre-flight failed: nothing was started. Fix the FAIL lines above.")
        tg_ok, tg_msg = paper_runner.telegram_status()
        self.stdout.write(f"  Telegram notifications: {tg_msg}")
        cash = round(o["capital_ngn"] / o["ngn_per_usdt"], 4)
        cfg = HftConfig(max_positions=o["max_positions"], pos_frac=o["pos_frac"], max_exposure=o["max_exposure"],
                        target_mult=o["target_mult"], hold_mult=o["hold_mult"], min_vol_bps=o["min_vol_bps"],
                        enabled=tuple(x for x in o["enabled"].split(",") if x), max_entries_per_day=o["max_entries_per_day"])
        self.stdout.write(f"Scanning {len(top)} markets over one WebSocket. Paper account {cash} USDT "
                          f"(₦{o['capital_ngn']:,.0f} at an ASSUMED {o['ngn_per_usdt']:.0f} ₦/USDT). Fee {o['fee']:.2%} per side.")
        self.stdout.write(self.style.WARNING("The four fast strategies are NEW and UNVALIDATED. This is an exploratory paper trial that "
                                             "records real tape for later validation. It is not evidence of profit."))
        if o["check"]:
            self.stdout.write("Check complete. Nothing started. Start with: python manage.py run_hft_scalper")
            return

        if o["clear_halt"] and runtime.state_path(base).exists():
            import json
            snap = json.loads(runtime.state_path(base).read_text())
            snap.update(halt="", pause_day=0, max_dd=0.0)
            if not any(v > 1e-12 for v in snap.get("holdings", {}).values()):
                snap["peak_equity"] = snap["cash"]
            runtime.atomic_write(runtime.state_path(base), json.dumps(snap))
            self.stdout.write("Halt released (cash and history kept).")
        lock = runtime.lock_path(base)
        if lock.exists() and time.time() - lock.stat().st_mtime < 120:
            raise CommandError("another HFT scalper is running (stop it: python manage.py stop_hft_scalper)")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
        runtime.stop_path(base).unlink(missing_ok=True)
        paper_runner.clear_old_alerts(datetime.now(UTC))
        tg = None
        notifier = None
        if tg_ok:
            from apps.notify.management.commands.run_telegram_bot import settings_from_env
            from apps.notify.telegram import TelegramClient
            from services.hft.notifier import TelegramNotifier
            token, chats, _pin = settings_from_env()
            client_tg = TelegramClient(token)
            notifier = TelegramNotifier(client_tg.send, set(chats))
            notifier.push("NEXTRADE HFT paper scalper started (simulated money, real prices). "
                          f"{len(top)} markets, account {cash} USDT. You will get a message for every open and close, "
                          f"and a status line every {o['digest_every']:g} s.")
        if tg_ok:
            logs = base / "logs" / "hft"
            logs.mkdir(parents=True, exist_ok=True)
            tg = subprocess.Popen([sys.executable, "manage.py", "run_telegram_bot"], cwd=base,
                                  stdout=open(logs / "telegram.log", "ab", buffering=0), stderr=subprocess.STDOUT)
        self.stdout.write("Running. Watch:  python manage.py hft_status --watch 5   Stop:  Ctrl+C or  python manage.py stop_hft_scalper")
        started, backoff = time.monotonic(), 2.0

        async def heartbeat():
            while True:
                lock.write_text(str(os.getpid()))
                await asyncio.sleep(10)

        async def main():
            hb = asyncio.create_task(heartbeat())
            try:
                left = o["max_seconds"]
                while not runtime.stop_path(base).exists():
                    t0 = time.monotonic()
                    try:
                        await runtime.run(base, top, rules, cash, cfg, notify=notify, summary_every_s=o["summary_every"],
                                          max_seconds=left, resume=not o["new_account"],
                                          telegram=notifier, digest_every_s=o["digest_every"])
                        break
                    except Exception:                      # noqa: BLE001  restart from the saved state
                        logging.getLogger("hft").exception("engine crashed; restarting")
                        notify("warning", "hft_restart", "HFT engine crashed and was restarted from its saved state")
                        await asyncio.sleep(backoff)
                    if o["max_seconds"]:
                        left = max(1.0, o["max_seconds"] - (time.monotonic() - started))
            finally:
                hb.cancel()

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            self.stdout.write("Ctrl+C: stopped. Open paper positions are saved and resume on the next start.")
        finally:
            if notifier:
                notifier.push("HFT paper scalper stopped. Open paper positions are saved.")
                notifier.close()
            if tg:
                tg.terminate()
            lock.unlink(missing_ok=True)
            runtime.stop_path(base).unlink(missing_ok=True)
        self.stdout.write("Stopped.")
