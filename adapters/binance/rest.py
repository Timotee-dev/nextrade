"""Minimal Binance spot REST client (signed + public), built on httpx.

Why not CCXT for this layer (see docs/adr/0002): order safety depends on exact
control of newClientOrderId, of Binance error codes, and of the used-weight
headers. This client exposes those directly and maps every failure onto the four
normalised error kinds in adapters.base.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from adapters.base import RateLimitedError, RejectedError, RetryableError, UnknownStateError

MAINNET_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"

ORDER_DOES_NOT_EXIST = -2013
TIMESTAMP_OUTSIDE_WINDOW = -1021

# Errors where the request never reached Binance: always safe to retry.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class BinanceRest:
    def __init__(
        self,
        base_url: str = MAINNET_URL,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        recv_window_ms: int = 5000,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        time_ms: Callable[[], int] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._secret = api_secret.encode() if api_secret else None
        self._recv_window = recv_window_ms
        self._time_ms = time_ms or (lambda: int(time.time() * 1000))
        self._offset_ms = 0
        self.used_weight_1m: int | None = None
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout_s, transport=transport)

    # ------------------------------------------------------------------ plumbing
    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    def close(self) -> None:
        self._http.close()

    def _sign(self, params: dict[str, Any]) -> str:
        params = {**params, "timestamp": self._time_ms() + self._offset_ms,
                  "recvWindow": self._recv_window}
        query = urlencode(params)
        assert self._secret is not None
        signature = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 *, signed: bool = False, mutating: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            if not self.has_credentials:
                raise RejectedError("API key and secret required for this call")
            headers["X-MBX-APIKEY"] = self._key  # type: ignore[assignment]
            url = f"{path}?{self._sign(params)}"
        else:
            url = f"{path}?{urlencode(params)}" if params else path

        try:
            response = self._http.request(method, url, headers=headers)
        except _NOT_SENT as exc:
            raise RetryableError(f"{method} {path}: not sent ({exc})") from exc
        except httpx.TransportError as exc:  # read timeout, dropped connection...
            if mutating:
                raise UnknownStateError(f"{method} {path}: outcome unknown ({exc})") from exc
            raise RetryableError(f"{method} {path}: {exc}") from exc

        weight = response.headers.get("x-mbx-used-weight-1m")
        if weight is not None:
            self.used_weight_1m = int(weight)
        return self._handle(response, method, path, mutating)

    @staticmethod
    def _handle(response: httpx.Response, method: str, path: str, mutating: bool) -> Any:
        status = response.status_code
        if status in (418, 429):
            retry_after = response.headers.get("retry-after")
            raise RateLimitedError(f"{method} {path}: rate limited ({status})",
                                   float(retry_after) if retry_after else None)
        if status >= 500:
            # Binance documents 5XX on order calls as "execution status UNKNOWN".
            if mutating:
                raise UnknownStateError(f"{method} {path}: HTTP {status}, outcome unknown")
            raise RetryableError(f"{method} {path}: HTTP {status}")
        body = response.json(parse_float=Decimal) if response.content else {}
        if status >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            msg = body.get("msg", response.text) if isinstance(body, dict) else response.text
            if code == TIMESTAMP_OUTSIDE_WINDOW:
                raise RetryableError(f"{method} {path}: clock drift ({msg}); call sync_time()")
            raise RejectedError(f"{method} {path}: {msg}", code=code)
        return body

    # ------------------------------------------------------------------ public
    def ping(self) -> None:
        self._request("GET", "/api/v3/ping")

    def server_time_ms(self) -> int:
        return int(self._request("GET", "/api/v3/time")["serverTime"])

    def sync_time(self) -> int:
        """Measure clock offset to Binance; returns the offset in ms."""
        before = self._time_ms()
        server = self.server_time_ms()
        after = self._time_ms()
        self._offset_ms = server - (before + after) // 2
        return self._offset_ms

    def exchange_info(self, symbols: list[str] | None = None) -> dict[str, Any]:
        params = {"symbols": json.dumps(symbols, separators=(",", ":"))} if symbols else None
        return self._request("GET", "/api/v3/exchangeInfo", params)

    def klines(self, symbol: str, interval: str, start_ms: int | None = None,
               end_ms: int | None = None, limit: int = 1000) -> list[list[Any]]:
        """Raw kline rows: [open_time, o, h, l, c, volume, close_time, quote_vol, trades, ...]."""
        return self._request("GET", "/api/v3/klines", {
            "symbol": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms,
            "limit": limit,
        })

    def ticker_price(self, symbol: str) -> Decimal:
        return Decimal(str(self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"]))

    # ------------------------------------------------------------------ signed
    def account(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)

    def api_restrictions(self) -> dict[str, Any]:
        """Key permissions (mainnet only): withdrawals, IP restriction, spot trading."""
        return self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    def new_order(self, *, symbol: str, side: str, order_type: str, quantity: Decimal,
                  client_order_id: str, price: Decimal | None = None,
                  time_in_force: str | None = None) -> dict[str, Any]:
        params = {
            "symbol": symbol, "side": side, "type": order_type,
            "quantity": format(quantity, "f"),
            "price": format(price, "f") if price is not None else None,
            "timeInForce": time_in_force,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        return self._request("POST", "/api/v3/order", params, signed=True, mutating=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        """Return the order, or None if Binance confirms it does not exist."""
        try:
            return self._request("GET", "/api/v3/order",
                                 {"symbol": symbol, "origClientOrderId": client_order_id},
                                 signed=True)
        except RejectedError as exc:
            if exc.code == ORDER_DOES_NOT_EXIST:
                return None
            raise

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return self._request("DELETE", "/api/v3/order",
                             {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True, mutating=True)
