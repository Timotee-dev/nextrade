"""Risk limits (spec 9). Starting values for TESTING, not values known to be safe.

Limits are versioned. Only the operator can change them, through the admin with an
audit row (Phase 10 wires that). NAIA, models and Telegram have no write path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskLimits:
    version: str = "limits-1"
    risk_per_trade: Decimal = Decimal("0.0025")     # 0.25% of equity
    max_open_risk: Decimal = Decimal("0.02")        # 2%
    max_cluster_risk: Decimal = Decimal("0.015")    # 1.5% in one correlated cluster
    daily_loss: Decimal = Decimal("0.02")           # pause entries until 00:00 UTC
    weekly_loss: Decimal = Decimal("0.05")          # risk shutdown
    max_drawdown: Decimal = Decimal("0.10")         # risk shutdown
    max_consecutive_losses: int = 6                 # pause entries
    consecutive_loss_pause_hours: int = 4
    max_stress_loss: Decimal = Decimal("0.04")
    max_positions: int = 10                          # operator cap
    max_spread_bps: Decimal = Decimal("15")
    max_capital_at_risk: Decimal | None = None       # absolute cap for live micro stage (account ccy)
    gap_allowance: Decimal = Decimal("0.25")         # extra fraction of stop distance for open risk

    def as_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


# Spreads routinely seen per venue (bps). Quidax NGN books showed 18-50 bps on 2026-11-11, so the
# Binance-calibrated 15 bps limit would block every Quidax trade.
VENUE_MAX_SPREAD_BPS = {"binance": Decimal("15"), "quidax": Decimal("60")}


def scaled_limits(risk_per_trade: Decimal, venue: str = "binance", base: RiskLimits | None = None) -> RiskLimits:
    """Limits for a different risk per trade. The limits that count risk ACROSS trades (total
    open risk, one correlated cluster, the stress loss) scale in proportion, so the same number
    of positions fits; capped at 10%, 8% and 20% of equity. Loss limits (daily, weekly,
    drawdown) are NOT loosened: bigger trades hit them sooner, by design."""
    from dataclasses import replace

    b = base or RiskLimits()
    if not Decimal("0") < risk_per_trade <= Decimal("0.02"):
        raise ValueError("risk per trade must be above 0 and at most 2%")
    k = risk_per_trade / b.risk_per_trade
    return replace(b, version=f"{b.version}-r{risk_per_trade}-{venue}", risk_per_trade=risk_per_trade,
                   max_open_risk=min(b.max_open_risk * k, Decimal("0.10")),
                   max_cluster_risk=min(b.max_cluster_risk * k, Decimal("0.08")),
                   max_stress_loss=min(b.max_stress_loss * k, Decimal("0.20")),
                   max_spread_bps=VENUE_MAX_SPREAD_BPS.get(venue, b.max_spread_bps))
