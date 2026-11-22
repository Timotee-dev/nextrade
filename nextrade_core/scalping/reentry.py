"""Re-entry guard: a closed position never reopens just because it closed.

Blocks a new entry on the same symbol until the cooldown passes, blocks the same strategy after
a loss streak, and refuses rapid side flips (noise). A new entry also needs a signal that is
NEWER than the one that opened the last trade. Pure and deterministic: time is passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReentryConfig:
    cooldown: timedelta = timedelta(minutes=15)           # per symbol after any close
    after_loss: timedelta = timedelta(minutes=45)         # per symbol after a losing close
    flip_cooldown: timedelta = timedelta(hours=1)         # opposite side on the same symbol
    loss_streak: int = 3                                  # strategy pauses after this many losses in a row
    streak_pause: timedelta = timedelta(hours=4)


@dataclass
class _Last:
    closed_at: datetime
    side: int
    pnl: float
    signal_time: datetime


@dataclass
class ReentryGuard:
    cfg: ReentryConfig = field(default_factory=ReentryConfig)
    _last: dict[str, _Last] = field(default_factory=dict)
    _streak: dict[str, int] = field(default_factory=dict)
    _paused_until: dict[str, datetime] = field(default_factory=dict)

    def record_close(self, symbol: str, strategy: str, side: int, pnl_net: float, when: datetime,
                     signal_time: datetime) -> None:
        self._last[symbol] = _Last(when, side, pnl_net, signal_time)
        self._streak[strategy] = self._streak.get(strategy, 0) + 1 if pnl_net < 0 else 0
        if self._streak[strategy] >= self.cfg.loss_streak:
            self._paused_until[strategy] = when + self.cfg.streak_pause
            self._streak[strategy] = 0

    def check(self, symbol: str, strategy: str, side: int, signal_time: datetime, now: datetime) -> tuple[bool, str]:
        until = self._paused_until.get(strategy)
        if until and now < until:
            return False, f"{strategy} paused after a loss streak until {until:%H:%M}"
        last = self._last.get(symbol)
        if last is None:
            return True, "no previous trade on this symbol"
        if signal_time <= last.signal_time:
            return False, "signal is not newer than the one behind the last trade"
        wait = self.cfg.after_loss if last.pnl < 0 else self.cfg.cooldown
        if side != last.side:
            wait = max(wait, self.cfg.flip_cooldown)
        if now < last.closed_at + wait:
            return False, f"cooling down until {(last.closed_at + wait):%H:%M}"
        return True, "cooldown over and a newer signal"
