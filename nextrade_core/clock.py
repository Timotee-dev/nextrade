"""Clock abstraction.

Every component asks a Clock for the time. Backtests use SimClock, which only
moves forward, so code cannot accidentally read a bar that has not closed yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    def __init__(self, start: datetime) -> None:
        _require_utc(start)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        _require_utc(when)
        if when < self._now:
            raise ValueError(f"SimClock cannot go backwards: {when} < {self._now}")
        self._now = when


def _require_utc(when: datetime) -> None:
    if when.tzinfo is None or when.utcoffset() != UTC.utcoffset(when):
        raise ValueError("timestamps must be timezone-aware UTC")
