"""Turn signals + their simulated outcomes into a labelled, hashable dataset."""

from __future__ import annotations

import hashlib

import pandas as pd

from nextrade_core.costs.model import CostModel
from nextrade_core.strategies.base import Signal

from .labels import simulate_exit

META = ["key", "symbol", "strategy", "t", "t_end", "reward_risk", "y", "r_gross", "r_net", "cost_r",
        "exit_reason", "bars_held"]


def build_dataset(frames: dict[str, pd.DataFrame], signals: list[Signal], costs: CostModel) -> pd.DataFrame:
    rows = []
    for s in signals:
        out = simulate_exit(frames[s.symbol], s, costs)
        if out is None:
            continue
        rows.append({
            "key": s.key, "symbol": s.symbol, "strategy": s.strategy,
            "t": pd.Timestamp(s.decision_time), "t_end": pd.Timestamp(out.exit_time) + s.timeframe.delta,
            "reward_risk": s.reward_risk, "y": out.hit_target, "r_gross": out.r_gross,
            "r_net": out.r_net, "cost_r": out.cost_r, "exit_reason": out.reason.value,
            "bars_held": out.bars_held, **s.features,
        })
    df = pd.DataFrame(rows)
    return df.sort_values("t", kind="stable").reset_index(drop=True) if len(df) else df


def dataset_hash(df: pd.DataFrame) -> str:
    """Stable fingerprint of a dataset's content (stored with every experiment)."""
    if df.empty:
        return "empty"
    digest = pd.util.hash_pandas_object(df.reset_index(drop=True), index=True).to_numpy().tobytes()
    return hashlib.sha256(digest).hexdigest()[:16]
