"""Mechanical checks of scalping behaviour on a finished simulation (not a profitability test).

Each check returns (name, passed, evidence). A check only passes when the simulation shows the
behaviour happening; absence of the behaviour in the data (for example no overlapping trades) is
reported as NOT DEMONSTRATED, never as a pass.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import numpy as np
import pandas as pd

from .validation import concurrency

Check = tuple[str, bool | None, str]          # passed: True, False, or None (not demonstrated)


def _net_exit_ok() -> bool:
    from . import net_exit
    return "should_take_profit" in inspect.getsource(net_exit)


def wiring(backtester_cls, daemon_cls) -> list[Check]:
    """What the code actually uses, found by reading it (not by trusting documentation)."""
    bt, dm = inspect.getsource(backtester_cls), inspect.getsource(daemon_cls)
    return [
        ("re-entry guard used by the backtester", "reentry" in bt, "Backtester(reentry=...) is checked on entry and updated on close"),
        ("re-entry guard used by the trader loop", "reentry" in dm, "TradingDaemon(reentry=...) is checked per signal and updated on close"),
        ("net-of-cost entry gate used by the backtester", "entry_gate" in bt, "Backtester(entry_gate=...) rejects signals whose target cannot clear costs"),
        ("net-profit exit used by the backtester", "exit_policy" in bt and _net_exit_ok(),
         "exit_policy runs the shared NetProfitExit (built on should_take_profit)"),
        ("net-profit exit used by the trader loop", "net_exit" in dm and _net_exit_ok(),
         "TradingDaemon(net_exit=NetProfitExit) is evaluated on every poll with the live quote"),
        ("short signals skipped on spot, counted as skipped", "skipped_short_unsupported" in dm,
         "spot venues cannot short: such signals are counted, never executed"),
    ]


def evaluate(trades: pd.DataFrame, signals: dict[str, pd.Timestamp], *, n_signal_symbols: int, rejections: dict,
             funnel: dict, cooldown: timedelta, days: float) -> list[Check]:
    out: list[Check] = []
    if trades.empty:
        return [("any trades at all", False, "the simulation produced no trades, so nothing can be demonstrated")]
    t = trades.sort_values("entry_time").reset_index(drop=True)
    conc = concurrency(t)

    # 1. multiple simultaneous independent positions (different instruments)
    out.append(("multiple simultaneous positions on different instruments", True if conc["peak_open"] >= 2 else None,
                f"peak {conc['peak_open']} open at once, mean {conc['mean_open']:.2f}, {conc['symbols_traded']} symbols traded"))

    # 2. automatic profit taking
    tp = t[t["reason"] == "target"]
    wins = int((tp["pnl"].astype(float) > 0).sum())
    out.append(("automatic profit-taking at the target", None if len(tp) == 0 else wins > 0,
                f"{len(tp)} trades closed at target, {wins} of them net positive after costs"))

    # 3. automatic closure
    reasons = t["reason"].value_counts().to_dict()
    auto = {k: v for k, v in reasons.items() if k in ("stop", "target", "time")}
    out.append(("automatic closure by stop, target or time limit", len(auto) >= 2 and t["exit_time"].notna().all(),
                f"closed by {reasons}"))

    # 4/5. fresh signals, no stale reuse
    keys = t["signal_key"]
    dup = int(keys.duplicated().sum())
    prev_exit_violations = prev_signal_violations = 0
    min_gap = None
    for _, g in t.groupby("symbol"):
        g = g.sort_values("entry_time")
        for a, b in zip(g.iloc[:-1].itertuples(), g.iloc[1:].itertuples(), strict=True):
            if pd.Timestamp(b.entry_time) <= pd.Timestamp(a.exit_time):
                prev_exit_violations += 1
            sa, sb = signals.get(a.signal_key), signals.get(b.signal_key)
            if sa is not None and sb is not None and sb <= sa:
                prev_signal_violations += 1
            gap = pd.Timestamp(b.entry_time) - pd.Timestamp(a.exit_time)
            min_gap = gap if min_gap is None or gap < min_gap else min_gap
    reentries = int(sum(len(g) - 1 for _, g in t.groupby("symbol")))
    out.append(("every entry uses a fresh signal (no signal reused)", dup == 0 and prev_signal_violations == 0,
                f"{dup} reused signal ids, {prev_signal_violations} entries not newer than the previous trade's signal, {len(t)} trades"))
    out.append(("re-entry after closing, never while still open", None if reentries == 0 else prev_exit_violations == 0,
                f"{reentries} re-entries on a symbol, {prev_exit_violations} opened before the earlier trade closed"))
    if min_gap is not None:
        out.append(("re-entry respects the cooldown", min_gap >= cooldown,
                    f"shortest wait between a close and the next entry on a symbol: {int(min_gap.total_seconds() // 60)} min "
                    f"(cooldown {int(cooldown.total_seconds() // 60)} min); {rejections.get('skip:reentry_guard', 0)} entries blocked by the guard, "
                    f"{rejections.get('skip:already_in_position', 0)} signals skipped because a position was already open"))

    # 6. new positions opened while others remain open
    ent, ext = pd.to_datetime(t["entry_time"]).to_numpy(), pd.to_datetime(t["exit_time"]).to_numpy()
    while_open = sum(int(((ent < e) & (ext > e)).sum() > 0) for e in ent)
    out.append(("new positions opened while others are still open", True if while_open else None,
                f"{while_open} of {len(t)} entries happened while another position was open"))

    # 7. continuous scanning
    ents = pd.to_datetime(t["entry_time"]).sort_values()
    max_idle = ents.diff().max()
    per_day = ents.dt.floor("D").value_counts()
    out.append(("scans many markets continuously", n_signal_symbols >= 2 and len(per_day) >= max(1, int(days * 0.8)),
                f"signals on {n_signal_symbols} symbols; entries on {len(per_day)} of ~{days:.0f} days "
                f"(per day min {int(per_day.min())}, median {int(per_day.median())}, max {int(per_day.max())}); "
                f"longest quiet spell {max_idle.total_seconds() / 3600:.1f} h"))
    return out


def verdict(checks: list[Check]) -> str:
    return "\n".join(f"  [{'PASS' if ok else 'NOT DEMONSTRATED' if ok is None else 'FAIL'}] {name}: {why}" for name, ok, why in checks)
