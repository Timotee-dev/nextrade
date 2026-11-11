"""Client for the MT5 bridge, used by NEXTRADE on any OS."""

from __future__ import annotations

from decimal import Decimal

import httpx

from adapters.base import RejectedError, RetryableError
from nextrade_core.domain.instrument import AssetClass, Instrument


class MT5BridgeClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 10.0) -> None:
        self._http = httpx.Client(base_url=base_url, headers={"X-Bridge-Token": token}, timeout=timeout_s,
                                  transport=transport)

    def _post(self, path: str, body: dict):
        try:
            r = self._http.post(path, json=body)
        except httpx.TransportError as exc:
            raise RetryableError(f"bridge {path}: {exc}") from exc
        if r.status_code == 401:
            raise RejectedError("bridge token rejected")
        if r.status_code >= 500:
            raise RetryableError(f"bridge {path}: {r.json().get('error')}")
        if r.status_code >= 400:
            raise RejectedError(f"bridge {path}: {r.json().get('error')}")
        return r.json()["result"]

    def health(self) -> bool:
        return self._http.get("/health").status_code == 200

    def symbol_info(self, symbol: str) -> dict:
        info = self._post("/symbol_info", {"symbol": symbol})
        if info is None:
            raise RejectedError(f"unknown symbol {symbol}")
        return info

    def instrument(self, symbol: str) -> Instrument:
        i = self.symbol_info(symbol)
        return Instrument(
            venue="mt5", symbol=symbol, base=i.get("currency_base", symbol[:3]),
            quote=i.get("currency_profit", symbol[3:6]), asset_class=AssetClass.FOREX,
            tick_size=Decimal(str(i["point"])), step_size=Decimal(str(i["volume_step"])),
            min_qty=Decimal(str(i["volume_min"])), min_notional=Decimal(0),
            max_qty=Decimal(str(i["volume_max"])), contract_size=Decimal(str(i["trade_contract_size"])),
            tradable=i.get("trade_mode", 4) != 0)

    def tick(self, symbol: str) -> dict:
        return self._post("/tick", {"symbol": symbol})

    def rates(self, symbol: str, timeframe: str, count: int = 500) -> list[dict]:
        return self._post("/rates", {"symbol": symbol, "timeframe": timeframe, "count": count})

    def order_send(self, request: dict) -> dict:
        return self._post("/order_send", {"request": request})

    def positions(self, symbol: str | None = None) -> list[dict]:
        return self._post("/positions", {"symbol": symbol} if symbol else {})
