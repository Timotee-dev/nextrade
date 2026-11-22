"""Operating modes and strategy approvals: the only door to trading.

Modes: research (no orders), paper (simulated orders), live (real money, OFF by default).
A strategy may run in paper only if it is in the approvals file, and in live only if it was also
approved for live. Approvals are written by the OPERATOR command `approve_strategy`, which
checks the latest recorded decision-gate result. Nothing in the trading or AI code writes this
file, and live mode additionally needs NEXTRADE_LIVE_ENABLED=yes in the environment. There is no
silent transition: live never starts from paper without an operator running a separate command.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Mode(StrEnum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class Approval:
    strategy: str
    paper: bool
    live: bool
    evidence: str
    approved_at: str
    timeframe: str = ""          # the timeframe the evidence was gathered on ("" = any, older files)


def approvals_path(base: Path) -> Path:
    return base / "config" / "approved_strategies.json"


def load(base: Path) -> dict[str, Approval]:
    p = approvals_path(base)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    return {k: Approval(**v) for k, v in raw.items()}


def save(base: Path, approvals: dict[str, Approval]) -> Path:
    p = approvals_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: v.__dict__ for k, v in sorted(approvals.items())}, indent=2))
    return p


def allowed_strategies(base: Path, mode: Mode) -> list[str]:
    if mode is Mode.RESEARCH:
        return []                                   # research places no orders at all
    ap = load(base)
    return sorted(k for k, v in ap.items() if (v.live if mode is Mode.LIVE else v.paper))


def live_enabled(env: dict | None = None) -> bool:
    return (env if env is not None else os.environ).get("NEXTRADE_LIVE_ENABLED", "").lower() == "yes"


def require_mode(base: Path, mode: Mode, env: dict | None = None) -> list[str]:
    """Strategies permitted in `mode`; raises PermissionError with the reason otherwise."""
    if mode is Mode.LIVE and not live_enabled(env):
        raise PermissionError("live mode is disabled: NEXTRADE_LIVE_ENABLED is not 'yes'")
    if mode is Mode.RESEARCH:
        return []
    names = allowed_strategies(base, mode)
    if not names:
        raise PermissionError(f"no strategy is approved for {mode.value}: none has passed the decision gate "
                              "and been approved with `approve_strategy`")
    return names
