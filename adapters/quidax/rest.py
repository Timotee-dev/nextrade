"""Quidax REST client (public market data + authenticated orders).

Base URL and endpoints from docs.quidax.io (checked 2026-11-11):
  GET  /markets                       markets and trading rules
  GET  /markets/tickers               all tickers
  GET  /markets/{market}/k            k-lines: period (minutes), timestamp (s), limit <= 10000
  GET  /markets/{market}/depth        order book top levels
  GET  /users/me/wallets              balances                 (auth)
  POST /users/me/orders               market, side, ord_type, price, volume (auth)
  GET  /users/me/orders/{id}          one order                (auth)
  GET  /users/me/orders?market&state  orders                   (auth)
  POST /users/me/orders/{id}/cancel   cancel (queued, < 1 s)   (auth)
Auth: "Authorization: Bearer <secret key>". Rate limit: 300 requests/minute; over the
limit Quidax answers HTTP 444. Quidax documents no client order id, so after an
unknown outcome the caller must reconcile by listing recent orders.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from adapters.base import RateLimitedError, RejectedError, RetryableError, UnknownStateError

BASE_URL = "https://openapi.quidax.io/exchange-open-api/api/v1"
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class QuidaxRest:
    def __init__(self, base_url: str = BASE_URL, secret: str | None = None, *, timeout_s: float = 15.0,
                 transport: httpx.BaseTransport | None = None, min_interval_s: float = 0.21,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
        self._secret = secret
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport)
        self._min_interval = min_interval_s     # 0.21 s keeps us under 300 requests/minute
        self._sleep, self._mono = sleep, monotonic
        self._last = -1e9

    @property
    def has_credentials(self) -> bool:
        return bool(self._secret)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ plumbing
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 *, auth: bool = False, mutating: bool = False) -> Any:
        wait = self._min_interval - (self._mono() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._mono()
        headers = {"accept": "application/json"}
        if auth:
            if not self._secret:
                raise RejectedError("Quidax secret key required for this call")
            headers["Authorization"] = f"Bearer {self._secret}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{path}?{urlencode(query)}" if query else path
        try:
            r = self._http.request(method, url, headers=headers, json=body)
        except _NOT_SENT as exc:
            raise RetryableError(f"{method} {path}: not sent ({exc})") from exc
        except httpx.TransportError as exc:
            if mutating:
                raise UnknownStateError(f"{method} {path}: outcome unknown ({exc})") from exc
            raise RetryableError(f"{method} {path}: {exc}") from exc

        if r.status_code in (429, 444):
            raise RateLimitedError(f"{method} {path}: rate limited ({r.status_code})", 60.0)
        if r.status_code >= 500:
            if mutating:
                raise UnknownStateError(f"{method} {path}: HTTP {r.status_code}, outcome unknown")
            raise RetryableError(f"{method} {path}: HTTP {r.status_code}")
        try:
            payload = r.json(parse_float=Decimal) if r.content else {}
        except ValueError:
            payload = {"message": r.text[:200]}
        if r.status_code >= 400 or (isinstance(payload, dict) and payload.get("status") == "error"):
            msg = payload.get("message") or payload.get("error") if isinstance(payload, dict) else r.text
            raise RejectedError(f"{method} {path}: HTTP {r.status_code} {msg}", code=r.status_code)
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    # ------------------------------------------------------------------ public
    def markets(self) -> list[dict]:
        return self._request("GET", "/markets")

    def tickers(self) -> dict[str, dict]:
        return self._request("GET", "/markets/tickers")

    def klines(self, market: str, period_min: int, *, since_s: int | None = None, limit: int = 10000) -> list:
        return self._request("GET", f"/markets/{market}/k",
                             {"period": period_min, "timestamp": since_s, "limit": limit})

    def depth(self, market: str, limit: int = 5) -> dict:
        return self._request("GET", f"/markets/{market}/depth", {"limit": limit})

    # ------------------------------------------------------------------ authenticated
    def wallets(self) -> list[dict]:
        return self._request("GET", "/users/me/wallets", auth=True)

    def create_order(self, market: str, side: str, ord_type: str, volume: Decimal,
                     price: Decimal | None = None) -> dict:
        body = {"market": market, "side": side, "ord_type": ord_type, "volume": format(volume, "f")}
        if price is not None:
            body["price"] = format(price, "f")
        return self._request("POST", "/users/me/orders", body=body, auth=True, mutating=True)

    def get_order(self, order_id: str) -> dict | None:
        try:
            return self._request("GET", f"/users/me/orders/{order_id}", auth=True)
        except RejectedError as exc:
            if exc.code == 404:
                return None
            raise

    def list_orders(self, market: str, state: str | None = None, limit: int = 50) -> list[dict]:
        return self._request("GET", "/users/me/orders",
                             {"market": market, "state": state, "order_by": "desc", "per_page": limit}, auth=True)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/users/me/orders/{order_id}/cancel", auth=True, mutating=True)
