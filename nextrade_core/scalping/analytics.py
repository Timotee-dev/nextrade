"""Post-trade analytics: where does the money go? By strategy, instrument, session and regime.

Works on a plain DataFrame so the same code serves backtests and the paper ledger. Net figures are
after fees AND after the spread/slippage already in the fill prices; "gross" is net plus fees
only, and is labelled that way. Small samples are flagged, never ranked as if they meant something.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_TRADES = 30
COLUMNS = ["strategy", "symbol", "opened_at", "closed_at", "net_pnl", "fees", "risk", "regime", "exit_reason"]


def session_of(ts: pd.Timestamp) -> str:
    h = ts.tz_convert("UTC").hour if ts.tzinfo else ts.hour
    return "asia" if h < 7 else "london" if h < 13 else "new_york" if h < 21 else "late"


def from_positions(qs) -> pd.DataFrame:
    rows = [{"strategy": p.strategy, "symbol": p.symbol, "opened_at": p.opened_at, "closed_at": p.closed_at,
             "net_pnl": float(p.realized_pnl), "fees": float(p.fees), "risk": float(p.initial_risk or 0),
             "regime": (p.thesis or {}).get("regime", "unknown"), "exit_reason": p.exit_reason}
            for p in qs if p.status == "closed" and p.realized_pnl is not None]
    return pd.DataFrame(rows, columns=COLUMNS)


def _stats(g: pd.DataFrame) -> dict:
    n = len(g)
    net, fees = g["net_pnl"], g["fees"]
    wins, losses = net[net > 0], net[net <= 0]
    hold = (pd.to_datetime(g["closed_at"]) - pd.to_datetime(g["opened_at"])).dt.total_seconds().mean() / 60
    gl = abs(losses.sum())
    return {"trades": n, "win_rate": float((net > 0).mean()), "net_profit": float(net.sum()),
            "gross_before_fees": float((net + fees).sum()), "fees": float(fees.sum()),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "profit_factor": float(wins.sum() / gl) if gl > 0 else (float("inf") if len(wins) else 0.0),
            "expectancy": float(net.mean()), "avg_hold_min": float(hold) if not np.isnan(hold) else 0.0,
            "fee_share_of_gross": float(fees.sum() / max((net + fees).clip(lower=0).sum(), 1e-12)),
            "enough_data": n >= MIN_TRADES}


def max_drawdown(net: pd.Series) -> float:
    eq = net.cumsum()
    return float((eq - eq.cummax()).min()) if len(eq) else 0.0


def report(df: pd.DataFrame) -> dict:
    """Overall plus breakdowns. Findings list names only patterns with enough trades behind them."""
    if df.empty:
        return {"overall": {"trades": 0}, "findings": ["no closed trades yet"]}
    d = df.copy()
    d["session"] = pd.to_datetime(d["opened_at"], utc=True).map(session_of)
    out: dict = {"overall": {**_stats(d), "max_drawdown": max_drawdown(d.sort_values("closed_at")["net_pnl"])}}
    for key in ("strategy", "symbol", "session", "regime", "exit_reason"):
        out[f"by_{key}"] = {str(k): _stats(g) for k, g in d.groupby(key)}
    f: list[str] = []
    for key in ("strategy", "symbol", "session", "regime"):
        for k, s in out[f"by_{key}"].items():
            if not s["enough_data"]:
                continue
            if s["expectancy"] < 0:
                f.append(f"{key} {k}: negative expectancy ({s['expectancy']:+.4g}) over {s['trades']} trades")
            if key == "symbol" and s["fee_share_of_gross"] > 0.5:
                f.append(f"symbol {k}: fees take {s['fee_share_of_gross']:.0%} of gross profit")
    out["findings"] = f or ["no pattern with enough trades to act on"]
    return out
