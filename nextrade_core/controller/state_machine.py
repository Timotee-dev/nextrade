"""Central Controller trading-state machine (spec part 3.1).

Pure logic: no I/O. The Django layer persists each Transition through a recorder
callback. Rules that matter for safety:

* LIVE can only be entered when reconciliation passed, the strategy version is
  promoted to live, and the operator confirmed in this request.
* Leaving RISK_SHUTDOWN or EMERGENCY needs an operator acknowledgement. NARA and
  the watchdog can put the system into these states but can never take it out.
* Any active state can drop to PAUSED, RISK_SHUTDOWN or EMERGENCY.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from nextrade_core.clock import Clock, WallClock


class TradingState(StrEnum):
    STOPPED = "stopped"
    DATA_INIT = "data_init"
    ANALYSIS = "analysis"
    PAPER = "paper"
    LIVE = "live"
    PAUSED = "paused"
    RISK_SHUTDOWN = "risk_shutdown"
    EMERGENCY = "emergency"


class Actor(StrEnum):
    OPERATOR = "operator"
    SYSTEM = "system"
    NARA = "nara"
    WATCHDOG = "watchdog"


S = TradingState

_ALLOWED: dict[TradingState, set[TradingState]] = {
    S.STOPPED: {S.DATA_INIT},
    S.DATA_INIT: {S.ANALYSIS, S.STOPPED},
    S.ANALYSIS: {S.PAPER, S.LIVE, S.STOPPED},
    S.PAPER: {S.ANALYSIS, S.STOPPED},
    S.LIVE: {S.ANALYSIS},
    S.PAUSED: {S.ANALYSIS, S.PAPER, S.LIVE, S.STOPPED},
    S.RISK_SHUTDOWN: {S.ANALYSIS, S.STOPPED},
    S.EMERGENCY: {S.ANALYSIS, S.STOPPED},
}
_HALT_STATES = {S.PAUSED, S.RISK_SHUTDOWN, S.EMERGENCY}
_LATCHED = {S.RISK_SHUTDOWN, S.EMERGENCY}
_SEVERITY = {S.PAUSED: 1, S.RISK_SHUTDOWN: 2, S.EMERGENCY: 3}


class TransitionError(Exception):
    """A requested transition is not allowed. Nothing changed."""


@dataclass(frozen=True, slots=True)
class LiveGate:
    reconciled: bool = False
    strategy_promoted: bool = False
    operator_confirmed: bool = False

    def missing(self) -> list[str]:
        return [name for name in ("reconciled", "strategy_promoted", "operator_confirmed")
                if not getattr(self, name)]


@dataclass(frozen=True, slots=True)
class Transition:
    from_state: TradingState
    to_state: TradingState
    actor: Actor
    reason: str
    at: datetime
    context: dict = field(default_factory=dict)


Recorder = Callable[[Transition], None]


class TradingStateMachine:
    def __init__(
        self,
        recorder: Recorder | None = None,
        clock: Clock | None = None,
        initial: TradingState = S.STOPPED,
    ) -> None:
        self._state = initial
        self._recorder = recorder
        self._clock = clock or WallClock()

    @property
    def state(self) -> TradingState:
        return self._state

    @property
    def allows_new_entries(self) -> bool:
        return self._state in (S.PAPER, S.LIVE)

    def transition(
        self,
        to: TradingState,
        actor: Actor,
        reason: str,
        *,
        live_gate: LiveGate | None = None,
        acknowledged: bool = False,
        context: dict | None = None,
    ) -> Transition:
        current = self._state
        if not reason.strip():
            raise TransitionError("every transition needs a reason")
        if to == current:
            raise TransitionError(f"already in {current}")

        if to in _HALT_STATES:
            self._check_halt(current, to, actor)
        else:
            if to not in _ALLOWED[current]:
                raise TransitionError(f"{current} -> {to} is not an allowed transition")
            if current in _LATCHED:
                if actor is not Actor.OPERATOR or not acknowledged:
                    raise TransitionError(
                        f"leaving {current} requires an operator acknowledgement"
                    )
            if current is S.PAUSED and actor is not Actor.OPERATOR:
                raise TransitionError("only the operator can resume from PAUSED")
            if to is S.LIVE:
                if actor is not Actor.OPERATOR:
                    raise TransitionError("only the operator can enable LIVE")
                missing = (live_gate or LiveGate()).missing()
                if missing:
                    raise TransitionError(f"LIVE blocked, missing: {', '.join(missing)}")

        record = Transition(current, to, actor, reason, self._clock.now(), dict(context or {}))
        if self._recorder is not None:
            self._recorder(record)  # persist first; if it raises, state is unchanged
        self._state = to
        return record

    def _check_halt(self, current: TradingState, to: TradingState, actor: Actor) -> None:
        if current is S.STOPPED:
            raise TransitionError("system is stopped; nothing to halt")
        if current in _HALT_STATES and _SEVERITY[to] <= _SEVERITY[current]:
            raise TransitionError(f"{to} would not escalate from {current}")
        if to is S.PAUSED and actor not in (Actor.OPERATOR, Actor.NARA):
            raise TransitionError("only the operator or NARA can pause")
        if to is S.RISK_SHUTDOWN and actor not in (Actor.NARA, Actor.WATCHDOG, Actor.OPERATOR):
            raise TransitionError("risk shutdown comes from NARA, watchdog or operator")
