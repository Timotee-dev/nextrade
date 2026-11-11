"""Async broker contract used by the execution engine (paper and live implement it)."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from .orders import OrderRequest, VenueOrderState


class Broker(Protocol):
    name: str

    async def submit(self, request: OrderRequest) -> VenueOrderState: ...
    async def get(self, symbol: str, client_order_id: str) -> VenueOrderState | None: ...
    async def cancel(self, symbol: str, client_order_id: str) -> VenueOrderState: ...
    async def open_orders(self, symbol: str | None = None) -> list[VenueOrderState]: ...
    async def balances(self) -> dict[str, Decimal]: ...
    async def place_protective_exit(self, symbol: str, qty: Decimal, stop: Decimal, target: Decimal,
                                    list_client_id: str) -> list[VenueOrderState]: ...
