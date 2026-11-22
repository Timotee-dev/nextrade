"""Stop the paper scalper safely: processes finish their cycle, then exit. Open paper positions stay recorded."""

from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.trading import supervisor


class Command(BaseCommand):
    help = "Ask the running paper scalper to stop."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=float, default=60.0)

    def handle(self, *args, **o):
        base = Path(settings.BASE_DIR)
        if not supervisor.is_running(base):
            self.stdout.write("The paper scalper is not running.")
            supervisor.stop_path(base).unlink(missing_ok=True)
            return
        supervisor.stop_path(base).parent.mkdir(parents=True, exist_ok=True)
        supervisor.stop_path(base).write_text("stop")
        self.stdout.write("Stop requested. Waiting for the processes to finish their cycle ...")
        end = time.monotonic() + o["wait"]
        while time.monotonic() < end:
            if not supervisor.lock_path(base).exists():
                self.stdout.write("Stopped. Restart any time with: python manage.py run_paper_scalper")
                return
            time.sleep(1)
        self.stdout.write("Still shutting down. If it does not end in a minute, close the window running it (Ctrl+C).")
