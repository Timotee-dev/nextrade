"""MT5 bridge (Windows only): exposes the MetaTrader 5 terminal to NEXTRADE over a
small authenticated HTTP API, because the MetaTrader5 Python package runs only on
Windows next to a running terminal.

    python -m services.mt5_bridge.server --token <secret> [--host 127.0.0.1 --port 8765]

Binds to localhost by default. Every request must carry X-Bridge-Token.
Status: tested against a fake MT5 module only. Use with a DEMO account first.
"""

from __future__ import annotations

import argparse
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TIMEFRAMES = {"1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
              "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1"}


def _plain(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "_asdict"):
        return {k: _plain(v) for k, v in obj._asdict().items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if hasattr(obj, "dtype") and hasattr(obj, "tolist"):   # numpy structured array from copy_rates
        names = obj.dtype.names
        return [dict(zip(names, (v.item() if hasattr(v, "item") else v for v in row))) for row in obj]
    return obj


class MT5Backend:
    def __init__(self, mt5_module) -> None:
        self.mt5 = mt5_module
        if not self.mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")

    def symbol_info(self, symbol: str):
        return _plain(self.mt5.symbol_info(symbol))

    def tick(self, symbol: str):
        return _plain(self.mt5.symbol_info_tick(symbol))

    def rates(self, symbol: str, timeframe: str, count: int):
        tf = getattr(self.mt5, TIMEFRAMES[timeframe])
        return _plain(self.mt5.copy_rates_from_pos(symbol, tf, 0, int(count)))

    def order_send(self, request: dict):
        return _plain(self.mt5.order_send(request))

    def orders(self, symbol: str | None = None):
        return _plain(self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get())

    def positions(self, symbol: str | None = None):
        return _plain(self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get())


def make_handler(backend: MT5Backend, token: str):
    routes = {
        "/symbol_info": lambda b: backend.symbol_info(b["symbol"]),
        "/tick": lambda b: backend.tick(b["symbol"]),
        "/rates": lambda b: backend.rates(b["symbol"], b["timeframe"], b.get("count", 500)),
        "/order_send": lambda b: backend.order_send(b["request"]),
        "/orders": lambda b: backend.orders(b.get("symbol")),
        "/positions": lambda b: backend.positions(b.get("symbol")),
    }

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            return hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), token)

        def do_GET(self):  # noqa: N802
            if not self._authorised():
                return self._reply(401, {"error": "unauthorised"})
            if self.path == "/health":
                return self._reply(200, {"ok": True})
            return self._reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authorised():
                return self._reply(401, {"error": "unauthorised"})
            fn = routes.get(self.path)
            if fn is None:
                return self._reply(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._reply(200, {"result": fn(body)})
            except (KeyError, ValueError) as exc:
                return self._reply(400, {"error": f"bad request: {exc}"})
            except Exception as exc:  # noqa: BLE001 - report terminal errors to the client
                return self._reply(502, {"error": f"mt5: {exc}"})

        def log_message(self, *args):  # keep the console quiet
            pass

    return Handler


def serve(backend: MT5Backend, token: str, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if len(token) < 16:
        raise ValueError("use a bridge token of at least 16 characters")
    return ThreadingHTTPServer((host, port), make_handler(backend, token))


def main() -> None:
    parser = argparse.ArgumentParser(description="NEXTRADE MT5 bridge (Windows)")
    parser.add_argument("--token", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    import MetaTrader5 as mt5  # noqa: N813  (Windows only)
    server = serve(MT5Backend(mt5), args.token, args.host, args.port)
    print(f"MT5 bridge on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
