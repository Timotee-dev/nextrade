"""Strategy contract (spec 4.4).

A strategy is a deterministic rule set that turns a feature frame (OHLCV plus
build_features columns, see features.feature_set.with_features) into SIGNALS: a
complete trade plan with entry, stop, target, maximum holding time and an
invalidation rule. It never sizes positions and never places orders.

Subclasses implement `_plan(f)`, a vectorised frame with one row per bar:
    entry_ok (bool), stop, target, max_hold, invalidation_level
`generate()` turns it into Signals, adding the rules every strategy shares:
* required columns must be present and finite,
* optional filters (spec B, E, F, G; see filters.py) must all agree,
* geometry must be valid (stop < entry < target for longs),
* after a signal, no new one for `cooldown_bars` bars (one setup = one trade).
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

import numpy as np
import pandas as pd

from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import MODEL_COLUMNS


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class Signal:
    strategy: str                 # strategy code, e.g. "trend"
    strategy_version: str         # code-v<tag>-<param hash>
    symbol: str
    timeframe: Timeframe
    bar_time: datetime            # OPEN time of the signal bar; act at bar_time + timeframe
    side: Side
    entry: float                  # reference entry: the signal bar's close
    stop: float
    target: float
    max_hold_bars: int
    invalidation: str             # rule in words (stored in the trade thesis)
    invalidation_level: float | None
    features: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def key(self) -> str:
        return f"{self.strategy_version}:{self.symbol}:{self.timeframe.value}:{self.bar_time:%Y%m%dT%H%M}"

    @property
    def decision_time(self) -> datetime:
        return self.bar_time + self.timeframe.delta

    @property
    def sign(self) -> int:
        """+1 for a long, -1 for a short: profit = sign * (exit - entry)."""
        return 1 if self.side is Side.LONG else -1

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_risk(self) -> float:
        return abs(self.target - self.entry) / self.risk_per_unit if self.risk_per_unit else 0.0


@dataclass(frozen=True)
class StrategyParams:
    cooldown_bars: int = 0


class SignalFilter(Protocol):
    def mask(self, bars: pd.DataFrame, f: pd.DataFrame) -> pd.Series: ...
    def describe(self) -> str: ...


class Strategy(ABC):
    code: str = "base"
    version_tag: str = "0"
    side: Side = Side.LONG
    required: tuple[str, ...] = ()

    def __init__(self, params: StrategyParams | None = None, filters: tuple[SignalFilter, ...] = ()) -> None:
        self.params = params if params is not None else self.default_params()
        self.filters = tuple(filters)

    @classmethod
    @abstractmethod
    def default_params(cls) -> StrategyParams: ...

    @abstractmethod
    def _plan(self, f: pd.DataFrame) -> pd.DataFrame:
        """Columns: entry_ok, stop, target, max_hold, invalidation_level (one row per bar).
        Optional column `short` (bool): that row's trade is a short; otherwise self.side applies."""

    @abstractmethod
    def invalidation_rule(self) -> str: ...

    @property
    def version(self) -> str:
        payload = json.dumps({"params": asdict(self.params), "filters": [x.describe() for x in self.filters]},
                             sort_keys=True, default=str)
        return f"{self.code}-v{self.version_tag}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"

    def is_invalidated(self, signal: Signal, bar: pd.Series) -> bool:
        """True when the bar's close breaks the signal's invalidation level (longs: closes below)."""
        level = signal.invalidation_level
        if level is None or pd.isna(level):
            return False
        close = float(bar["close"])
        return close < level if signal.side is Side.LONG else close > level

    # ------------------------------------------------------------------ signals
    def entry_mask(self, f: pd.DataFrame, plan: pd.DataFrame | None = None) -> pd.Series:
        plan = self._plan(f) if plan is None else plan
        ok = plan["entry_ok"].fillna(False).astype(bool)
        for col in self.required:
            ok &= f[col].notna() & np.isfinite(f[col])
        for flt in self.filters:
            ok &= flt.mask(f, f).fillna(False).astype(bool)
        close = f["close"]
        short = self._short_mask(plan)
        long_ok = (plan["stop"] < close) & (close < plan["target"])
        short_ok = (plan["target"] < close) & (close < plan["stop"])
        ok &= (long_ok & ~short) | (short_ok & short)
        return ok

    def _short_mask(self, plan: pd.DataFrame) -> pd.Series:
        if "short" in plan.columns:
            return plan["short"].fillna(False).astype(bool)
        return pd.Series(self.side is Side.SHORT, index=plan.index)

    def generate(self, f: pd.DataFrame, symbol: str, timeframe: Timeframe) -> list[Signal]:
        plan = self._plan(f)
        ok = self.entry_mask(f, plan).to_numpy()
        short = self._short_mask(plan).to_numpy()
        cooldown = int(getattr(self.params, "cooldown_bars", 0))
        version, rule = self.version, self.invalidation_rule()
        model_cols = [c for c in MODEL_COLUMNS if c in f.columns]
        out: list[Signal] = []
        last = -10**9
        for i in np.flatnonzero(ok):
            if i - last <= cooldown:
                continue
            last = i
            ts, row, p = f.index[i], f.iloc[i], plan.iloc[i]
            inv = p.get("invalidation_level")
            out.append(Signal(
                strategy=self.code, strategy_version=version, symbol=symbol, timeframe=timeframe,
                bar_time=ts.to_pydatetime(), side=Side.SHORT if short[i] else Side.LONG, entry=float(row["close"]),
                stop=float(p["stop"]), target=float(p["target"]), max_hold_bars=int(p["max_hold"]),
                invalidation=rule, invalidation_level=None if inv is None or pd.isna(inv) else float(inv),
                features={c: float(row[c]) for c in model_cols if pd.notna(row[c])},
            ))
        return out
