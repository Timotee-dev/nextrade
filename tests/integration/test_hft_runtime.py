"""HFT runtime against a local fake Bybit websocket: events flow, tape is recorded, state saved, only subscribe/ping are sent."""
import asyncio
import gzip
import json
from pathlib import Path

import pytest
import websockets

from adapters.bybit.ws import BybitWs
from nextrade_core.hft.engine import HftConfig
from nextrade_core.hft.exchange import InstrumentRules
from services.hft import runtime



def test_runtime_end_to_end_with_fake_server_and_reconnect(tmp_path: Path):
    received: list[dict] = []
    conns = {"n": 0}

    async def handler(ws):
        conns["n"] += 1
        n = conns["n"]
        try:
            i = 0
            async for raw in _merge(ws, received):
                pass
        except Exception:
            return

    async def _merge(ws, sink):
        async def pump():
            p = 100.0
            i = 0
            while True:
                i += 1
                p *= 1.00002
                await ws.send(json.dumps({"topic": "orderbook.1.BTCUSDT", "type": "snapshot" if i == 1 else "delta",
                                          "data": {"s": "BTCUSDT", "b": [[f"{p * 0.9999:.4f}", "5"]], "a": [[f"{p * 1.0001:.4f}", "5"]]}}))
                await ws.send(json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{"T": 1, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": f"{p:.4f}"}]}))
                if conns["n"] == 1 and i == 30:
                    await ws.close()
                    return
                await asyncio.sleep(0.02)
        task = asyncio.create_task(pump())
        try:
            async for raw in ws:
                sink.append(json.loads(raw))
                yield raw
        finally:
            task.cancel()

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}"
            return await runtime.run(
                tmp_path, ["BTCUSDT"], {"BTCUSDT": InstrumentRules(0.0001, 0.0001, 1.0)}, 27.0, HftConfig(),
                ws_factory=lambda syms, cb: BybitWs(syms, cb, url=url, sleep=_fast_sleep), max_seconds=4.0,
                summary_every_s=1.0, notify=lambda *a: None)

    async def _fast_sleep(s):
        await asyncio.sleep(min(s, 0.1))

    engine = asyncio.run(go())
    assert conns["n"] >= 2, "it must reconnect after the server drops it"
    assert engine.stats["quotes"] > 50 and engine.stats["trades"] > 50
    assert {m["op"] for m in received} <= {"subscribe", "ping"}
    status = json.loads(runtime.status_path(tmp_path).read_text())
    assert status["quotes"] > 50 and status["feed"]["reconnects"] >= 1
    assert runtime.state_path(tmp_path).exists()
    files = list((tmp_path / "data" / "ticks").glob("*.jsonl.gz"))
    assert files
    evs = list(runtime.read_ticks(files[0]))
    assert evs and evs[0].symbol == "BTCUSDT"
    snap = json.loads(runtime.state_path(tmp_path).read_text())
    assert snap["cash"] > 0


def test_stop_file_stops_the_runtime(tmp_path: Path):
    async def go():
        async def later():
            await asyncio.sleep(1.5)
            runtime.stop_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
            runtime.stop_path(tmp_path).write_text("stop")

        class Idle:
            stats = {}
            def __init__(self, *a, **k): pass
            async def run(self, stop):
                await stop.wait()
        t = asyncio.create_task(later())
        await runtime.run(tmp_path, ["X"], {}, 27.0, HftConfig(), ws_factory=lambda s, cb: Idle(), record_ticks=False, resume=False)
        await t
    asyncio.run(asyncio.wait_for(go(), 10))


def test_tick_recorder_prunes_and_throttles(tmp_path: Path):
    from nextrade_core.hft.events import Quote, Trade
    r = runtime.TickRecorder(tmp_path, min_gap_s=0.25)
    for i in range(10):
        r.write(Quote("X", 1_700_000_000 + i * 0.01, 1, 2, 1, 1))
    r.write(Trade("X", 1_700_000_000.2, 1.5, 1, True))
    r.close()
    assert r.written == 2
    f = next(tmp_path.glob("*.gz"))
    assert len(gzip.open(f, "rt").read().splitlines()) == 2


def test_runtime_sends_telegram_digest(tmp_path: Path):
    from services.hft.notifier import TelegramNotifier
    sent = []
    n = TelegramNotifier(lambda c, t: sent.append(t) or True, {1}, batch_s=0.05)

    class Idle:
        stats = {}
        def __init__(self, *a, **k): pass
        async def run(self, stop): await stop.wait()

    asyncio.run(runtime.run(tmp_path, ["X"], {}, 27.0, HftConfig(), ws_factory=lambda s, cb: Idle(), record_ticks=False,
                            resume=False, telegram=n, digest_every_s=0.5, max_seconds=3.0))
    n.close()
    assert any("open 0" in m and "equity 27.0000" in m for m in sent)


def test_runtime_telegram_gets_open_and_close_messages(tmp_path: Path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "unit"))
    from test_hft import Always
    from nextrade_core.hft.events import Quote
    from services.hft.notifier import TelegramNotifier
    monkeypatch.setattr(runtime, "make_all", lambda: {"always": Always(target=500.0, stop=500.0, hold=1.0)})
    sent = []
    n = TelegramNotifier(lambda c, t: sent.append(t) or True, {1}, batch_s=0.05)

    class Feed:
        stats = {}
        def __init__(self, syms, cb): self.cb = cb
        async def run(self, stop):
            while not stop.is_set():
                self.cb(Quote("X", time.time(), 99.99, 100.01, 500, 500))
                await asyncio.sleep(0.05)

    import time
    asyncio.run(runtime.run(tmp_path, ["X"], {"X": InstrumentRules(0.0001, 0.0001, 1.0)}, 27.0, HftConfig(cooldown_s=0.5),
                            ws_factory=lambda s, cb: Feed(s, cb), record_ticks=False, resume=False, telegram=n,
                            digest_every_s=0, max_seconds=5.0))
    n.close()
    text = "\n".join(sent)
    assert "OPENED X" in text and "CLOSED X" in text and "NET" in text
