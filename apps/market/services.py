"""Market-data services used by the management commands."""

from __future__ import annotations

from adapters.binance.filters import instrument_from_symbol, supports_oco
from adapters.binance.rest import BinanceRest

from .models import Instrument


def upsert_instruments(client: BinanceRest, symbols: list[str]) -> tuple[int, int]:
    info = client.exchange_info(symbols)
    created = updated = 0
    for raw in info["symbols"]:
        inst = instrument_from_symbol(raw)
        _, was_created = Instrument.objects.update_or_create(
            venue=inst.venue, symbol=inst.symbol,
            defaults={
                "base": inst.base, "quote": inst.quote, "asset_class": inst.asset_class.value,
                "tick_size": inst.tick_size, "step_size": inst.step_size,
                "min_qty": inst.min_qty, "min_notional": inst.min_notional,
                "max_qty": inst.max_qty, "tradable": inst.tradable,
                "oco_allowed": supports_oco(raw), "order_types": sorted(inst.order_types),
            },
        )
        created += was_created
        updated += not was_created
    return created, updated


def ensure_instruments(client: BinanceRest, symbols: list[str], venue: str = "binance") -> dict[str, Instrument]:
    """Instruments for `symbols`, fetching any that are not in the database yet."""
    existing = {i.symbol: i for i in Instrument.objects.filter(venue=venue, symbol__in=symbols)}
    missing = [s for s in symbols if s not in existing]
    if missing:
        upsert_instruments(client, missing)
        existing = {i.symbol: i for i in Instrument.objects.filter(venue=venue, symbol__in=symbols)}
    unknown = sorted(set(symbols) - set(existing))
    if unknown:
        raise ValueError(f"not listed on {venue}: {', '.join(unknown)}")
    return existing
