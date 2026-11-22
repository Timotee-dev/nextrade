"""Market events for the high-frequency scalper. Plain floats and local receive time: this is the hot path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    ts: float            # local receive time, seconds
    bid: float
    ask: float
    bid_sz: float
    ask_sz: float


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: float            # local receive time, seconds
    price: float
    size: float
    buy: bool            # True: taker bought (lifted the ask)
