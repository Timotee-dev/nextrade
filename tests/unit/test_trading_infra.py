import threading
from collections import namedtuple
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from adapters.base import RejectedError
from adapters.mt5.client import MT5BridgeClient
from nextrade_core.data.sessions import ForexCalendar
from services.mt5_bridge.server import MT5Backend, serve
from services.watchdog import check_heartbeat

# ------------------------------------------------------------------ forex hours
CAL = ForexCalendar(holidays=frozenset({date(2026, 12, 25)}))


@pytest.mark.parametrize("when,open_", [
    (datetime(2026, 1, 9, 21, 59, tzinfo=UTC), True),     # Fri 16:59 New York (winter, UTC-5)
    (datetime(2026, 1, 9, 22, 0, tzinfo=UTC), False),     # Fri 17:00 New York: closed
    (datetime(2026, 1, 10, 12, 0, tzinfo=UTC), False),    # Saturday
    (datetime(2026, 1, 11, 21, 59, tzinfo=UTC), False),   # Sun 16:59 NY
    (datetime(2026, 1, 11, 22, 0, tzinfo=UTC), True),     # Sun 17:00 NY: open
    (datetime(2026, 7, 10, 20, 59, tzinfo=UTC), True),    # Fri 16:59 NY in summer (UTC-4)
    (datetime(2026, 7, 10, 21, 0, tzinfo=UTC), False),    # summer close moves an hour earlier in UTC
    (datetime(2026, 12, 25, 12, 0, tzinfo=UTC), False),   # broker holiday
])
def test_forex_week(when, open_):
    assert CAL.is_open(when) is open_


def test_reopen_buffer_and_maintenance():
    reopen = datetime(2026, 1, 11, 22, 0, tzinfo=UTC)
    assert CAL.is_open(reopen + timedelta(minutes=30)) and not CAL.tradable(reopen + timedelta(minutes=30))
    assert CAL.tradable(reopen + timedelta(minutes=61))
    w = datetime(2026, 1, 13, 3, 0, tzinfo=UTC)
    cal = ForexCalendar(maintenance=((w, w + timedelta(minutes=10)),))
    assert not cal.is_open(w + timedelta(minutes=5)) and cal.is_open(w + timedelta(minutes=10))


def test_sessions_overlap():
    assert set(CAL.active_sessions(datetime(2026, 1, 13, 13, tzinfo=UTC))) == {"london", "new_york"}
    assert "sydney" in CAL.active_sessions(datetime(2026, 1, 13, 23, tzinfo=UTC))


# ------------------------------------------------------------------ watchdog
def test_watchdog_rules():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert not check_heartbeat(None, now, "analysis").escalate
    assert check_heartbeat(None, now, "paper").escalate
    assert not check_heartbeat(now - timedelta(seconds=30), now, "live").escalate
    v = check_heartbeat(now - timedelta(seconds=90), now, "live")
    assert v.escalate and "90s" in v.reason


# ------------------------------------------------------------------ MT5 bridge
SymbolInfo = namedtuple("SymbolInfo", "point volume_step volume_min volume_max trade_contract_size "
                                      "currency_base currency_profit trade_mode")
Tick = namedtuple("Tick", "time bid ask")
Result = namedtuple("Result", "retcode order comment")


class FakeMT5:
    TIMEFRAME_M15 = 15

    def __init__(self):
        self.sent = []

    def initialize(self):
        return True

    def last_error(self):
        return (0, "ok")

    def symbol_info(self, symbol):
        return SymbolInfo(0.00001, 0.01, 0.01, 100.0, 100000.0, "EUR", "USD", 4) if symbol == "EURUSD" else None

    def symbol_info_tick(self, symbol):
        return Tick(1767225600, 1.1000, 1.1001)

    def copy_rates_from_pos(self, symbol, tf, start, count):
        dt = np.dtype([("time", "i8"), ("open", "f8"), ("close", "f8")])
        return np.array([(1767225600 + 900 * i, 1.1, 1.1001) for i in range(count)], dtype=dt)

    def order_send(self, request):
        self.sent.append(request)
        return Result(10009, 555, "done")

    def orders_get(self, symbol=None):
        return ()

    def positions_get(self, symbol=None):
        return ()


@pytest.fixture
def bridge():
    fake = FakeMT5()
    server = serve(MT5Backend(fake), token="a-long-bridge-token", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url, fake
    server.shutdown()


def test_bridge_round_trip(bridge):
    url, fake = bridge
    c = MT5BridgeClient(url, "a-long-bridge-token")
    assert c.health()
    inst = c.instrument("EURUSD")
    assert inst.contract_size == 100000 and str(inst.step_size) == "0.01" and inst.asset_class == "forex"
    assert c.tick("EURUSD")["ask"] == 1.1001
    rates = c.rates("EURUSD", "15m", 3)
    assert len(rates) == 3 and rates[1]["time"] - rates[0]["time"] == 900
    assert c.order_send({"symbol": "EURUSD", "volume": 0.01})["retcode"] == 10009 and fake.sent
    with pytest.raises(RejectedError):
        c.symbol_info("NOPE")


def test_bridge_rejects_bad_token_and_short_tokens(bridge):
    url, _ = bridge
    with pytest.raises(RejectedError, match="token"):
        MT5BridgeClient(url, "wrong-token-xxxxxxxx").tick("EURUSD")
    with pytest.raises(ValueError):
        serve(MT5Backend(FakeMT5()), token="short", port=0)
