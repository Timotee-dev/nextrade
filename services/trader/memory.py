"""In-memory MarketSource and TradingStore for tests and dry runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import count

import pandas as pd

from nextrade_core.data.candles import Timeframe


@dataclass
class FrameSource:
    """Serves bars from full frames, revealing only bars closed by `now`."""
    frames: dict[str, pd.DataFrame]
    quotes: dict[str, tuple[Decimal, Decimal, datetime]] = field(default_factory=dict)
    broken: set[str] = field(default_factory=set)

    def _closed(self, symbol: str, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
        f = self.frames[symbol]
        return f[f.index + timeframe.delta <= now]

    def frame(self, symbol, timeframe, bars, now):
        return self._closed(symbol, timeframe, now).iloc[-bars:]

    def last_closed_bar(self, symbol, timeframe, now):
        f = self._closed(symbol, timeframe, now)
        return f.index[-1].to_pydatetime() if len(f) else None

    def latest_quote(self, symbol):
        return self.quotes.get(symbol)

    def data_ok(self, symbol, now):
        return symbol not in self.broken


@dataclass
class MemoryStore:
    signals: dict[int, dict] = field(default_factory=dict)
    opportunities: dict[int, dict] = field(default_factory=dict)
    risks: dict[int, object] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)
    positions: dict[int, dict] = field(default_factory=dict)
    ledger_rows: list[tuple] = field(default_factory=list)
    equity_rows: list[dict] = field(default_factory=list)
    heartbeats: dict[str, datetime] = field(default_factory=dict)
    alerts: list[tuple[str, str, str]] = field(default_factory=list)
    _ids: count = field(default_factory=lambda: count(1))

    def record_signal(self, run_id, signal, regime):
        i = next(self._ids)
        self.signals[i] = {"run": run_id, "signal": signal, "regime": regime}
        return i

    def record_opportunity(self, signal_id, opp):
        i = next(self._ids)
        self.opportunities[i] = {"signal_id": signal_id, "opp": opp}
        return i

    def record_risk(self, opportunity_id, decision):
        self.risks[opportunity_id] = decision

    def save_order(self, run_id, order, purpose, position_id):
        self.orders[order.client_order_id] = {"order": order, "purpose": purpose, "position_id": position_id,
                                              "status": order.status}

    def open_position(self, run_id, **fields):
        i = next(self._ids)
        self.positions[i] = {"id": i, "run": run_id, "status": "open", **fields}
        return i

    def update_position(self, position_id, **fields):
        self.positions[position_id].update(fields)

    def close_position(self, position_id, **fields):
        self.positions[position_id].update(status="closed", **fields)

    def open_positions(self, run_id):
        return [dict(p) for p in self.positions.values() if p["run"] == run_id and p["status"] == "open"]

    def recent_closed_pnls(self, run_id, limit):
        closed = [p for p in self.positions.values() if p["run"] == run_id and p["status"] == "closed"]
        closed.sort(key=lambda p: p["closed_at"], reverse=True)
        return [p["realized_pnl"] for p in closed[:limit]]

    def ledger(self, run_id, at, asset, amount, kind, ref):
        self.ledger_rows.append((run_id, at, asset, amount, kind, ref))

    def equity_snapshot(self, run_id, **fields):
        self.equity_rows.append({"run": run_id, **fields})

    def equity_history(self, run_id):
        return [(r["at"], r["equity"]) for r in self.equity_rows if r["run"] == run_id]

    def heartbeat(self, process, at, detail):
        self.heartbeats[process] = at

    def alert(self, level, kind, message):
        self.alerts.append((level, kind, message))
