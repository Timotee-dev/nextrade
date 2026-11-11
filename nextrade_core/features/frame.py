"""Candles -> float DataFrame for feature work, and multi-timeframe alignment.

Features use float64 (fast, vectorised); the ledger never does. The frame index is
the bar OPEN time in UTC. A bar's values are known only at its close time, so a
feature row at index t is usable for a decision made at t + timeframe.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from nextrade_core.data.candles import Candle, Timeframe

COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume", "trades"]


def candles_to_frame(candles: Iterable[Candle]) -> pd.DataFrame:
    rows = [(c.open_time, float(c.open), float(c.high), float(c.low), float(c.close),
             float(c.volume), float(c.quote_volume), c.trade_count) for c in candles]
    df = pd.DataFrame(rows, columns=["open_time", *COLUMNS]).set_index("open_time")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("candles must be strictly increasing in time")
    return df


def align_higher_timeframe(base: pd.DataFrame, base_tf: Timeframe, higher: pd.DataFrame,
                           higher_tf: Timeframe, prefix: str | None = None) -> pd.DataFrame:
    """Attach, to each base row, the latest HIGHER-timeframe row that had CLOSED by the
    time the base bar closed. This is the only correct way to mix timeframes: a 4h bar
    that is still forming must never leak into a 15m decision.
    """
    prefix = prefix or f"{higher_tf.value}_"
    left = pd.DataFrame({"decision_at": base.index + base_tf.delta}, index=base.index)
    right = higher.add_prefix(prefix).copy()
    right["available_at"] = higher.index + higher_tf.delta
    merged = pd.merge_asof(left.reset_index(), right.sort_values("available_at"),
                           left_on="decision_at", right_on="available_at", direction="backward")
    merged = merged.set_index(base.index.name or "open_time")
    return merged.drop(columns=["decision_at", "available_at"])
