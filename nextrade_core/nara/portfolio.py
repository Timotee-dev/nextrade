"""Portfolio snapshot that NARA evaluates, plus aggregate, cluster and stress risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    price: Decimal               # latest mark
    asset_class: str = "crypto"
    beta: Decimal = Decimal(1)   # move relative to the market shock (BTC = 1)

    @property
    def risk_to_stop(self) -> Decimal:
        return max(Decimal(0), (self.price - self.stop) * self.qty)

    @property
    def value(self) -> Decimal:
        return self.price * self.qty


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    free_cash: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    consecutive_losses: int = 0
    positions: tuple[OpenPosition, ...] = ()
    clusters: dict[str, str] = field(default_factory=dict)   # symbol -> cluster id

    @property
    def drawdown(self) -> Decimal:
        return Decimal(0) if self.peak_equity <= 0 else max(Decimal(0), 1 - self.equity / self.peak_equity)

    @property
    def day_loss(self) -> Decimal:
        return max(Decimal(0), (self.day_start_equity - self.equity) / self.day_start_equity)

    @property
    def week_loss(self) -> Decimal:
        return max(Decimal(0), (self.week_start_equity - self.equity) / self.week_start_equity)

    def cluster_of(self, symbol: str) -> str:
        return self.clusters.get(symbol, symbol)


def open_risk(positions, gap_allowance: Decimal) -> Decimal:
    """Loss if every stop is hit, plus an allowance for stops filling worse (gaps)."""
    return sum((p.risk_to_stop * (1 + gap_allowance) for p in positions), Decimal(0))


@dataclass(frozen=True)
class StressScenario:
    name: str = "crypto crash"
    market_shock: Decimal = Decimal("0.10")     # BTC -10%
    exit_slippage: Decimal = Decimal("0.005")   # stops fill 0.5% worse
    floor_same_class_corr: Decimal = Decimal("0.8")


def stress_loss(positions, scenario: StressScenario = StressScenario()) -> Decimal:
    """Assume every position in the shocked class falls together (correlation goes to 1 in a
    crash). Each exits at the worse of its stop and the shocked price, minus slippage."""
    total = Decimal(0)
    for p in positions:
        shocked = p.price * (1 - scenario.market_shock * p.beta)
        exit_px = min(p.stop, shocked) * (1 - scenario.exit_slippage)
        total += max(Decimal(0), (p.price - exit_px) * p.qty)
    return total


def correlation_clusters(returns: pd.DataFrame, threshold: float = 0.7, floor: float | None = 0.8,
                         same_class: dict[str, str] | None = None) -> dict[str, str]:
    """Group symbols whose rolling return correlation exceeds `threshold` (single linkage).

    `floor` raises the correlation of symbols in the same asset class to at least this
    value, because correlations jump in sell-offs. With the default floor every crypto
    pair ends up in one cluster: the honest assumption for long-only spot crypto.
    """
    corr = returns.corr().fillna(0).to_numpy().copy()  # writable (pandas 3 returns read-only views)
    symbols = list(returns.columns)
    if floor is not None and same_class:
        for i, a in enumerate(symbols):
            for j, b in enumerate(symbols):
                if i != j and same_class.get(a) is not None and same_class.get(a) == same_class.get(b):
                    corr[i, j] = max(corr[i, j], floor)
    parent = list(range(len(symbols)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            if corr[i, j] > threshold:
                parent[find(i)] = find(j)
    roots = {i: find(i) for i in range(len(symbols))}
    names = {}
    for i, s in enumerate(symbols):
        names.setdefault(roots[i], f"cluster-{len(names) + 1}")
    return {s: names[roots[i]] for i, s in enumerate(symbols)}

