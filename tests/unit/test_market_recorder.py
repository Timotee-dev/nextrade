import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from nextrade_core.clock import SimClock
from nextrade_core.data.candles import Timeframe
from services.market_recorder import LiveRecorder
from tests.helpers import T0, minutes
from tests.unit.test_klines_stream import book_msg, kline_msg

TRUTH = minutes(20)


class MemorySink:
    def __init__(self):
        self.bars = {}      # (symbol, open_time) -> (candle, source)
        self.quotes = []
        self.issues = []
        self.details = []

    def last_open_time(self, symbol, timeframe=Timeframe.M1):
        times = [t for (s, t) in self.bars if s == symbol]
        return max(times) if times else None

    def save_candles(self, candles, source):
        for c in candles:
            self.bars.setdefault((c.symbol, c.open_time), (c, source))
        return len(candles)

    def save_quotes(self, quotes):
        self.quotes.extend(quotes)
        return len(quotes)

    def record_issue(self, kind, symbol, detail):
        self.issues.append(kind)
        self.details.append(detail)


class FakeSocket:
    def __init__(self, script):
        self.script = list(script)

    async def recv(self):
        while self.script:
            item = self.script.pop(0)
            if item == "HANG":  # never answers, like a half-dead connection
                self.script.insert(0, item)
                await asyncio.sleep(3600)
            if callable(item):
                result = item()
                if asyncio.iscoroutine(result):
                    await result
                continue
            return item
        raise ConnectionError("server closed the connection")


def fake_connector(scripts):
    scripts = list(scripts)

    @asynccontextmanager
    async def connect(url):
        if not scripts:
            raise ConnectionError("no more connections")
        yield FakeSocket(scripts.pop(0))
    return connect


def make_recorder(sink, scripts, state, **kw):
    def fetch_closed(symbol, start, now):
        return [c for c in TRUTH if start <= c.open_time <= TRUTH[state["limit"]].open_time]

    return LiveRecorder(["BTCUSDT"], sink, fetch_closed, fake_connector(scripts),
                        SimClock(T0 + timedelta(minutes=30)), max_backoff_s=0.01, **kw)


def test_backfill_stream_gap_repair_and_reconnect():
    sink, state = MemorySink(), {"limit": 9}
    conn1 = [lambda: state.update(limit=12), kline_msg(TRUTH[10]), kline_msg(TRUTH[13]),
             book_msg(), book_msg(), book_msg()]
    conn2 = [kline_msg(TRUTH[13]), kline_msg(TRUTH[14], closed=False)]
    rec = make_recorder(sink, [conn1, conn2], state, max_reconnects=1)
    asyncio.run(rec.run(asyncio.Event()))

    stored = sorted(t for (_, t) in sink.bars)
    assert stored == [c.open_time for c in TRUTH[:14]]           # 0..13, each once, forming bar 14 ignored
    sources = {t: src for (_, t), (_, src) in sink.bars.items()}
    assert sources[TRUTH[10].open_time] == "ws" and sources[TRUTH[13].open_time] == "ws"
    assert sources[TRUTH[11].open_time] == "rest" and sources[TRUTH[12].open_time] == "rest"
    assert rec.stats["gaps_repaired"] == 1 and rec.stats["connections"] == 2
    assert "ws_gap" in sink.issues and sink.issues.count("ws_disconnect") == 2
    assert len(sink.quotes) == 1                                   # same timestamp: sampled once


def test_quiet_stream_is_treated_as_dead():
    sink, state = MemorySink(), {"limit": 5}
    conn = ["HANG"]
    rec = make_recorder(sink, [conn], state, idle_timeout_s=0.3, max_reconnects=0)
    asyncio.run(asyncio.wait_for(rec.run(asyncio.Event()), timeout=4))
    assert rec.stats["reconnects"] == 1
    assert "StaleStreamError" in sink.details[0]["error"]
    assert len(sink.bars) == 6                                     # initial backfill happened


def test_stop_event_ends_cleanly():
    sink, state = MemorySink(), {"limit": 3}
    stop = asyncio.Event()
    rec = make_recorder(sink, [[lambda: stop.set(), book_msg()]], state)
    asyncio.run(asyncio.wait_for(rec.run(stop), timeout=4))
    assert rec.stats["reconnects"] == 0 and len(sink.bars) == 4
