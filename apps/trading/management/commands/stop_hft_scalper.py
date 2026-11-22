"""Stop the HFT paper scalper. Open paper positions are saved and resume on the next start."""

from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from services.hft import runtime


class Command(BaseCommand):
    help = "Ask the running HFT paper scalper to stop."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=float, default=30.0)

    def handle(self, *args, **o):
        base = Path(settings.BASE_DIR)
        lock = runtime.lock_path(base)
        if not lock.exists():
            self.stdout.write("The HFT scalper is not running.")
            return
        runtime.stop_path(base).parent.mkdir(parents=True, exist_ok=True)
        runtime.stop_path(base).write_text("stop")
        end = time.monotonic() + o["wait"]
        while time.monotonic() < end:
            if not lock.exists():
                self.stdout.write("Stopped.")
                return
            time.sleep(0.5)
        self.stdout.write("Still shutting down; close the window running it if it does not end soon.")
