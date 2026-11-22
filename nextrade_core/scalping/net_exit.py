"""One net-profit exit rule, used by the backtester AND the paper/live trader.

A scalp is a success only if it is profitable AFTER costs: entry fee, exit fee, spread and slippage.
This module owns three things so both engines behave identically:

* `planned_net_frac`  : net return a signal's own target would give after the full round trip.
* `entry_gate`        : refuse a signal whose target cannot clear costs by `min_net_bps`.
* `NetProfitExit`     : close a position when its NET profit (if closed right now) has cleared the
                        minimum and then gives back too much of its peak. Built on
                        `profit.should_take_profit`, so it never closes in profit below the minimum.

Stops and time limits are risk exits and stay as they are; they may close at a net loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nextrade_core.costs.model import CostModel

from .profit import ExitKind, OpenTrade, ProfitPolicy, net_if_closed_now, should_take_profit


@dataclass(frozen=True)
class NetExitConfig:
    min_net_bps: float = 8.0        # net profit (bps of entry notional) a scalp must clear to count and to exit
    give_back: float = 0.35         # fraction of the peak net profit that may be given back
    min_planned_bps: float = 15.0   # a signal's target must promise at least this much NET at entry


def planned_net_frac(costs: CostModel, symbol: str, entry_mid: float, target: float, sign: int = 1) -> float:
    """Net return (fraction of the entry fill) if the target is hit: after spread, slippage and both fees."""
    fill = costs.entry_fill(symbol, entry_mid, sign)
    out = costs.exit_fill(symbol, target, slippage=False, sign=sign)
    gross = sign * (out - fill) / fill
    return gross - costs.fee_rate - costs.fee_rate * out / fill


def entry_gate(costs: CostModel, symbol: str, entry_mid: float, target: float, sign: int,
               cfg: NetExitConfig) -> str | None:
    """None = acceptable. Otherwise the reason the signal's profit target cannot beat costs."""
    net = planned_net_frac(costs, symbol, entry_mid, target, sign)
    if net * 10_000 < cfg.min_planned_bps:
        return f"target nets {net * 10_000:.1f} bps after costs, below {cfg.min_planned_bps:g}"
    return None


def cost_breakdown(costs: CostModel, symbol: str, sign: int, qty: float, entry_fill: float, exit_fill_: float,
                   entry_fee: float, exit_fee: float, entry_mid: float | None = None,
                   exit_mid: float | None = None) -> dict:
    """gross (fill to fill, before fees), fees, estimated spread+slippage already inside the fills, net."""
    gross = sign * (exit_fill_ - entry_fill) * qty
    fees = entry_fee + exit_fee
    friction = (costs.spread(symbol) + 2 * costs.slippage_bps / 10_000) * entry_fill * qty
    return {"gross": gross, "fees": fees, "spread_slippage_est": friction, "net": gross - fees,
            "gross_ex_friction_est": gross + friction}


@dataclass
class NetProfitExit:
    """Stateful per-position tracker of the best net figure seen. Keyed by any hashable id."""
    costs: CostModel
    cfg: NetExitConfig = field(default_factory=NetExitConfig)
    _peak: dict = field(default_factory=dict)

    def reason(self, key, symbol: str, sign: int, entry_fill: float, qty: float, entry_fee: float,
               mid: float, bars_held: int = 0) -> str | None:
        notional = entry_fill * qty
        min_net = self.cfg.min_net_bps / 10_000 * notional
        t = OpenTrade(symbol, sign, entry_fill, qty, entry_fee, bars_held, self._peak.get(key, 0.0))
        net = net_if_closed_now(t, mid, self.costs)
        self._peak[key] = max(self._peak.get(key, 0.0), net)
        t = OpenTrade(symbol, sign, entry_fill, qty, entry_fee, bars_held, self._peak[key])
        if self._peak[key] <= min_net:          # the net profit has never cleared the minimum: nothing to protect
            return None
        policy = ProfitPolicy(ExitKind.TRAILING, min_net=min_net, give_back=self.cfg.give_back)
        hit, why = should_take_profit(policy, t, mid, self.costs)
        return f"net_profit_trail" if hit else None

    def net_now(self, symbol: str, sign: int, entry_fill: float, qty: float, entry_fee: float, mid: float) -> float:
        return net_if_closed_now(OpenTrade(symbol, sign, entry_fill, qty, entry_fee), mid, self.costs)

    def forget(self, key) -> None:
        self._peak.pop(key, None)


def backtest_exit_policy(costs: CostModel, cfg: NetExitConfig | None = None):
    """An `exit_policy` for `Backtester`: same rule the live loop uses, evaluated at each bar close."""
    tracker = NetProfitExit(costs, cfg or NetExitConfig())

    def policy(p, row, i) -> str | None:
        key = p.signal.key
        why = tracker.reason(key, p.signal.symbol, p.sign, float(p.fill), float(p.qty), float(p.entry_fee),
                             float(row["close"]), p.bars_held)
        if why:
            tracker.forget(key)
        return why
    return policy


def backtest_entry_gate(costs: CostModel, cfg: NetExitConfig | None = None):
    """An `entry_gate` for `Backtester`: (symbol, open_price, signal) -> reason or None."""
    c = cfg or NetExitConfig()

    def gate(symbol: str, open_px: float, sig) -> str | None:
        return entry_gate(costs, symbol, open_px, sig.target, sig.sign, c)
    return gate
