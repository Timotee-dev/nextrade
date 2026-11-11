"""Performance metrics (spec 10.4) and the statistics behind the decision gate."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timedelta | None]:
    """Largest peak-to-trough fall (fraction) and the longest time spent below a peak."""
    if equity.empty:
        return 0.0, None
    peak = equity.cummax()
    dd = 1 - equity / peak
    under = dd > 0
    longest, start = pd.Timedelta(0), None
    for t, flag in under.items():
        if flag and start is None:
            start = t
        elif not flag and start is not None:
            longest, start = max(longest, t - start), None
    if start is not None:
        longest = max(longest, equity.index[-1] - start)
    return float(dd.max()), longest


def daily_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    return equity.resample("1D").last().ffill().pct_change().dropna()


def sharpe(daily: pd.Series, periods: int = 365) -> float | None:
    if len(daily) < 10 or daily.std(ddof=1) == 0:
        return None
    return float(daily.mean() / daily.std(ddof=1) * math.sqrt(periods))


def sortino(daily: pd.Series, periods: int = 365) -> float | None:
    downside = daily[daily < 0]
    if len(daily) < 10 or len(downside) < 2:
        return None
    dd = math.sqrt((downside ** 2).mean())
    return float(daily.mean() / dd * math.sqrt(periods)) if dd > 0 else None


def bootstrap_mean_ci(values, n: int = 4000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float] | None:
    v = np.asarray(values, dtype=float)
    if len(v) < 5:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n, len(v)), replace=True).mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def risk_of_ruin(r_values, risk_fraction: float, *, n_trades: int = 500, paths: int = 4000,
                 ruin_drawdown: float = 0.5, seed: int = 0) -> float | None:
    """Share of simulated paths that ever fall `ruin_drawdown` below their peak, when each
    trade risks `risk_fraction` of current equity and trade results (in R) are resampled
    from the observed ones. Assumes trades are independent: optimistic if they are not."""
    r = np.asarray(r_values, dtype=float)
    if len(r) < 20:
        return None
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(paths, n_trades), replace=True)
    growth = np.cumprod(1 + risk_fraction * draws, axis=1)
    growth = np.clip(growth, 0, None)
    peaks = np.maximum.accumulate(np.concatenate([np.ones((paths, 1)), growth], axis=1), axis=1)[:, 1:]
    ruined = ((1 - growth / peaks) >= ruin_drawdown).any(axis=1)
    return float(ruined.mean())


def summarize(trades: pd.DataFrame, equity: pd.Series, initial_cash: float, *, risk_fraction: float = 0.0025) -> dict:
    out: dict = {"trades": int(len(trades)), "initial_cash": float(initial_cash)}
    final = float(equity.iloc[-1]) if len(equity) else float(initial_cash)
    out["final_equity"] = final
    out["net_return"] = final / float(initial_cash) - 1
    mdd, mdd_dur = max_drawdown(equity)
    out["max_drawdown"] = mdd
    out["max_drawdown_duration_days"] = mdd_dur.total_seconds() / 86400 if mdd_dur is not None else None
    daily = daily_returns(equity)
    out["sharpe"], out["sortino"] = sharpe(daily), sortino(daily)
    if trades.empty:
        return out
    pnl = trades["pnl"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    r = trades["r_net"].astype(float)
    out.update({
        "gross_profit": gross_profit, "gross_loss": gross_loss, "net_profit": float(pnl.sum()),
        "fees": float(trades["fees"].astype(float).sum()),
        "win_rate": float((pnl > 0).mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(r.mean()),
        "expectancy_r_ci95": bootstrap_mean_ci(r),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "avg_bars_held": float(trades["bars_held"].mean()),
        "exit_reasons": trades["reason"].value_counts().to_dict(),
        "risk_of_ruin_50pct_dd": risk_of_ruin(r, risk_fraction),
    })
    out["cost_share_of_gross"] = out["fees"] / gross_profit if gross_profit > 0 else None
    return out
