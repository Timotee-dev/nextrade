from datetime import UTC, datetime

import pytest

from nextrade_core.clock import SimClock
from nextrade_core.controller.state_machine import (
    Actor, LiveGate, TradingState as S, TradingStateMachine, TransitionError,
)

OP, NARA, WD, SYS = Actor.OPERATOR, Actor.NARA, Actor.WATCHDOG, Actor.SYSTEM
FULL_GATE = LiveGate(reconciled=True, strategy_promoted=True, operator_confirmed=True)


def machine(state=S.STOPPED, log=None):
    clock = SimClock(datetime(2026, 11, 11, tzinfo=UTC))
    return TradingStateMachine(recorder=(log.append if log is not None else None), clock=clock, initial=state)


def test_normal_start_to_paper_is_recorded():
    log = []
    m = machine(log=log)
    m.transition(S.DATA_INIT, SYS, "start")
    m.transition(S.ANALYSIS, SYS, "data ready")
    m.transition(S.PAPER, OP, "begin paper run")
    assert m.state is S.PAPER and m.allows_new_entries
    assert [t.to_state for t in log] == [S.DATA_INIT, S.ANALYSIS, S.PAPER]
    assert all(t.reason for t in log)


def test_cannot_skip_states():
    with pytest.raises(TransitionError):
        machine().transition(S.PAPER, OP, "skip")


@pytest.mark.parametrize("gate", [
    LiveGate(),
    LiveGate(reconciled=True, strategy_promoted=True),
    LiveGate(reconciled=True, operator_confirmed=True),
    LiveGate(strategy_promoted=True, operator_confirmed=True),
])
def test_live_requires_every_gate(gate):
    m = machine(S.ANALYSIS)
    with pytest.raises(TransitionError, match="LIVE blocked"):
        m.transition(S.LIVE, OP, "go live", live_gate=gate)
    assert m.state is S.ANALYSIS


def test_live_only_by_operator():
    with pytest.raises(TransitionError):
        machine(S.ANALYSIS).transition(S.LIVE, SYS, "auto", live_gate=FULL_GATE)


def test_live_with_full_gate():
    m = machine(S.ANALYSIS)
    m.transition(S.LIVE, OP, "approved", live_gate=FULL_GATE)
    assert m.state is S.LIVE


@pytest.mark.parametrize("start", [S.ANALYSIS, S.PAPER, S.LIVE, S.DATA_INIT])
def test_nara_can_shut_down_any_active_state(start):
    m = machine(start)
    m.transition(S.RISK_SHUTDOWN, NARA, "weekly loss limit")
    assert m.state is S.RISK_SHUTDOWN and not m.allows_new_entries


def test_shutdown_is_latched_until_operator_acknowledges():
    m = machine(S.RISK_SHUTDOWN)
    with pytest.raises(TransitionError):
        m.transition(S.ANALYSIS, NARA, "auto recover")
    with pytest.raises(TransitionError):
        m.transition(S.ANALYSIS, OP, "resume")  # no acknowledgement
    m.transition(S.ANALYSIS, OP, "reviewed losses", acknowledged=True)
    assert m.state is S.ANALYSIS


def test_cannot_jump_from_shutdown_straight_to_live():
    with pytest.raises(TransitionError):
        machine(S.EMERGENCY).transition(S.LIVE, OP, "x", acknowledged=True, live_gate=FULL_GATE)


def test_halts_only_escalate():
    m = machine(S.LIVE)
    m.transition(S.PAUSED, OP, "lunch")
    m.transition(S.EMERGENCY, WD, "trader heartbeat lost")
    with pytest.raises(TransitionError):
        m.transition(S.RISK_SHUTDOWN, NARA, "would downgrade")
    with pytest.raises(TransitionError):
        m.transition(S.PAUSED, OP, "would downgrade")


def test_watchdog_cannot_pause_and_system_cannot_resume():
    with pytest.raises(TransitionError):
        machine(S.PAPER).transition(S.PAUSED, WD, "x")
    with pytest.raises(TransitionError):
        machine(S.PAUSED).transition(S.PAPER, SYS, "x")


def test_cannot_halt_when_stopped():
    with pytest.raises(TransitionError):
        machine(S.STOPPED).transition(S.EMERGENCY, WD, "x")


def test_reason_required():
    with pytest.raises(TransitionError):
        machine().transition(S.DATA_INIT, SYS, "  ")


def test_recorder_failure_leaves_state_unchanged():
    def broken(_):
        raise RuntimeError("db down")

    m = TradingStateMachine(recorder=broken)
    with pytest.raises(RuntimeError):
        m.transition(S.DATA_INIT, SYS, "start")
    assert m.state is S.STOPPED
