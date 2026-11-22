"""Bybit public spot market data (no API key, no trading): deep 1-minute history and tickers.

Why: Binance is blocked on the operator's network and Gate.io serves at most 10,000 bars back
(about 7 days of 1-minute bars). Bybit pages back through years of 1-minute data.

Format (from Bybit's v5 documentation; NOT yet confirmed against a live response from the
operator's network, run `probe_venues` first):
* GET /v5/market/kline?category=spot&symbol=BTCUSDT&interval=1&start=<ms>&end=<ms>&limit=1000
  -> result.list rows [startTime_ms, open, high, low, close, volume, turnover], newest first.
* GET /v5/market/tickers?category=spot -> result.list of {symbol, bid1Price, ask1Price, turnover24h, ...}
* GET /v5/market/instruments-info?category=spot -> result.list of {symbol, baseCoin, quoteCoin,
  status, lotSizeFilter{basePrecision, minOrderQty, minOrderAmt}, priceFilter{tickSize}}
Responses carry retCode 0 on success. HTTP 403 usually means the region is blocked.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from adapters.base import RateLimitedError, RejectedError, RetryableError
from nextrade_core.data.candles import EPOCH, Candle, Timeframe
from nextrade_core.domain.instrument import AssetClass, Instrument

BASE_URL = "https://api.bybit.com"
VENUE = "bybit"
MAX_PER_REQUEST = 1_000
INTERVALS = {Timeframe.M1: "1", Timeframe.M3: "3", Timeframe.M5: "5", Timeframe.M15: "15", Timeframe.M30: "30",
             Timeframe.H1: "60", Timeframe.H4: "240", Timeframe.D1: "D"}
STABLES = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "USDP", "PYUSD", "EUR", "EURI", "USDT", "BUSD"}
LEVERAGED = ("2L", "2S", "3L", "3S", "5L", "5S", "UP", "DOWN")


def _dec(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x)) if x not in (None, "") else Decimal(default)
    except InvalidOperation:
        return Decimal(default)


def row_to_candle(row: list[Any], symbol: str, tf: Timeframe) -> Candle:
    t, o, h, low, c, v, turnover = row[:7]
    return Candle(symbol=symbol, timeframe=tf, open_time=EPOCH + timedelta(milliseconds=int(t)),
                  open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c),
                  volume=Decimal(v), quote_volume=Decimal(turnover), trade_count=0)


def instrument_from_info(p: dict[str, Any]) -> Instrument:
    lot, price = p.get("lotSizeFilter") or {}, p.get("priceFilter") or {}
    step = _dec(lot.get("basePrecision"), "0.000001")
    return Instrument(
        venue=VENUE, symbol=p["symbol"], base=p.get("baseCoin", p["symbol"][:-4]), quote=p.get("quoteCoin", "USDT"),
        asset_class=AssetClass.CRYPTO, tick_size=_dec(price.get("tickSize"), "0.0001"), step_size=step,
        min_qty=max(_dec(lot.get("minOrderQty")), step), min_notional=_dec(lot.get("minOrderAmt"), "1"),
        tradable=p.get("status", "Trading") == "Trading", order_types=frozenset({"LIMIT", "MARKET"}))


def pick_top_usdt(tickers: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """The n most traded USDT spot pairs by 24h turnover, without stablecoins or leveraged tokens.
    NOTE: ranking by TODAY's turnover is itself a selection effect (survivorship); results on this
    universe are optimistic about which coins were liquid in the past. Reports say so."""
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLES or base.endswith(LEVERAGED) or not t.get("bid1Price") or not t.get("ask1Price"):
            continue
        rows.append((float(_dec(t.get("turnover24h"))), t))
    rows.sort(key=lambda r: -r[0])
    return [t for _, t in rows[:n]]


class BybitClient:
    def __init__(self, base_url: str = BASE_URL, *, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 20.0, min_interval_s: float = 0.05,
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
        if r.status_code == 403:
            raise RejectedError(f"GET {path}: HTTP 403 (this region or network is probably blocked)", code=403)
        if r.status_code >= 500:
            raise RetryableError(f"GET {path}: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            raise RetryableError(f"GET {path}: not JSON (HTTP {r.status_code})") from exc
        if r.status_code >= 400 or body.get("retCode", 0) != 0:
            raise RejectedError(f"GET {path}: {body.get('retCode')}: {body.get('retMsg')}", code=r.status_code)
        return body.get("result", {})

    def server_time_ms(self) -> int:
        return int(self._get("/v5/market/time").get("timeSecond", 0)) * 1000

    def tickers(self) -> list[dict[str, Any]]:
        return self._get("/v5/market/tickers", {"category": "spot"}).get("list", [])

    def quotes(self) -> dict[str, tuple[str, str]]:
        return {t["symbol"]: (t["bid1Price"], t["ask1Price"]) for t in self.tickers()
                if t.get("bid1Price") and t.get("ask1Price")}

    def instruments(self, symbols: list[str] | None = None) -> list[Instrument]:
        rows = self._get("/v5/market/instruments-info", {"category": "spot", "limit": 1000}).get("list", [])
        want = set(symbols) if symbols else None
        return [instrument_from_info(p) for p in rows if want is None or p.get("symbol") in want]

    def candles(self, symbol: str, tf: Timeframe, start: datetime, end: datetime) -> list[Candle]:
        """Closed bars with open_time in [start, end), paged 1,000 at a time, oldest first."""
        out: dict[datetime, Candle] = {}
        cursor = tf.floor(start)
        while cursor < end:
            stop = min(cursor + (MAX_PER_REQUEST - 1) * tf.delta, end - tf.delta)
            if stop < cursor:
                break
            result = self._get("/v5/market/kline", {
                "category": "spot", "symbol": symbol, "interval": INTERVALS[tf], "limit": MAX_PER_REQUEST,
                "start": int((cursor - EPOCH).total_seconds() * 1000), "end": int((stop - EPOCH).total_seconds() * 1000)})
            for row in result.get("list", []):
                c = row_to_candle(row, symbol, tf)
                if start <= c.open_time < end:
                    out[c.open_time] = c
            cursor = stop + tf.delta
        return [out[k] for k in sorted(out)]
