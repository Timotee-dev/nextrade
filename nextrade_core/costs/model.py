"""Trading-cost model (spec 10.2): fees, half-spread on each side, and slippage.

All costs are expressed per unit traded and converted to R (multiples of the
trade's risk), because a 0.2% cost is trivial on a 5% stop and fatal on a 0.3% stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.001             # per side, Binance spot standard tier
    spread_bps: float = 2.0             # default full spread, basis points of price
    slippage_bps: float = 2.0           # per side, on top of the spread
    spread_bps_by_symbol: dict[str, float] = field(default_factory=dict)

    def spread(self, symbol: str) -> float:
        return self.spread_bps_by_symbol.get(symbol, self.spread_bps) / 10_000

    def entry_fill(self, symbol: str, mid: float) -> float:
        """Buy price paid: mid plus half the spread plus slippage."""
        return mid * (1 + self.spread(symbol) / 2 + self.slippage_bps / 10_000)

    def exit_fill(self, symbol: str, mid: float, slippage: bool = True) -> float:
        """Sell price received (limit-style exits at a target pass slippage=False)."""
        slip = self.slippage_bps / 10_000 if slippage else 0.0
        return mid * (1 - self.spread(symbol) / 2 - slip)

    def fees(self, notional: float) -> float:
        return notional * self.fee_rate

    def round_trip_fraction(self, symbol: str) -> float:
        """Approximate total cost of entering and exiting, as a fraction of price."""
        return 2 * self.fee_rate + self.spread(symbol) + 2 * self.slippage_bps / 10_000

    def cost_in_r(self, symbol: str, entry: float, stop: float) -> float:
        risk = entry - stop
        if risk <= 0:
            raise ValueError("entry must be above stop")
        return self.round_trip_fraction(symbol) * entry / risk
