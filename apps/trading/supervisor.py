"""Keep the paper scalper's processes alive: start them, restart them when they die, stop them cleanly.

Pure process logic with injected `spawn` and `clock`, so it is tested without launching anything.
Restart delays grow 5, 10, 20 ... 120 s for a process that keeps dying quickly, and reset once it has run
for 10 minutes. An optional process (the Telegram bot) that fails 3 times in a row is switched off, with the
reason shown, instead of being restarted forever.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

FAST_RUN_S = 60.0
STABLE_RUN_S = 600.0
MAX_DELAY_S = 120.0
LOCK_STALE_S = 15.0


@dataclass
class ChildSpec:
    name: str
    argv: list[str]
    optional: bool = False


@dataclass
class Child:
    spec: ChildSpec
    proc: object | None = None
    started_at: float = 0.0
    start_count: int = 0
    fast_failures: int = 0
    next_start: float = 0.0
    disabled: str = ""
    last_exit: int | None = None


@dataclass
class Supervisor:
    specs: list[ChildSpec]
    spawn: Callable[[ChildSpec], object]
    clock: Callable[[], float] = time.monotonic
    on_event: Callable[[str], None] = print
    children: dict[str, Child] = field(init=False)

    def __post_init__(self) -> None:
        self.children = {s.name: Child(s) for s in self.specs}

    def tick(self) -> None:
        now = self.clock()
        for c in self.children.values():
            if c.disabled:
                continue
            if c.proc is None:
                if now >= c.next_start:
                    c.proc, c.started_at = self.spawn(c.spec), now
                    c.start_count += 1
                    self.on_event(f"started {c.spec.name}" + (f" (restart #{c.start_count - 1})" if c.start_count > 1 else ""))
                continue
            code = c.proc.poll()
            if code is None:
                if c.fast_failures and now - c.started_at >= STABLE_RUN_S:
                    c.fast_failures = 0
                continue
            ran = now - c.started_at
            c.proc, c.last_exit = None, code
            c.fast_failures = c.fast_failures + 1 if ran < FAST_RUN_S else 0
            if c.spec.optional and c.fast_failures >= 3:
                c.disabled = f"{c.spec.name} failed {c.fast_failures} times in a row (exit {code}); see its log"
                self.on_event(f"DISABLED {c.disabled}")
                continue
            delay = min(MAX_DELAY_S, 5.0 * 2 ** max(0, c.fast_failures - 1)) if c.fast_failures else 5.0
            c.next_start = now + delay
            self.on_event(f"{c.spec.name} exited (code {code}) after {ran:.0f}s; restarting in {delay:.0f}s")

    def alive(self) -> dict[str, bool]:
        return {n: c.proc is not None and c.proc.poll() is None for n, c in self.children.items()}

    def shutdown(self, grace_s: float = 25.0, sleep: Callable[[float], None] = time.sleep) -> None:
        """Children were asked to stop through the stop file; give them `grace_s`, then terminate, then kill."""
        end = self.clock() + grace_s
        while self.clock() < end and any(self.alive().values()):
            sleep(0.5)
        for c in self.children.values():
            if c.proc is not None and c.proc.poll() is None:
                c.proc.terminate()
        end = self.clock() + 5
        while self.clock() < end and any(self.alive().values()):
            sleep(0.2)
        for c in self.children.values():
            if c.proc is not None and c.proc.poll() is None:
                c.proc.kill()


# ---------------------------------------------------------------- lock: one supervisor at a time
def lock_path(base: Path) -> Path:
    return base / "run" / "paper_scalper.lock"


def stop_path(base: Path) -> Path:
    return base / "run" / "STOP"


def acquire_lock(base: Path, now: float | None = None) -> None:
    p, t = lock_path(base), time.time() if now is None else now
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            held = json.loads(p.read_text())
        except ValueError:
            held = {}
        if t - float(held.get("ts", 0)) < LOCK_STALE_S:
            raise RuntimeError(f"another paper scalper is already running (pid {held.get('pid')}); "
                               "stop it first with: python manage.py stop_paper_scalper")
    p.write_text(json.dumps({"pid": os.getpid(), "ts": t}))


def refresh_lock(base: Path, now: float | None = None) -> None:
    lock_path(base).write_text(json.dumps({"pid": os.getpid(), "ts": time.time() if now is None else now}))


def release_lock(base: Path) -> None:
    lock_path(base).unlink(missing_ok=True)


def is_running(base: Path, now: float | None = None) -> bool:
    p = lock_path(base)
    if not p.exists():
        return False
    try:
        return (time.time() if now is None else now) - float(json.loads(p.read_text()).get("ts", 0)) < LOCK_STALE_S
    except ValueError:
        return False
