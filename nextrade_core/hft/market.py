"""In-memory market state per symbol, updated by every quote and trade. No database, no pandas: this is the hot path."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .events import Quote, Trade

TRADE_WINDOW_S = 180.0
MID_WINDOW_S = 180.0
MID_SAMPLE_S = 0.25
IMB_TAU_S = 3.0


@dataclass
class SymbolState:
    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    bid_sz: float = 0.0
    ask_sz: float = 0.0
    quote_ts: float = 0.0
    trades: deque = field(default_factory=deque)     # (ts, price, size, buy)
    mids: deque = field(default_factory=deque)       # (ts, mid), sampled every MID_SAMPLE_S
    imb_ema: float = 0.0
    quotes_seen: int = 0
    trades_seen: int = 0

    # ------------------------------------------------------------------ updates
    def on_quote(self, q: Quote) -> None:
        dt = q.ts - self.quote_ts if self.quote_ts else 0.0
        self.bid, self.ask, self.bid_sz, self.ask_sz, self.quote_ts = q.bid, q.ask, q.bid_sz, q.ask_sz, q.ts
        tot = q.bid_sz + q.ask_sz
        imb = (q.bid_sz - q.ask_sz) / tot if tot > 0 else 0.0
        a = 1 - math.exp(-dt / IMB_TAU_S) if dt > 0 else 1.0
        self.imb_ema += a * (imb - self.imb_ema)
        self.quotes_seen += 1
        mid = (q.bid + q.ask) / 2
        if not self.mids or q.ts - self.mids[-1][0] >= MID_SAMPLE_S:
            self.mids.append((q.ts, mid))
        while self.mids and q.ts - self.mids[0][0] > MID_WINDOW_S:
            self.mids.popleft()

    def on_trade(self, t: Trade) -> None:
        self.trades.append((t.ts, t.price, t.size, t.buy))
        self.trades_seen += 1
        while self.trades and t.ts - self.trades[0][0] > TRADE_WINDOW_S:
            self.trades.popleft()

    # ------------------------------------------------------------------ views
    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000 if self.mid > 0 else float("inf")

    @property
    def imbalance(self) -> float:
        tot = self.bid_sz + self.ask_sz
        return (self.bid_sz - self.ask_sz) / tot if tot > 0 else 0.0

    def fresh(self, now: float, max_age_s: float = 2.0) -> bool:
        return self.bid > 0 and self.ask > self.bid and now - self.quote_ts <= max_age_s

    def mid_ago(self, now: float, seconds: float) -> float | None:
        """Mid price about `seconds` ago (the newest sample at or before that time), None if history is too short."""
        cutoff = now - seconds
        for ts, mid in reversed(self.mids):
            if ts <= cutoff:
                return mid
        return None

    def ret_bps(self, now: float, seconds: float) -> float | None:
        old = self.mid_ago(now, seconds)
        return None if not old or self.mid <= 0 else (self.mid / old - 1) * 10_000

    def window_mids(self, now: float, seconds: float) -> list[float]:
        cutoff = now - seconds
        return [m for ts, m in self.mids if ts >= cutoff]

    def flow(self, now: float, seconds: float, until: float | None = None) -> tuple[float, int, float]:
        """(buy share of volume, trade count, volume) over the `seconds` before `now` (ending at `until` if given)."""
        cutoff, upper, buy, tot, n = now - seconds, now if until is None else until, 0.0, 0.0, 0
        for ts, _p, sz, is_buy in reversed(self.trades):
            if ts > upper:
                continue
            if ts < cutoff:
                break
            tot += sz
            n += 1
            if is_buy:
                buy += sz
        return (buy / tot if tot > 0 else 0.5), n, tot

    def vol_bps(self, now: float, seconds: float = 120.0) -> float:
        """Typical 1-second move in bps: standard deviation of mid changes over the window, scaled to 1 s."""
        w = self.window_mids(now, seconds)
        if len(w) < 20:
            return 0.0
        d = [(w[i] / w[i - 1] - 1) * 10_000 for i in range(1, len(w))]
        m = sum(d) / len(d)
        sd = math.sqrt(sum((x - m) ** 2 for x in d) / len(d))
        return sd * math.sqrt(1.0 / MID_SAMPLE_S)


class Market:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        self.state = {s: SymbolState(s) for s in symbols}
        self.events = 0

    def on_event(self, ev: Quote | Trade) -> SymbolState | None:
        st = self.state.get(ev.symbol)
        if st is None:
            return None
        self.events += 1
        if isinstance(ev, Quote):
            st.on_quote(ev)
        else:
            st.on_trade(ev)
        return st
