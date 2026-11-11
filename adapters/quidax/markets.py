"""Parse Quidax public market data into NEXTRADE types.

Formats confirmed from real responses captured on 2026-11-11 (tests/fixtures/quidax):

* k-line rows are [open_time_ms, OPEN, CLOSE, HIGH, LOW, volume] (NOT Binance's
  O, H, L, C order), newest first. Verified: the 3rd value is always the bar's max,
  the 4th its min, and each bar's close equals the next bar's open.
* depth: {"asks": [[price, qty], ...], "bids": [...], "timestamp": ms} with JSON
  numbers (parsed as Decimal via the client).
* markets: trading_rules {base_precision, price_precision, minimum_order_size}.
  ASSUMPTION to verify with the first real order: minimum_order_size is in NGN
  (1250 on every market seen), converted for non-NGN quotes.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from nextrade_core.data.candles import Candle, Timeframe, ms_to_utc
from nextrade_core.domain.instrument import AssetClass, Instrument

VENUE = "quidax"
PERIOD_MINUTES = {Timeframe.M1: 1, Timeframe.M5: 5, Timeframe.M15: 15, Timeframe.M30: 30,
                  Timeframe.H1: 60, Timeframe.H4: 240, Timeframe.D1: 1440}


def symbol_of(market_id: str) -> str:
    return market_id.upper()


def market_of(symbol: str) -> str:
    return symbol.lower()


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def instrument_from_market(m: dict[str, Any], ngn_per_quote: Decimal | None = None) -> Instrument:
    rules = m.get("trading_rules") or {}
    quote = m["quote_unit"].upper()
    min_ngn = _d(rules.get("minimum_order_size", 0))
    if quote == "NGN":
        min_notional = min_ngn
    elif ngn_per_quote and ngn_per_quote > 0:
        min_notional = (min_ngn / ngn_per_quote).quantize(Decimal("0.01"))
    else:
        min_notional = Decimal(1)  # unknown conversion: placeholder, re-synced once a rate is known
    return Instrument(
        venue=VENUE, symbol=symbol_of(m["id"]), base=m["base_unit"].upper(), quote=quote,
        asset_class=AssetClass.CRYPTO,
        tick_size=Decimal(1).scaleb(-int(rules.get("price_precision", 2))),
        step_size=Decimal(1).scaleb(-int(rules.get("base_precision", 8))),
        min_qty=Decimal(1).scaleb(-int(rules.get("base_precision", 8))),
        min_notional=min_notional, tradable=True,
        order_types=frozenset({"LIMIT", "MARKET"}),
    )


def kline_to_candle(row: Sequence[Any], symbol: str, timeframe: Timeframe) -> Candle:
    t, o, c, h, low, v = row[:6]
    return Candle(symbol=symbol, timeframe=timeframe, open_time=ms_to_utc(int(t)),
                  open=_d(o), high=_d(h), low=_d(low), close=_d(c), volume=_d(v),
                  quote_volume=_d(v) * _d(c), trade_count=0)


def parse_klines(rows: Sequence[Sequence[Any]], symbol: str, timeframe: Timeframe) -> list[Candle]:
    """Oldest first, de-duplicated."""
    by_time = {}
    for row in rows:
        c = kline_to_candle(row, symbol, timeframe)
        by_time[c.open_time] = c
    return [by_time[t] for t in sorted(by_time)]


def best_bid_ask(depth: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    """(bid, ask, bid_qty, ask_qty) from a depth snapshot, or None if a side is empty."""
    bids = sorted(depth.get("bids") or [], key=lambda r: _d(r[0]), reverse=True)
    asks = sorted(depth.get("asks") or [], key=lambda r: _d(r[0]))
    if not bids or not asks:
        return None
    return _d(bids[0][0]), _d(asks[0][0]), _d(bids[0][1]), _d(asks[0][1])
