"""Market-data services used by the management commands."""

from __future__ import annotations

from adapters.binance.filters import instrument_from_symbol, supports_oco
from adapters.binance.rest import BinanceRest

from .models import Instrument


def save_instrument(inst, oco_allowed: bool = False) -> bool:
    """Upsert one domain Instrument; returns True if created."""
    _, created = Instrument.objects.update_or_create(
        venue=inst.venue, symbol=inst.symbol,
        defaults={
            "base": inst.base, "quote": inst.quote, "asset_class": inst.asset_class.value,
            "tick_size": inst.tick_size, "step_size": inst.step_size, "min_qty": inst.min_qty,
            "min_notional": inst.min_notional, "max_qty": inst.max_qty, "tradable": inst.tradable,
            "oco_allowed": oco_allowed, "order_types": sorted(inst.order_types),
        },
    )
    return created


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


def sync_quidax_markets(client, symbols: list[str] | None = None) -> dict[str, Instrument]:
    """Upsert Quidax markets (all, or only `symbols`) and return them by symbol."""
    from decimal import Decimal

    from adapters.quidax.markets import instrument_from_market, symbol_of

    tickers = client.tickers()
    usdt = tickers.get("usdtngn", {}).get("ticker", {})
    ngn_per_usdt = Decimal(str(usdt["last"])) if usdt.get("last") else None
    wanted = {s.upper() for s in symbols} if symbols else None
    for m in client.markets():
        if wanted and symbol_of(m["id"]) not in wanted:
            continue
        quote = m["quote_unit"].upper()
        rate = Decimal(1) if quote == "NGN" else (ngn_per_usdt if quote in ("USDT", "USDC") else None)
        save_instrument(instrument_from_market(m, rate))
    found = {i.symbol: i for i in Instrument.objects.filter(venue="quidax")}
    if wanted:
        missing = sorted(wanted - set(found))
        if missing:
            raise ValueError(f"not listed on quidax: {', '.join(missing)}")
        found = {k: v for k, v in found.items() if k in wanted}
    return found


def measured_spreads_bps(instruments: dict[str, Instrument], days: int = 7, min_samples: int = 30) -> dict[str, float]:
    """Median recorded spread per symbol (basis points of mid), where enough samples exist."""
    from datetime import timedelta
    from statistics import median

    from django.utils import timezone

    from .models import QuoteSample

    since = timezone.now() - timedelta(days=days)
    out = {}
    for sym, inst in instruments.items():
        rows = list(QuoteSample.objects.filter(instrument=inst, ts__gte=since).values_list("bid", "ask"))
        vals = [float((a - b) / ((a + b) / 2)) * 1e4 for b, a in rows if a > b > 0]
        if len(vals) >= min_samples:
            out[sym] = round(median(vals), 2)
    return out
