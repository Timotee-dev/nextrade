"""A stand-in for the Windows-only MetaTrader5 package: server clock UTC+2, EURUSDc / USDJPYc."""

from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime, timedelta

import numpy as np

Info = namedtuple("Info", "name point digits volume_min volume_step volume_max trade_contract_size "
                          "currency_base currency_profit trade_mode")
Acct = namedtuple("Acct", "login server company currency balance equity leverage trade_mode")
Tick = namedtuple("Tick", "time bid ask")
RATE = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                 ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
    TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408
    MINUTES = {1: 1, 5: 5, 15: 15, 30: 30, 16385: 60, 16388: 240, 16408: 1440}
    OFFSET = timedelta(hours=2)

    def __init__(self, now: datetime, ok: bool = True, mode: int = 0):
        self.now, self.ok, self.mode, self.calls = now, ok, mode, []
        self.symbols = {
            "EURUSDc": Info("EURUSDc", 1e-05, 5, 0.01, 0.01, 200.0, 1000.0, "EUR", "USD", 4),
            "USDJPYc": Info("USDJPYc", 0.001, 3, 0.01, 0.01, 200.0, 1000.0, "USD", "JPY", 4),
        }

    def initialize(self, **kw):
        self.calls.append(("initialize", kw))
        return self.ok

    def last_error(self):
        return (-10003, "IPC initialize failed")

    def shutdown(self):
        self.calls.append(("shutdown",))

    def account_info(self):
        return Acct(5012345, "Exness-MT5Trial", "Exness", "USD", 1000.0, 1000.0, 500, self.mode)

    def symbol_select(self, name, enable):
        return name in self.symbols

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_info_tick(self, name):
        return Tick(int((self.now + self.OFFSET).timestamp()), 1.1, 1.1001)

    def copy_rates_range(self, name, tf, lo, hi):
        """Bars every tf minutes in server time, skipping weekends; spread 12 points."""
        step = timedelta(minutes=self.MINUTES[tf])
        base = 1.10 if name.startswith("EUR") else 150.0
        rows, t = [], lo
        while t < hi:
            if t.weekday() < 5:
                k = len(rows)
                c = base * (1 + 0.001 * np.sin(k / 7))
                rows.append((int(t.replace(tzinfo=UTC).timestamp()), c, c * 1.0005, c * 0.9995, c, 100, 12, 0))
            t += step
        return np.array(rows, dtype=RATE)
