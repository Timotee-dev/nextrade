"""Runtime for the high-frequency PAPER scalper: WebSocket feed -> engine, plus recorder, logs, snapshots and summaries.

Everything on the hot path is in memory. Disk writes are append-only JSONL (closed trades) and an hourly-rotated gzip tick file
(so strategies can later be replayed and validated on real tape), plus a state snapshot every few seconds for restart recovery.
Nothing here can send an order: the only outbound socket traffic is subscribe/ping (see adapters/bybit/ws.py).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from adapters.bybit.ws import BybitWs
from services.hft import notifier as tg
from nextrade_core.hft.engine import HftConfig, HftEngine
from nextrade_core.hft.events import Quote, Trade
from nextrade_core.hft.exchange import InstrumentRules, PaperExchange
from nextrade_core.hft.market import Market
from nextrade_core.hft.strategies import make_all

log = logging.getLogger("hft")


def lock_path(base: Path) -> Path:
    return base / "run" / "hft_scalper.lock"


def stop_path(base: Path) -> Path:
    return base / "run" / "HFT_STOP"


def state_path(base: Path) -> Path:
    return base / "run" / "hft_state.json"


def status_path(base: Path) -> Path:
    return base / "run" / "hft_status.json"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class TickRecorder:
    """Quotes (on change, at most every `min_gap_s` per symbol) and every trade, to hourly gzip JSONL files. Old files pruned."""

    def __init__(self, folder: Path, keep_hours: int = 72, min_gap_s: float = 0.25, clock: Callable[[], float] = time.time) -> None:
        self.folder, self.keep_hours, self.min_gap_s, self.clock = folder, keep_hours, min_gap_s, clock
        self._fh = None
        self._hour = ""
        self._last: dict[str, float] = {}
        self.written = 0

    def _open(self, ts: float) -> None:
        hour = datetime.fromtimestamp(ts, UTC).strftime("%Y%m%d-%H")
        if hour == self._hour:
            return
        if self._fh:
            self._fh.close()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._fh = gzip.open(self.folder / f"ticks-{hour}.jsonl.gz", "at", encoding="utf-8")
        self._hour = hour
        self._prune(ts)

    def _prune(self, ts: float) -> None:
        for p in self.folder.glob("ticks-*.jsonl.gz"):
            if ts - p.stat().st_mtime > self.keep_hours * 3600:
                p.unlink(missing_ok=True)

    def write(self, ev: Quote | Trade) -> None:
        if isinstance(ev, Quote):
            if ev.ts - self._last.get(ev.symbol, 0.0) < self.min_gap_s:
                return
            self._last[ev.symbol] = ev.ts
            row = ["q", ev.symbol, round(ev.ts, 3), ev.bid, ev.ask, ev.bid_sz, ev.ask_sz]
        else:
            row = ["t", ev.symbol, round(ev.ts, 3), ev.price, ev.size, 1 if ev.buy else 0]
        self._open(ev.ts)
        self._fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.written += 1

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def read_ticks(path: Path):
    """Yield Quote/Trade events from a recorder file, for replay."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r[0] == "q":
                yield Quote(r[1], r[2], r[3], r[4], r[5], r[6])
            else:
                yield Trade(r[1], r[2], r[3], r[4], bool(r[5]))


class TradeLog:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.pending_alerts: list[tuple[str, str, str]] = []

    def __call__(self, rec: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        day = datetime.fromtimestamp(rec.get("ts", time.time()), UTC).strftime("%Y%m%d")
        with open(self.folder / f"events-{day}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if rec.get("type") in ("halt", "pause"):
            self.pending_alerts.append(("critical" if rec["type"] == "halt" else "warning", "hft_" + rec["type"], rec["reason"]))


def build_engine(cfg: HftConfig, symbols: list[str], rules: dict[str, InstrumentRules], cash: float, sink,
                 taker: float = 0.001, maker: float = 0.001) -> HftEngine:
    ex = PaperExchange(cash=cash, taker_fee=taker, maker_fee=maker, rules=rules)
    return HftEngine(cfg, ex, Market(symbols), make_all(), sink=sink, initial_equity=cash)


def summary_text(s: dict, label: str = "HFT paper scalper") -> str:
    def f(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"
    pf = "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    wr = "-" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
    return (f"{label} (paper, simulated money)\n"
            f"Closed {s['closed']} in {s['hours']:.2f} h = {f(s['trades_per_hour'], 1)}/h | open {s['open']} (max {s['max_open']})\n"
            f"Net {s['net']:+.4f} USDT | gross {s['gross']:+.4f} | fees {s['fees']:.4f} | est. spread+slippage {s['friction_est']:.4f}\n"
            f"Win rate {wr} | PF {pf} | avg hold {f(s['avg_hold_s'], 1)} s | max DD {s['max_drawdown']:.2%} | equity {s['equity']:.4f}"
            + (f"\nHALTED: {s['halt']}" if s["halt"] else ""))


async def run(base: Path, symbols: list[str], rules: dict[str, InstrumentRules], cash: float, cfg: HftConfig, *,
              ws_factory: Callable[..., BybitWs] | None = None, summary_every_s: float = 600.0, snapshot_every_s: float = 5.0,
              notify: Callable[[str, str, str], None] | None = None, max_seconds: float = 0.0, record_ticks: bool = True,
              resume: bool = True, telegram: tg.TelegramNotifier | None = None, digest_every_s: float = 5.0) -> HftEngine:
    folder = base / "logs" / "hft"
    tlog = TradeLog(folder)
    holder: dict = {}

    def sink(rec: dict) -> None:
        tlog(rec)
        if telegram is None:
            return
        if rec["type"] == "open":
            telegram.push(tg.open_text(rec))
        elif rec["type"] == "close":
            telegram.push(tg.close_text(rec, holder["engine"].summary(rec["ts"])))
        elif rec["type"] in ("halt", "pause"):
            telegram.push(("HALT: " if rec["type"] == "halt" else "PAUSE: ") + rec["reason"])

    engine = build_engine(cfg, symbols, rules, cash, sink)
    holder["engine"] = engine
    sp = state_path(base)
    if resume and sp.exists():
        try:
            engine.restore(json.loads(sp.read_text()))
            log.info("restored state: cash %.4f, %d open positions", engine.ex.cash, len(engine.positions))
        except Exception as exc:                  # noqa: BLE001  a bad snapshot must not stop trading: start clean
            log.warning("state snapshot unreadable (%s): starting clean", exc)
    rec = TickRecorder(base / "data" / "ticks") if record_ticks else None
    stop = asyncio.Event()

    def on_event(ev):
        if rec is not None:
            rec.write(ev)
        engine.on_event(ev)

    ws = (ws_factory or BybitWs)(symbols, on_event)

    async def timer():
        while not stop.is_set():
            await asyncio.sleep(0.25)
            engine.on_timer(time.time())
            if tlog.pending_alerts and notify:
                for lvl, kind, msg in tlog.pending_alerts:
                    notify(lvl, kind, msg)
            tlog.pending_alerts.clear()

    async def housekeeping():
        last_sum = last_snap = last_dig = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(1.0)
            if stop_path(base).exists():
                stop.set()
                break
            if max_seconds and time.monotonic() - started > max_seconds:
                stop.set()
                break
            m = time.monotonic()
            st = engine.summary(time.time())
            st.update(feed=ws.stats, written_ticks=rec.written if rec else 0, symbols=len(symbols), ts=time.time(),
                      cfg=cfg.__dict__)
            atomic_write(status_path(base), json.dumps(st, default=str))
            if telegram is not None and digest_every_s and m - last_dig >= digest_every_s:
                last_dig = m
                telegram.push(tg.digest_text(st, engine.open_views()))
            if m - last_snap >= snapshot_every_s:
                last_snap = m
                atomic_write(sp, json.dumps(engine.snapshot()))
            if m - last_sum >= summary_every_s:
                last_sum = m
                if notify:
                    notify("info", "hft_summary", summary_text(st))

    started = time.monotonic()
    tasks = [asyncio.create_task(ws.run(stop)), asyncio.create_task(timer()), asyncio.create_task(housekeeping())]
    try:
        await stop.wait()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        atomic_write(sp, json.dumps(engine.snapshot()))
        if rec:
            rec.close()
    return engine
