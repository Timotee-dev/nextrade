"""Gate.io public market data, used as RESEARCH history only (NEXTRADE never trades here).

Why: Quidax keeps only ~300 bars and Binance is blocked on the operator's network; Gate's
public API is reachable. Formats confirmed from real responses (tests/fixtures/gate):

* GET /spot/candlesticks?currency_pair=ETH_USDT&interval=1h&from=<s>&to=<s>
  rows: [time_s, quote_volume, CLOSE, HIGH, LOW, OPEN, base_volume, window_closed]
  (a third different order: Binance is O,H,L,C and Quidax is O,C,H,L).
  The last row may be the forming bar ("false"): dropped.
* At most 10,000 bars back from now per interval ("Candlestick too long ago"):
  1h covers ~14 months, 4h ~4.5 years. At most 1,000 bars per request.
* GET /spot/currency_pairs/{pair}: precision, amount_precision, min_base/quote_amount.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from adapters.base import RateLimitedError, RejectedError, RetryableError
from nextrade_core.data.candles import EPOCH, Candle, Timeframe
from nextrade_core.domain.instrument import AssetClass, Instrument

BASE_URL = "https://api.gateio.ws/api/v4"
VENUE = "gate"
MAX_BARS_BACK = 10_000
MAX_PER_REQUEST = 1_000
INTERVALS = {Timeframe.M1: "1m", Timeframe.M5: "5m", Timeframe.M15: "15m", Timeframe.M30: "30m",
             Timeframe.H1: "1h", Timeframe.H4: "4h", Timeframe.D1: "1d"}


def pair_of(symbol: str) -> str:
    """ETHUSDT -> ETH_USDT (quote assumed USDT/USDC/BTC/ETH suffix)."""
    for quote in ("USDT", "USDC", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}_{quote}"
    raise ValueError(f"cannot split {symbol} into base and quote")


def row_to_candle(row: list[Any], symbol: str, tf: Timeframe) -> Candle | None:
    """None for a bar that is still forming."""
    t, qv, c, h, low, o, bv = row[:7]
    if len(row) > 7 and str(row[7]).lower() == "false":
        return None
    return Candle(symbol=symbol, timeframe=tf, open_time=EPOCH + timedelta(seconds=int(t)),
                  open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c),
                  volume=Decimal(bv), quote_volume=Decimal(qv), trade_count=0)


def instrument_from_pair(p: dict[str, Any]) -> Instrument:
    step = Decimal(1).scaleb(-int(p.get("amount_precision", 8)))
    return Instrument(
        venue=VENUE, symbol=p["id"].replace("_", ""), base=p["base"], quote=p["quote"],
        asset_class=AssetClass.CRYPTO, tick_size=Decimal(1).scaleb(-int(p.get("precision", 8))),
        step_size=step, min_qty=max(Decimal(str(p.get("min_base_amount") or 0)), step),
        min_notional=Decimal(str(p.get("min_quote_amount") or 0)),
        tradable=p.get("trade_status") == "tradable", order_types=frozenset({"LIMIT", "MARKET"}),
    )


def earliest_allowed(now: datetime, tf: Timeframe) -> datetime:
    """Oldest bar Gate will serve, with a small safety margin."""
    return tf.floor(now) - (MAX_BARS_BACK - 10) * tf.delta


class GateClient:
    def __init__(self, base_url: str = BASE_URL, *, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 20.0, min_interval_s: float = 0.1,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport)
        self._min_interval, self._sleep, self._last = min_interval_s, sleep, -1e9

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict | None = None) -> Any:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        try:
            r = self._http.get(path, params=params)
        except httpx.TransportError as exc:
            raise RetryableError(f"GET {path}: {exc}") from exc
        if r.status_code == 429:
            raise RateLimitedError(f"GET {path}: rate limited", 10.0)
        if r.status_code >= 500:
            raise RetryableError(f"GET {path}: HTTP {r.status_code}")
        body = r.json()
        if r.status_code >= 400 or (isinstance(body, dict) and "label" in body):
            raise RejectedError(f"GET {path}: {body.get('label')}: {body.get('message')}", code=r.status_code)
        return body

    def currency_pair(self, pair: str) -> dict:
        return self._get(f"/spot/currency_pairs/{pair}")

    def candles(self, symbol: str, tf: Timeframe, start: datetime, end: datetime) -> list[Candle]:
        """Closed bars with open_time in [start, end), paged 1,000 at a time, oldest first."""
        out: dict[datetime, Candle] = {}
        cursor = tf.floor(start)
        while cursor < end:
            stop = min(cursor + (MAX_PER_REQUEST - 1) * tf.delta, end - tf.delta)
            if stop < cursor:
                break
            rows = self._get("/spot/candlesticks", {
                "currency_pair": pair_of(symbol), "interval": INTERVALS[tf],
                "from": int((cursor - EPOCH).total_seconds()), "to": int((stop - EPOCH).total_seconds())})
            for row in rows:
                c = row_to_candle(row, symbol, tf)
                if c is not None and start <= c.open_time < end:
                    out[c.open_time] = c
            cursor = stop + tf.delta
        return [out[t] for t in sorted(out)]
