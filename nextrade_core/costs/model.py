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

    def entry_fill(self, symbol: str, mid: float, sign: int = 1) -> float:
        """Entry price: a long buys at mid + half spread + slippage; a short (sign -1) sells
        at mid - half spread - slippage. Costs always move the price against the trade."""
        return mid * (1 + sign * (self.spread(symbol) / 2 + self.slippage_bps / 10_000))

    def exit_fill(self, symbol: str, mid: float, slippage: bool = True, sign: int = 1) -> float:
        """Exit price: a long sells below mid, a short buys back above it
        (limit-style exits at a target pass slippage=False)."""
        slip = self.slippage_bps / 10_000 if slippage else 0.0
        return mid * (1 - sign * (self.spread(symbol) / 2 + slip))

    def fees(self, notional: float) -> float:
        return notional * self.fee_rate

    def round_trip_fraction(self, symbol: str) -> float:
        """Approximate total cost of entering and exiting, as a fraction of price."""
        return 2 * self.fee_rate + self.spread(symbol) + 2 * self.slippage_bps / 10_000

    def cost_in_r(self, symbol: str, entry: float, stop: float) -> float:
        risk = abs(entry - stop)
        if risk <= 0:
            raise ValueError("entry and stop must differ")
        return self.round_trip_fraction(symbol) * entry / risk


# Starting cost assumptions per venue. Measured spreads (QuoteSample) override spread_bps.
VENUE_COSTS = {
    # Binance spot standard tier: 0.1% per side, tight books on major USDT pairs.
    "binance": dict(fee_rate=0.001, spread_bps=2.0, slippage_bps=2.0),
    # Quidax: 0.1% advertised, up to 0.15% taker reported, so 0.15% is assumed. NGN books are
    # thin: spreads of 18 to 50 bps were observed on 2026-11-11 (ETH/NGN 18, BTC/NGN 49).
    "quidax": dict(fee_rate=0.0015, spread_bps=40.0, slippage_bps=5.0),
    # MT5 forex (Exness Standard): no commission, cost is the spread. Bar spreads measured on
    # 2026-11-12 were 0.65-2.9 bps on 8 majors; MT5 records the LOWEST spread in each bar, so
    # measured values are doubled (SPREAD_MULTIPLIER) and 0.5 bps slippage is added per side.
    "mt5": dict(fee_rate=0.0, spread_bps=2.0, slippage_bps=0.5),
    # Gate.io spot, standard tier: 0.2% per side (taker); tight books on major USDT pairs.
    "gate": dict(fee_rate=0.002, spread_bps=2.0, slippage_bps=2.0),
}
SPREAD_MULTIPLIER = {"mt5": 2.0}


def cost_model_for(venue: str, spread_bps_by_symbol: dict[str, float] | None = None) -> CostModel:
    k = SPREAD_MULTIPLIER.get(venue, 1.0)
    measured = {s: v * k for s, v in (spread_bps_by_symbol or {}).items()}
    return CostModel(**VENUE_COSTS[venue], spread_bps_by_symbol=measured)
