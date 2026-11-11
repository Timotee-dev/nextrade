"""Kill-switch levels (spec 9): what to do, in what order, for each level.

Level 4 (close positions) is only planned when the venue state is VERIFIED and closing
is safer than holding. A network outage never triggers blind closing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class KillLevel(IntEnum):
    BLOCK_ENTRIES = 1
    CANCEL_ENTRIES = 2
    ENSURE_STOPS = 3
    CLOSE_POSITIONS = 4


@dataclass(frozen=True)
class KillAction:
    action: str
    target: str = ""


def plan_actions(level: KillLevel, *, venue_state_verified: bool, working_entry_orders: list[str],
                 positions_missing_stop: list[str], positions: list[str],
                 closing_safer_than_holding: bool = False) -> list[KillAction]:
    actions = [KillAction("block_new_entries")]
    if level >= KillLevel.CANCEL_ENTRIES:
        actions += [KillAction("cancel_order", oid) for oid in working_entry_orders]
    if level >= KillLevel.ENSURE_STOPS:
        if not venue_state_verified:
            actions.append(KillAction("reconcile_with_venue"))
        actions += [KillAction("place_protective_stop", s) for s in positions_missing_stop]
    if level >= KillLevel.CLOSE_POSITIONS:
        if venue_state_verified and closing_safer_than_holding:
            actions += [KillAction("close_position", s) for s in positions]
        else:
            actions.append(KillAction("hold_positions_with_stops",
                                      "venue state unverified" if not venue_state_verified else "holding is safer"))
    return actions
