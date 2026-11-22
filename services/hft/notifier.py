"""Live Telegram messages for the HFT paper scalper, sent straight from the engine's process (no outbox delay).

Messages are queued and sent by a background thread, so the trading loop never waits on the network. Messages that arrive within
`batch_s` of each other are joined into one Telegram message (Telegram allows roughly one message per second per chat).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

log = logging.getLogger("hft.telegram")


def fmt_px(x: float) -> str:
    return f"{x:.8g}" if x < 1 else f"{x:,.4f}".rstrip("0").rstrip(".")


def open_text(r: dict) -> str:
    notional = r["fill"] * r["qty"]
    tgt = (r["target"] / r["fill"] - 1) * 1e4
    stp = (r["stop"] / r["fill"] - 1) * 1e4
    return (f"OPENED {r['symbol']} ({r['strategy']})\n"
            f"Bought {r['qty']:g} @ {fmt_px(r['fill'])} = {notional:.2f} USDT, fee {r['fee']:.4f}\n"
            f"Target sell {fmt_px(r['target'])} ({tgt:+.0f} bps) | stop {fmt_px(r['stop'])} ({stp:+.0f} bps)\n"
            f"Why: {r.get('note', '')}\nOpen positions now: {r['open_positions']}")


def close_text(r: dict, running: dict | None = None) -> str:
    head = "WIN" if r["net"] > 0 else "LOSS"
    out = (f"CLOSED {r['symbol']} ({r['strategy']}) {head}\n"
           f"Bought {r['qty']:g} @ {fmt_px(r['entry'])} -> sold @ {fmt_px(r['exit'])} via {r['reason']}\n"
           f"Gross {r['gross']:+.4f} | fees {r['fees']:.4f} | NET {r['net']:+.4f} USDT ({r['net_bps']:+.1f} bps)\n"
           f"Held {r['hold_s']:.1f} s")
    if running:
        out += f"\nTotal: {running['closed']} closed, net {running['net']:+.4f} USDT, {running['trades_per_hour'] or 0:.0f}/h"
    return out


def digest_text(s: dict, views: list[dict]) -> str:
    now = datetime.now(UTC).strftime("%H:%M:%S")
    wr = "-" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
    pos = ", ".join(f"{v['symbol']} {v['net_bps']:+.0f}bp" for v in views[:8]) or "none"
    return (f"[{now} UTC] open {s['open']} ({pos}) | closed {s['closed']} ({s['wins']}W/{s['losses']}L, {wr}) | "
            f"net {s['net']:+.4f} gross {s['gross']:+.4f} fees {s['fees']:.4f} | equity {s['equity']:.4f} | "
            f"{s['trades_per_hour'] or 0:.0f}/h")


class TelegramNotifier:
    def __init__(self, send: Callable[[int, str], bool], chat_ids: set[int], batch_s: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._send, self.chat_ids, self.batch_s, self._sleep = send, sorted(chat_ids), batch_s, sleep
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=2000)
        self.sent = self.failed = self.dropped = 0
        self._t = threading.Thread(target=self._run, name="hft-telegram", daemon=True)
        self._t.start()

    def push(self, text: str) -> None:
        try:
            self._q.put_nowait(text)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            first = self._q.get()
            if first is None:
                return
            batch = [first]
            end = time.monotonic() + self.batch_s
            while time.monotonic() < end:
                try:
                    nxt = self._q.get(timeout=max(0.0, end - time.monotonic()))
                except queue.Empty:
                    break
                if nxt is None:
                    self._flush(batch)
                    return
                batch.append(nxt)
            self._flush(batch)

    def _flush(self, batch: list[str]) -> None:
        text = "\n\n".join(batch)
        for i in range(0, len(text), 3800):
            chunk = text[i:i + 3800]
            for cid in self.chat_ids:
                ok = False
                for attempt in range(3):
                    try:
                        ok = self._send(cid, chunk)
                    except Exception:                      # noqa: BLE001  never let a network problem kill the thread
                        ok = False
                    if ok:
                        break
                    self._sleep(1.5 * (attempt + 1))
                self.sent += ok
                self.failed += not ok

    def close(self, wait_s: float = 5.0) -> None:
        self._q.put(None)
        self._t.join(wait_s)
