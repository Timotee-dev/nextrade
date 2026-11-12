"""Trade outcome simulation for labels (triple barrier: stop, target, time).

Rules (conservative, and shared with the backtester's assumptions):
* Entry at the NEXT bar's open, plus half-spread and slippage.
* If that open is already at or below the stop, or at or above the target, the
  trade is skipped: the plan no longer makes sense.
* Each bar: stop checked before target. A bar that touches both counts as a loss.
* A gap through the stop exits at the bar's open (worse than the stop).
* After max_hold bars the trade exits at that bar's close.
* A trade whose path runs past the end of the data is UNRESOLVED and excluded,
  never counted as a timeout (that would bias labels).
R is measured against the actual risk taken: fill price minus stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
import pandas as pd

from nextrade_core.costs.model import CostModel
from nextrade_core.strategies.base import Signal


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TIME = "time"


@dataclass(frozen=True)
class Outcome:
    entry_time: datetime
    exit_time: datetime          # open time of the exit bar
    fill: float
    exit_price: float
    reason: ExitReason
    bars_held: int
    r_gross: float
    r_net: float
    cost_r: float
    mfe_r: float                 # best unrealised move, in R
    mae_r: float                 # worst unrealised move, in R

    @property
    def hit_target(self) -> int:
        return int(self.reason is ExitReason.TARGET)


def simulate_exit(frame: pd.DataFrame, signal: Signal, costs: CostModel) -> Outcome | None:
    """Longs and shorts: `sg` is +1 for a long, -1 for a short, so `sg * (a - b) > 0` means
    a is better than b for the trade. A short's stop is touched by the bar's HIGH."""
    idx = frame.index.get_indexer([pd.Timestamp(signal.bar_time)])[0]
    if idx < 0 or idx + 1 >= len(frame):
        return None
    o, h, l, c = (frame[k].to_numpy() for k in ("open", "high", "low", "close"))
    sg = signal.sign
    adverse, favour = (l, h) if sg > 0 else (h, l)
    e = idx + 1
    fill = costs.entry_fill(signal.symbol, o[e], sign=sg)
    if sg * (fill - signal.stop) <= 0 or sg * (o[e] - signal.target) >= 0:
        return None
    risk = sg * (fill - signal.stop)
    last = e + signal.max_hold_bars - 1
    available_last = min(last, len(frame) - 1)
    worse_of = min if sg > 0 else max

    best = worst = 0.0
    for j in range(e, available_last + 1):
        worst = min(worst, sg * (adverse[j] - fill) / risk)
        if sg * (adverse[j] - signal.stop) <= 0:
            mid = worse_of(signal.stop, o[j]) if j > e else signal.stop
            px, reason = costs.exit_fill(signal.symbol, mid, sign=sg), ExitReason.STOP
            break
        best = max(best, sg * (favour[j] - fill) / risk)
        if sg * (favour[j] - signal.target) >= 0:
            px, reason = costs.exit_fill(signal.symbol, signal.target, slippage=False, sign=sg), ExitReason.TARGET
            break
    else:
        if last >= len(frame):
            return None  # unresolved: neither barrier hit and the time limit lies past the data
        j, px, reason = last, costs.exit_fill(signal.symbol, c[last], sign=sg), ExitReason.TIME

    fees = costs.fees(fill) + costs.fees(px)
    r_gross = sg * (px - fill) / risk
    r_net = (sg * (px - fill) - fees) / risk
    ideal = sg * (_mid_exit(reason, signal, o, c, j, e) - o[e]) / risk
    return Outcome(
        entry_time=frame.index[e].to_pydatetime(), exit_time=frame.index[j].to_pydatetime(),
        fill=float(fill), exit_price=float(px), reason=reason, bars_held=j - e + 1,
        r_gross=float(r_gross), r_net=float(r_net), cost_r=float(ideal - r_net),
        mfe_r=float(best), mae_r=float(worst),
    )


def _mid_exit(reason: ExitReason, s: Signal, o: np.ndarray, c: np.ndarray, j: int, e: int) -> float:
    if reason is ExitReason.TARGET:
        return s.target
    if reason is ExitReason.STOP:
        worse_of = min if s.sign > 0 else max
        return worse_of(s.stop, o[j]) if j > e else s.stop
    return c[j]
