"""Independent watchdog (spec 6.7, 9): runs as its own process, depends on nothing but
the database and (in live mode) the venue.

* Trader heartbeat older than `max_silence` while trading -> EMERGENCY + critical alert.
* Live mode: every open position must have a working protective exit at the venue;
  missing ones are re-placed (kill-switch level 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

ACTIVE = {"paper", "live", "paused"}


@dataclass(frozen=True)
class WatchdogVerdict:
    escalate: bool
    reason: str = ""


def check_heartbeat(last_beat: datetime | None, now: datetime, trading_state: str | None,
                    max_silence: timedelta = timedelta(seconds=60)) -> WatchdogVerdict:
    if trading_state not in ACTIVE:
        return WatchdogVerdict(False)
    if last_beat is None:
        return WatchdogVerdict(True, "trader has never sent a heartbeat while trading")
    silence = now - last_beat
    if silence > max_silence:
        return WatchdogVerdict(True, f"trader silent for {int(silence.total_seconds())}s")
    return WatchdogVerdict(False)
