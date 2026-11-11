"""Turn Binance exchangeInfo symbol entries into venue-independent Instruments."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nextrade_core.domain.instrument import AssetClass, Instrument

VENUE = "binance"


def _filter(symbol: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((f for f in symbol.get("filters", []) if f.get("filterType") == kind), None)


def _clean(raw: Any) -> Decimal:
    """'0.01000000' -> 0.01 and '10.00000000' -> 10 (never 1E+1)."""
    d = Decimal(str(raw)).normalize()
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d


def instrument_from_symbol(symbol: dict[str, Any]) -> Instrument:
    price = _filter(symbol, "PRICE_FILTER")
    lot = _filter(symbol, "LOT_SIZE")
    # Binance replaced MIN_NOTIONAL with NOTIONAL on most pairs; accept either.
    notional = _filter(symbol, "NOTIONAL") or _filter(symbol, "MIN_NOTIONAL")
    if price is None or lot is None:
        raise ValueError(f"{symbol.get('symbol')}: missing PRICE_FILTER or LOT_SIZE")

    max_qty = Decimal(str(lot["maxQty"])) if lot.get("maxQty") else None
    return Instrument(
        venue=VENUE,
        symbol=symbol["symbol"],
        base=symbol["baseAsset"],
        quote=symbol["quoteAsset"],
        asset_class=AssetClass.CRYPTO,
        tick_size=_clean(price["tickSize"]),
        step_size=_clean(lot["stepSize"]),
        min_qty=Decimal(str(lot["minQty"])),
        min_notional=Decimal(str(notional["minNotional"])) if notional else Decimal(0),
        max_qty=max_qty,
        tradable=symbol.get("status") == "TRADING" and symbol.get("isSpotTradingAllowed", True),
        order_types=frozenset(symbol.get("orderTypes", [])),
    )


def supports_oco(symbol: dict[str, Any]) -> bool:
    return bool(symbol.get("ocoAllowed"))
