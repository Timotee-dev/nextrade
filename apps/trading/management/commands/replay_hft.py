"""Replay recorded real tape (data/ticks/*.jsonl.gz) through the HFT engine, offline, with the same costs and rules.

    python manage.py replay_hft                      # all recorded files
    python manage.py replay_hft --last-hours 6 --max-positions 8

Use it to evaluate strategy changes on the tape the live paper run recorded. Replay results on the same tape that produced a
change are in-sample: judge a change on tape recorded AFTER it was made.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nextrade_core.hft.engine import HftConfig
from nextrade_core.hft.market import SymbolState
from services.hft import runtime


class Command(BaseCommand):
    help = "Replay recorded ticks through the HFT engine."

    def add_arguments(self, parser):
        parser.add_argument("--folder", default="")
        parser.add_argument("--last-hours", type=int, default=0)
        parser.add_argument("--cash", type=float, default=27.0)
        parser.add_argument("--max-positions", type=int, default=12)
        parser.add_argument("--fee", type=float, default=0.001)

    def handle(self, *args, **o):
        folder = Path(o["folder"]) if o["folder"] else Path(settings.BASE_DIR) / "data" / "ticks"
        files = sorted(folder.glob("ticks-*.jsonl.gz"))
        if o["last_hours"]:
            files = files[-o["last_hours"]:]
        if not files:
            raise CommandError(f"no tick files in {folder}: run the live scalper first (it records the tape)")
        evs = (e for f in files for e in runtime.read_ticks(f))
        engine = None
        for ev in evs:
            if engine is None:
                engine = runtime.build_engine(HftConfig(max_positions=o["max_positions"]), [], {}, o["cash"], lambda r: None,
                                              o["fee"], o["fee"])
            if ev.symbol not in engine.market.state:
                engine.market.state[ev.symbol] = SymbolState(ev.symbol)
            engine.on_event(ev)
            if engine.last_ts and int(engine.last_ts * 4) != getattr(self, "_q", 0):
                self._q = int(engine.last_ts * 4)
                engine.on_timer(engine.last_ts)
        if engine is None:
            raise CommandError("tick files were empty")
        self.stdout.write(json.dumps(engine.summary(), indent=2, default=str))
