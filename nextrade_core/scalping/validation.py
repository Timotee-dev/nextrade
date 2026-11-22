"""Plain-DataFrame measurements for the scalping validation report.

Every number comes from the out-of-sample trade list (columns: symbol, entry_time, exit_time,
pnl, fees, r_net). Nothing here invents or smooths results. Drawdown is computed from the
stitched realised pnl ordered by exit time, as a share of the starting cash.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def covered_days(folds: list[dict], holdout_from: pd.Timestamp | None = None) -> float:
    """Days of out-of-sample time actually traded (sum of test windows, trimmed at the hold-out start)."""
    total = 0.0
    for f in folds:
        a, b = pd.Timestamp(f["test_start"]), pd.Timestamp(f["test_end"])
        if holdout_from is not None:
            a = max(a, holdout_from)
        if b > a:
            total += (b - a).total_seconds() / 86400
    return total


def max_drawdown(trades: pd.DataFrame, initial_cash: float) -> float:
    if trades.empty:
        return 0.0
    pnl = trades.sort_values("exit_time")["pnl"].astype(float).cumsum().to_numpy()
    equity = np.concatenate([[initial_cash], initial_cash + pnl])
    peak = np.maximum.accumulate(equity)
    return float(((peak - equity) / peak).max())


def concurrency(trades: pd.DataFrame) -> dict:
    """Peak and time-weighted mean number of positions open at once, and symbols traded."""
    if trades.empty:
        return {"peak_open": 0, "mean_open": 0.0, "symbols_traded": 0}
    ev = pd.concat([pd.DataFrame({"t": pd.to_datetime(trades["entry_time"]), "d": 1}),
                    pd.DataFrame({"t": pd.to_datetime(trades["exit_time"]), "d": -1})])
    ev = ev.sort_values(["t", "d"]).reset_index(drop=True)      # exits before entries at the same instant
    open_now = ev["d"].cumsum().to_numpy()
    span = ev["t"].diff().shift(-1).dt.total_seconds().fillna(0).to_numpy()
    total = span.sum()
    return {"peak_open": int(open_now.max()),
            "mean_open": float((open_now * span).sum() / total) if total > 0 else 0.0,
            "symbols_traded": int(trades["symbol"].nunique())}


def measure(trades: pd.DataFrame, days: float, initial_cash: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "days": round(days, 2), "trades_per_day": 0.0, "net_pnl": 0.0, "net_return": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0, "fees": 0.0,
                "mean_r_net": 0.0, "peak_open": 0, "mean_open": 0.0, "symbols_traded": 0}
    pnl = trades["pnl"].astype(float)
    wins, losses = pnl[pnl > 0].sum(), abs(pnl[pnl <= 0].sum())
    out = {"trades": n, "days": round(days, 2), "trades_per_day": round(n / days, 2) if days > 0 else 0.0,
           "net_pnl": float(pnl.sum()), "net_return": float(pnl.sum() / initial_cash),
           "win_rate": float((pnl > 0).mean()),
           "profit_factor": float(wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0),
           "max_drawdown": max_drawdown(trades, initial_cash), "fees": float(trades["fees"].astype(float).sum()),
           "mean_r_net": float(trades["r_net"].astype(float).mean())}
    out.update(concurrency(trades))
    return out


def sustains_frequency(dev_gate_pass: bool, dev: dict, hold: dict | None, hold_gate_pass: bool | None,
                       min_per_day: float) -> tuple[bool, list[str]]:
    """The only definition of 'can sustain high frequency without losing money after costs'."""
    why = []
    if not dev_gate_pass:
        why.append("failed the development-period decision gate")
    if dev.get("net_pnl", 0) <= 0:
        why.append("net loss after costs in development period")
    if dev.get("trades_per_day", 0) < min_per_day:
        why.append(f"only {dev.get('trades_per_day', 0):.1f} trades/day (need {min_per_day:g})")
    if hold is None:
        why.append("hold-out not run (development did not pass)")
    else:
        if not hold_gate_pass:
            why.append("failed the untouched hold-out gate")
        if hold.get("net_pnl", 0) <= 0:
            why.append("net loss after costs in hold-out")
        if hold.get("trades_per_day", 0) < min_per_day:
            why.append(f"hold-out only {hold.get('trades_per_day', 0):.1f} trades/day")
    return (not why), why
