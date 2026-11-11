"""Helpers shared by the Quidax market-data commands."""

from __future__ import annotations

from django.conf import settings

from adapters.quidax.rest import QuidaxRest


def make_quidax() -> QuidaxRest:
    return QuidaxRest(settings.NEXTRADE["QUIDAX_BASE_URL"])


def quidax_symbols(raw: str | None) -> list[str]:
    raw = raw or ",".join(settings.NEXTRADE["QUIDAX_SYMBOLS"])
    return [s.strip().upper() for s in raw.split(",") if s.strip()]
