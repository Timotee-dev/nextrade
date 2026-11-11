"""Event-driven portfolio backtester (spec 11).

Runs the SAME strategy signals, NAIA scoring and NARA risk engine that live trading
uses, bar by bar, with one simulated cash account in the quote currency (USDT).

Order of events inside each bar T (all symbols):
  1. Entries decided at the previous close fill at T's open (+ half spread + slippage).
     Several signals at once are ranked by NAIA score; NARA approves them one by one.
  2. Every open position checks T's range: stop first, then target. Touching both in
     one bar counts as the stop. A gap through the stop exits at the open.
  3. At T's close: optional exit policy (NPEI), then the time stop, then mark-to-market,
     then NARA's halt check on the new equity.
Fees are charged on both sides. Cash is Decimal; prices come from float bars.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from nextrade_core.costs.model import CostModel
from nextrade_core.domain.instrument import Instrument
from nextrade_core.nara.engine import Halt, RiskEngine, TradeProposal
from nextrade_core.nara.portfolio import OpenPosition, PortfolioSnapshot
from nextrade_core.naia.scoring import Opportunity
from nextrade_core.nctpi.allocation import Candidate, allocate
from nextrade_core.strategies.base import Signal

Scorer = Callable[[Signal], Opportunity | None]          # None = accept with score 0
ExitPolicy = Callable[["Position", pd.Series, int], str | None]  # returns an exit reason or None

_Q = Decimal("0.0000000001")


def dec(x: float) -> Decimal:
    return Decimal(repr(float(x))).quantize(_Q)


@dataclass
class Position:
    signal: Signal
    qty: Decimal
    fill: Decimal
    entry_time: datetime
    entry_fee: Decimal
    initial_risk: Decimal          # qty * (fill - stop)
    bars_held: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    stop: float = 0.0              # may be raised by a trailing policy, never lowered

    def __post_init__(self) -> None:
        self.stop = self.signal.stop


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    initial_cash: Decimal
    funnel: Counter = field(default_factory=Counter)
    rejections: Counter = field(default_factory=Counter)
    events: list[dict] = field(default_factory=list)


class Backtester:
    def __init__(self, frames: dict[str, pd.DataFrame], instruments: dict[str, Instrument],
                 costs: CostModel, risk: RiskEngine, initial_cash: Decimal = Decimal(1000), *,
                 scorer: Scorer | None = None, exit_policy: ExitPolicy | None = None,
                 clusters: dict[str, str] | None = None) -> None:
        self.frames, self.instruments = frames, instruments
        self.costs, self.risk = costs, risk
        self.initial_cash = Decimal(initial_cash)
        self.scorer, self.exit_policy = scorer, exit_policy
        self.clusters = clusters or {s: "crypto" for s in frames}

    # ------------------------------------------------------------------ run
    def run(self, signals: list[Signal]) -> BacktestResult:
        arrays = {s: (f.index, f["open"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy(),
                      f["close"].to_numpy()) for s, f in self.frames.items()}
        row_of = {s: {ts: i for i, ts in enumerate(f.index)} for s, f in self.frames.items()}
        times = sorted(set().union(*(f.index for f in self.frames.values())))

        funnel, rejections, events = Counter(), Counter(), []
        pending: dict[pd.Timestamp, list[tuple[Signal, float]]] = defaultdict(list)
        for s in signals:
            funnel["signals"] += 1
            opp = self.scorer(s) if self.scorer else None
            if opp is not None and not opp.accepted:
                funnel["rejected_by_naia"] += 1
                for r in opp.reasons:
                    rejections[f"naia:{r.value}"] += 1
                continue
            pending[pd.Timestamp(s.decision_time)].append((s, opp.score if opp else 0.0))

        cash = self.initial_cash
        positions: dict[str, Position] = {}
        last_close: dict[str, float] = {}
        trades: list[dict] = []
        equity_points: list[tuple[pd.Timestamp, Decimal]] = []
        peak = day_start = week_start = self.initial_cash
        current_day = current_week = None
        consecutive_losses = 0
        blocked_until: pd.Timestamp | None = None

        def equity_now() -> Decimal:
            return cash + sum((p.qty * dec(last_close[sym]) for sym, p in positions.items()), Decimal(0))

        def close_position(sym: str, px_mid: float, when, reason: str, slippage: bool = True) -> None:
            nonlocal cash, consecutive_losses
            p = positions.pop(sym)
            px = dec(self.costs.exit_fill(sym, px_mid, slippage=slippage))
            proceeds = p.qty * px
            fee = dec(self.costs.fees(float(proceeds)))
            cash += proceeds - fee
            pnl = proceeds - fee - (p.qty * p.fill + p.entry_fee)
            consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
            trades.append({
                "symbol": sym, "strategy": p.signal.strategy, "signal_key": p.signal.key,
                "entry_time": p.entry_time, "exit_time": when, "qty": p.qty, "fill": p.fill, "exit": px,
                "reason": reason, "bars_held": p.bars_held, "pnl": pnl, "fees": p.entry_fee + fee,
                "risk": p.initial_risk, "r_net": float(pnl / p.initial_risk) if p.initial_risk else 0.0,
                "mfe_r": p.mfe_r, "mae_r": p.mae_r,
            })
            funnel["closed"] += 1

        for T in times:
            day, week = T.normalize(), (T - timedelta(days=T.weekday())).normalize()
            if day != current_day:
                current_day, day_start = day, equity_now() if last_close else self.initial_cash
            if week != current_week:
                current_week, week_start = week, equity_now() if last_close else self.initial_cash

            # 1. entries at the open: filter, then rank + NARA through the shared allocator
            ready: list[Candidate] = []
            fills: dict[str, tuple[Signal, Decimal]] = {}
            for sig, score in pending.pop(T, []):
                sym = sig.symbol
                i = row_of[sym].get(T)
                if i is None:
                    rejections["skip:no_bar_at_entry"] += 1
                    continue
                if sym in positions or sym in fills:
                    rejections["skip:already_in_position"] += 1
                    continue
                o = arrays[sym][1][i]
                if o <= sig.stop or o >= sig.target:
                    rejections["skip:plan_invalid_at_open"] += 1
                    continue
                if blocked_until is not None and T < blocked_until:
                    rejections["skip:halted"] += 1
                    continue
                fill = dec(self.costs.entry_fill(sym, o))
                fills[sym] = (sig, fill)
                ready.append(Candidate(sig.key, sym, score,
                                       TradeProposal(sym, fill, dec(sig.stop), dec(self.costs.round_trip_fraction(sym)))))
            if ready:
                snap = PortfolioSnapshot(
                    equity=equity_now() if last_close else cash, free_cash=cash, peak_equity=peak,
                    day_start_equity=day_start, week_start_equity=week_start,
                    consecutive_losses=consecutive_losses,
                    positions=tuple(OpenPosition(s, p.qty, p.fill, dec(p.stop), dec(last_close.get(s, float(p.fill))))
                                    for s, p in positions.items()),
                    clusters=self.clusters)
                alloc = allocate(ready, snap, self.risk, self.instruments, entries_allowed=True)
                for cand, decision, _why in alloc.rejected:
                    funnel["rejected_by_nara"] += 1
                    for rule in (decision.failed if decision else []):
                        rejections[f"nara:{rule}"] += 1
                for cand, decision in alloc.approved:
                    sig, fill = fills[cand.symbol]
                    notional = decision.qty * fill
                    fee = dec(self.costs.fees(float(notional)))
                    cash -= notional + fee
                    positions[cand.symbol] = Position(sig, decision.qty, fill, T.to_pydatetime(), fee,
                                                      decision.qty * (fill - dec(sig.stop)))
                    funnel["entries"] += 1

            # 2. intrabar exits, 3. close-of-bar logic
            for sym in list(positions):
                i = row_of[sym].get(T)
                if i is None:
                    continue
                _, o_, h_, l_, c_ = arrays[sym]
                p = positions[sym]
                p.bars_held += 1
                fill_f = float(p.fill)
                risk_unit = fill_f - p.signal.stop
                first_bar = p.bars_held == 1
                p.mae_r = min(p.mae_r, (l_[i] - fill_f) / risk_unit)
                if l_[i] <= p.stop:
                    close_position(sym, p.stop if first_bar else min(p.stop, o_[i]), T.to_pydatetime(), "stop")
                    continue
                p.mfe_r = max(p.mfe_r, (h_[i] - fill_f) / risk_unit)
                if h_[i] >= p.signal.target:
                    close_position(sym, p.signal.target, T.to_pydatetime(), "target", slippage=False)
                    continue
                last_close[sym] = c_[i]
                if self.exit_policy is not None:
                    reason = self.exit_policy(p, self.frames[sym].iloc[i], i)
                    if reason:
                        close_position(sym, c_[i], T.to_pydatetime(), reason)
                        continue
                if p.bars_held >= p.signal.max_hold_bars:
                    close_position(sym, c_[i], T.to_pydatetime(), "time")
            for sym, (idx, *_cols) in arrays.items():
                i = row_of[sym].get(T)
                if i is not None:
                    last_close[sym] = _cols[3][i]

            eq = equity_now()
            peak = max(peak, eq)
            equity_points.append((T, eq))
            snap = PortfolioSnapshot(eq, cash, peak, day_start, week_start, consecutive_losses)
            halt, why = self.risk.halts(snap)
            if halt is not Halt.NONE and (blocked_until is None or T >= blocked_until):
                if halt is Halt.PAUSE_DAY:
                    blocked_until = day + timedelta(days=1)
                elif halt is Halt.PAUSE_LOSSES:
                    blocked_until = T + timedelta(hours=self.risk.limits.consecutive_loss_pause_hours)
                    consecutive_losses = 0  # the pause is the operator's review in simulation
                else:
                    blocked_until = pd.Timestamp.max.tz_localize("UTC")
                events.append({"time": T.to_pydatetime(), "halt": halt.value, "reason": why})

        if times:
            for sym in list(positions):
                close_position(sym, last_close[sym], times[-1].to_pydatetime(), "end_of_data")
            eq = equity_now()
            if equity_points:
                equity_points[-1] = (times[-1], eq)
        trades_df = pd.DataFrame(trades)
        equity = pd.Series({t: float(v) for t, v in equity_points}, dtype=float)
        return BacktestResult(trades_df, equity, self.initial_cash, funnel, rejections, events)
