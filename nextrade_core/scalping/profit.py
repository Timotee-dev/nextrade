"""Net-profit-aware profit taking. A trade is "in profit" only after costs.

Unrealised P&L at the mid price ignores what closing will cost. `net_if_closed_now` charges the
exit side (half spread, slippage, exit fee) and the fee already paid on entry, so a position only
counts as profitable, and only qualifies for a profit exit, when that figure clears a minimum.
Every exit kind below is evaluated on this net figure, never on the displayed gross P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nextrade_core.costs.model import CostModel


class ExitKind(StrEnum):
    FIXED = "fixed"            # take-profit price
    PERCENT = "percent"        # take-profit as a % move
    ATR = "atr"                # take-profit in ATR multiples
    TRAILING = "trailing"      # give back at most X of the peak net profit once past the minimum
    MOMENTUM = "momentum"      # momentum faded while in net profit
    TIME = "time"              # held too long while in net profit


@dataclass(frozen=True)
class OpenTrade:
    symbol: str
    sign: int                  # +1 long, -1 short
    entry_fill: float          # actual fill price paid
    qty: float
    entry_fee: float           # fee already paid (quote currency)
    bars_held: int = 0
    peak_net: float = 0.0      # best net-if-closed figure seen so far


def net_if_closed_now(t: OpenTrade, mid: float, costs: CostModel, contract: float = 1.0) -> float:
    """Net profit (quote currency) if the position were closed at market right now."""
    exit_px = costs.exit_fill(t.symbol, mid, True, t.sign)
    gross = t.sign * (exit_px - t.entry_fill) * t.qty * contract
    return gross - costs.fees(exit_px * t.qty * contract) - t.entry_fee


def breakeven_mid(t: OpenTrade, costs: CostModel, contract: float = 1.0) -> float:
    """Mid price at which closing now nets exactly zero (solved by bisection; monotone in price)."""
    lo, hi = t.entry_fill * 0.5, t.entry_fill * 1.5
    for _ in range(80):
        mid = (lo + hi) / 2
        v = net_if_closed_now(t, mid, costs, contract) * t.sign
        lo, hi = (mid, hi) if v < 0 else (lo, mid)
    return (lo + hi) / 2


@dataclass(frozen=True)
class ProfitPolicy:
    kind: ExitKind
    min_net: float = 0.0          # minimum net profit (quote currency) before ANY profit exit
    pct: float = 0.0              # PERCENT: required price move
    atr_mult: float = 0.0         # ATR: required move in ATR
    give_back: float = 0.35       # TRAILING: allowed fraction of peak net profit to give back
    max_bars: int = 0             # TIME
    momentum_floor: float = 0.0   # MOMENTUM: exit when momentum (signed in trade direction) falls below


def should_take_profit(policy: ProfitPolicy, t: OpenTrade, mid: float, costs: CostModel, *,
                       atr: float = 0.0, momentum: float = 0.0, target: float | None = None,
                       contract: float = 1.0) -> tuple[bool, str]:
    """(close?, reason). Never closes below `min_net`, whatever the kind says."""
    net = net_if_closed_now(t, mid, costs, contract)
    if net <= max(policy.min_net, 0.0):
        return False, f"net {net:+.6g} does not clear the minimum {policy.min_net:g}"
    moved = t.sign * (mid - t.entry_fill)
    k = policy.kind
    if k is ExitKind.FIXED:
        hit = target is not None and t.sign * (mid - target) >= 0
        return hit, "take-profit price reached" if hit else "target not reached"
    if k is ExitKind.PERCENT:
        hit = moved / t.entry_fill >= policy.pct
        return hit, f"moved {moved / t.entry_fill:.3%}" if hit else "percent target not reached"
    if k is ExitKind.ATR:
        hit = atr > 0 and moved >= policy.atr_mult * atr
        return hit, f"moved {moved / atr:.2f} ATR" if hit else "ATR target not reached"
    if k is ExitKind.TRAILING:
        peak = max(t.peak_net, net)
        hit = net <= peak * (1 - policy.give_back)
        return hit, "gave back too much of the peak profit" if hit else "still within the trail"
    if k is ExitKind.MOMENTUM:
        hit = t.sign * momentum <= policy.momentum_floor
        return hit, "momentum faded" if hit else "momentum intact"
    hit = policy.max_bars > 0 and t.bars_held >= policy.max_bars
    return hit, "time limit reached while in net profit" if hit else "within time limit"
