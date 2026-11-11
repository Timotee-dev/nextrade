"""Helpers shared by the market-data commands."""

from __future__ import annotations

from datetime import UTC, date, datetime

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection

from adapters.base import VenueError
from adapters.binance.rest import BinanceRest


def make_rest() -> BinanceRest:
    return BinanceRest(settings.NEXTRADE["BINANCE_BASE_URL"])


def parse_symbols(raw: str | None) -> list[str]:
    raw = raw or ",".join(settings.NEXTRADE["DEFAULT_SYMBOLS"])
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def parse_day(raw: str) -> datetime:
    return datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=UTC)


def using_sqlite() -> bool:
    return connection.vendor == "sqlite"


def unreachable(exc: VenueError) -> CommandError:
    return CommandError(
        f"Cannot reach Binance: {exc}\n"
        "Check your connection, then run: python scripts\\check_binance_access.py"
    )
