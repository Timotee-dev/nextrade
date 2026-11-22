"""The high-frequency paper scalper's brain: one event in, decisions out, no waiting.

For EVERY quote and trade of a symbol the engine (1) lets the paper exchange complete due orders, (2) manages that symbol's open
position against the live book (stop, net-profit trail, time limit, profit target) and, if it has none, (3) at most every
`eval_interval_s` asks the strategies for a fresh opportunity. Positions on different symbols are completely independent: opening
one never waits for another to close. A 250 ms timer completes in-flight orders and enforces time limits on quiet markets.

Safeguards that stay (catastrophic-loss protection, not trade throttles):
  max concurrent positions and total exposure, one position per market, sizing capped by risk per trade and by the displayed book,
  short cooldown after a close (longer after a loss), duplicate-signal block, per-strategy loss-streak pause, daily-loss entry pause
  (resumes next UTC day), drawdown halt that flattens everything and stays latched until the operator acknowledges it.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .events import Quote, Trade
from .exchange import DuplicateOrder, PaperExchange
from .market import Market, SymbolState
from .strategies import HSignal, HStrategy


@dataclass
class HftConfig:
    max_positions: int = 12
    pos_frac: float = 0.08            # target notional per position, fraction of equity
    risk_per_trade: float = 0.005     # max loss at the stop, fraction of equity
    max_exposure: float = 0.90        # all open notional, fraction of equity
    min_net_bps: float = 5.0          # a signal's target must plan at least this NET (after fees, spread, slippage)
    give_back: float = 0.35           # profit trail: fraction of the peak net profit that may be given back
    eval_interval_s: float = 0.2
    resignal_s: float = 5.0           # same strategy + symbol cannot signal again inside this
    cooldown_s: float = 10.0          # per symbol after a close
    cooldown_loss_s: float = 30.0     # per symbol after a net loss
    streak_losses: int = 6            # a strategy pauses after this many net losses in a row
    streak_pause_s: float = 300.0
    daily_loss: float = 0.03          # entries pause until the next UTC day
    max_drawdown: float = 0.10        # flatten and latch
    max_spread_bps: float = 10.0
    quote_max_age_s: float = 2.0
    exit_retry_s: float = 1.0
    stale_exit_s: float = 10.0
    entry_timeout_s: float = 5.0


@dataclass
class Position:
    id: int
    symbol: str
    strategy: str
    state: str                        # entering | open
    signal_mid: float
    stop_px: float
    target_px: float
    max_hold_s: float
    take_net_bps: float
    buy_id: str
    sent_ts: float
    qty: float = 0.0
    entry_fill: float = 0.0
    entry_fee: float = 0.0
    entry_qty: float = 0.0
    opened_ts: float = 0.0
    peak_net: float = 0.0
    target_id: str = ""
    exiting: str = ""
    exit_id: str = ""
    exit_sent: float = 0.0
    exit_n: int = 0
    exit_qty: float = 0.0
    exit_value: float = 0.0
    exit_fee: float = 0.0
    exit_kind: str = ""
    note: str = ""
    pending_notional: float = 0.0


def utc_day(ts: float) -> int:
    return int(datetime.fromtimestamp(ts, UTC).strftime("%Y%m%d"))


class HftEngine:
    def __init__(self, cfg: HftConfig, exchange: PaperExchange, market: Market, strategies: dict[str, HStrategy],
                 sink=None, initial_equity: float | None = None) -> None:
        self.cfg, self.ex, self.market, self.strategies = cfg, exchange, market, strategies
        self.sink = sink or (lambda rec: None)
        self.positions: dict[str, Position] = {}
        self._pid = 0
        self._last_eval: dict[str, float] = {}
        self._last_signal: dict[tuple[str, str], float] = {}
        self._last_close: dict[str, tuple[float, bool]] = {}
        self._streak: Counter = Counter()
        self._strategy_paused_until: dict[str, float] = {}
        self.initial_equity = initial_equity if initial_equity is not None else exchange.cash
        self.peak_equity = self.initial_equity
        self.day = 0
        self.day_start_equity = self.initial_equity
        self.halt = ""                 # latched: "drawdown"
        self.pause_day = 0             # entries paused for this UTC day (daily loss)
        self.max_dd = 0.0
        self.stats: Counter = Counter()
        self.signals_by: Counter = Counter()
        self.rejects: Counter = Counter()
        self.exits: Counter = Counter()
        self.by_strategy: dict[str, Counter] = {}
        self.sums = {"gross": 0.0, "fees": 0.0, "net": 0.0, "wins_net": 0.0, "losses_net": 0.0, "hold_s": 0.0, "friction": 0.0,
                     "notional": 0.0}
        self.max_open = 0
        self.first_ts = 0.0
        self.last_ts = 0.0
        self.proc_ns_total = 0
        self.proc_ns_max = 0
        self.recent: list[dict] = []

    # ------------------------------------------------------------------ small helpers
    def fees_bps(self) -> float:
        return (self.ex.taker_fee + self.ex.maker_fee) * 10_000

    def cost_bps(self, st: SymbolState) -> float:
        return self.fees_bps() + st.spread_bps / 2 + self.ex.slippage_bps

    def equity(self) -> float:
        eq = self.ex.cash
        for sym, qty in self.ex.holdings.items():
            if qty > 1e-12:
                st = self.market.state.get(sym)
                if st is not None and st.bid > 0:
                    eq += qty * st.bid
        return eq

    def exposure(self) -> float:
        tot = 0.0
        for p in self.positions.values():
            st = self.market.state[p.symbol]
            tot += (p.qty or 0.0) * (st.ask if st.ask > 0 else p.entry_fill) if p.state == "open" else p.pending_notional
        return tot

    # ------------------------------------------------------------------ event entry point
    def on_event(self, ev: Quote | Trade) -> None:
        st = self.market.on_event(ev)
        if st is None:
            return
        t0 = time.perf_counter_ns()
        now = ev.ts
        if not self.first_ts:
            self.first_ts = now
        self.last_ts = now
        self.stats["quotes" if isinstance(ev, Quote) else "trades"] += 1
        for f in self.ex.process(st, now, ev if isinstance(ev, Trade) else None):
            self._on_fill(f, st, now)
        pos = self.positions.get(st.symbol)
        if pos is not None:
            self._manage(pos, st, now)
        elif isinstance(ev, Quote) and now - self._last_eval.get(st.symbol, 0.0) >= self.cfg.eval_interval_s:
            self._last_eval[st.symbol] = now
            self._scan(st, now)
        dt = time.perf_counter_ns() - t0
        self.proc_ns_total += dt
        self.proc_ns_max = max(self.proc_ns_max, dt)

    def on_timer(self, now: float) -> None:
        """Complete in-flight orders, enforce limits on quiet markets, mark equity, apply the portfolio halts."""
        self.last_ts = max(self.last_ts, now)
        for sym, pos in list(self.positions.items()):
            st = self.market.state[sym]
            for f in self.ex.process(st, now):
                self._on_fill(f, st, now)
            if sym in self.positions:
                self._manage(self.positions[sym], st, now)
        eq = self.equity()
        day = utc_day(now)
        if day != self.day:
            self.day, self.day_start_equity = day, eq
            if self.pause_day and day != self.pause_day:
                self.pause_day = 0
        self.peak_equity = max(self.peak_equity, eq)
        dd = 1 - eq / self.peak_equity if self.peak_equity > 0 else 0.0
        self.max_dd = max(self.max_dd, dd)
        if not self.halt and dd >= self.cfg.max_drawdown:
            self.halt = "drawdown"
            self.sink({"type": "halt", "ts": now, "reason": f"drawdown {dd:.2%} reached {self.cfg.max_drawdown:.0%}: flattening"})
        if self.halt:
            for p in list(self.positions.values()):
                if p.state == "open" and not p.exiting:
                    self._exit(p, "halt", now)
        elif not self.pause_day and self.day_start_equity > 0 and 1 - eq / self.day_start_equity >= self.cfg.daily_loss:
            self.pause_day = day
            self.sink({"type": "pause", "ts": now, "reason": f"daily loss reached {self.cfg.daily_loss:.0%}: no new entries until the next UTC day"})

    # ------------------------------------------------------------------ entries
    def entries_allowed(self, now: float) -> str:
        if self.halt:
            return "halted"
        if self.pause_day:
            return "daily_loss_pause"
        return ""

    def _scan(self, st: SymbolState, now: float) -> None:
        if not st.fresh(now, self.cfg.quote_max_age_s):
            return
        why = self.entries_allowed(now)
        if why:
            return
        self.stats["scans"] += 1
        cost = self.cost_bps(st)
        for strat in self.strategies.values():
            sig = strat.evaluate(st, now, cost)
            if sig is not None:
                self._try_enter(sig, st, now)
                return

    def _reject(self, why: str) -> None:
        self.rejects[why] += 1

    def _try_enter(self, sig: HSignal, st: SymbolState, now: float) -> None:
        self.stats["signals"] += 1
        self.signals_by[sig.strategy] += 1
        k = (sig.strategy, sig.symbol)
        if now - self._last_signal.get(k, -1e9) < self.cfg.resignal_s:
            return self._reject("duplicate_signal")
        self._last_signal[k] = now
        if sig.symbol in self.positions:
            return self._reject("already_in_market")
        if len(self.positions) >= self.cfg.max_positions:
            return self._reject("max_positions")
        if st.spread_bps > self.cfg.max_spread_bps:
            return self._reject("spread_too_wide")
        lc = self._last_close.get(sig.symbol)
        if lc is not None and now - lc[0] < (self.cfg.cooldown_loss_s if lc[1] else self.cfg.cooldown_s):
            return self._reject("symbol_cooldown")
        if now < self._strategy_paused_until.get(sig.strategy, 0.0):
            return self._reject("strategy_paused_after_losses")
        planned = sig.target_bps - st.spread_bps / 2 - self.ex.slippage_bps - self.fees_bps()
        if planned < self.cfg.min_net_bps - 1e-9:
            return self._reject("net_target_below_costs")
        eq = self.equity()
        stop_frac = sig.stop_bps / 10_000
        notional = min(eq * self.cfg.pos_frac, eq * self.cfg.risk_per_trade / stop_frac)
        if self.exposure() + notional > eq * self.cfg.max_exposure:
            return self._reject("exposure_limit")
        rules = self.ex.rule(sig.symbol)
        qty = rules.floor_qty(notional / st.ask)
        if qty < rules.min_qty or qty * st.ask < rules.min_notional * 1.02:
            return self._reject("below_min_notional")
        if self.ex.cash < qty * st.ask * (1 + self.ex.taker_fee) * 1.001:
            return self._reject("insufficient_cash")
        self._pid += 1
        bid_id = f"{sig.key}:buy"
        try:
            self.ex.market_buy(bid_id, sig.symbol, qty, now)
        except DuplicateOrder:
            return self._reject("duplicate_order_id")
        self.positions[sig.symbol] = Position(
            self._pid, sig.symbol, sig.strategy, "entering", sig.mid, sig.mid * (1 - stop_frac),
            sig.mid * (1 + sig.target_bps / 10_000), sig.max_hold_s, sig.take_net_bps, bid_id, now, note=sig.note,
            pending_notional=qty * st.ask)
        self.stats["entries_sent"] += 1

    # ------------------------------------------------------------------ fills
    def _on_fill(self, f, st: SymbolState, now: float) -> None:
        pos = self.positions.get(f.symbol)
        if pos is None:
            return
        if f.side == "buy":
            pos.qty = pos.entry_qty = f.qty
            pos.entry_fill, pos.entry_fee, pos.opened_ts, pos.state = f.price, f.fee, f.ts, "open"
            self.stats["entries"] += 1
            self.by_strategy.setdefault(pos.strategy, Counter())["entries"] += 1
            self.max_open = max(self.max_open, len(self.positions))
            self._place_target(pos, now)
            self.sink({"type": "open", "ts": now, "symbol": pos.symbol, "strategy": pos.strategy, "qty": pos.qty,
                       "fill": pos.entry_fill, "fee": pos.entry_fee, "target": pos.target_px, "stop": pos.stop_px,
                       "note": pos.note, "open_positions": len(self.positions)})
            return
        pos.exit_qty += f.qty
        pos.exit_value += f.qty * f.price
        pos.exit_fee += f.fee
        pos.exit_kind = f.kind
        pos.qty = max(0.0, pos.qty - f.qty)
        if pos.qty <= 1e-12 or self.ex.holdings.get(f.symbol, 0.0) <= 1e-12:
            self._close(pos, st, now)

    def _place_target(self, pos: Position, now: float) -> None:
        pos.target_id = f"{pos.buy_id}:tp{pos.exit_n}"
        try:
            self.ex.limit_sell(pos.target_id, pos.symbol, pos.qty, pos.target_px, now)
        except DuplicateOrder:
            pos.exit_n += 1
            pos.target_id = f"{pos.buy_id}:tp{pos.exit_n}"
            self.ex.limit_sell(pos.target_id, pos.symbol, pos.qty, pos.target_px, now)

    def _close(self, pos: Position, st: SymbolState, now: float) -> None:
        self.positions.pop(pos.symbol, None)
        for oid in (pos.target_id, pos.exit_id):
            if oid:
                self.ex.cancel(oid, now)
        qty = pos.exit_qty or pos.entry_qty
        exit_fill = pos.exit_value / qty if qty else 0.0
        gross = pos.exit_value - pos.entry_fill * pos.entry_qty
        fees = pos.entry_fee + pos.exit_fee
        net = gross - fees
        notional = pos.entry_fill * pos.entry_qty
        reason = "target" if pos.exit_kind == "limit" else (pos.exiting or "exit")
        friction = (pos.entry_fill - pos.signal_mid) * pos.entry_qty + (st.mid - exit_fill) * qty if st.mid > 0 else 0.0
        rec = {"type": "close", "ts": now, "opened": pos.opened_ts, "symbol": pos.symbol, "strategy": pos.strategy, "reason": reason,
               "qty": pos.entry_qty, "entry": pos.entry_fill, "exit": exit_fill, "gross": gross, "fees": fees,
               "friction_est": friction, "net": net, "net_bps": net / notional * 1e4 if notional else 0.0,
               "hold_s": now - pos.opened_ts, "win": net > 0}
        s = self.sums
        s["gross"] += gross
        s["fees"] += fees
        s["net"] += net
        s["friction"] += friction
        s["hold_s"] += rec["hold_s"]
        s["notional"] += notional
        s["wins_net" if net > 0 else "losses_net"] += abs(net)
        self.stats["closed"] += 1
        self.stats["wins" if net > 0 else "losses"] += 1
        self.exits[reason] += 1
        bs = self.by_strategy.setdefault(pos.strategy, Counter())
        bs["closed"] += 1
        bs["wins" if net > 0 else "losses"] += 1
        bs["net_x1e8"] += int(net * 1e8)
        self._last_close[pos.symbol] = (now, net <= 0)
        self._streak[pos.strategy] = self._streak[pos.strategy] + 1 if net <= 0 else 0
        if self._streak[pos.strategy] >= self.cfg.streak_losses:
            self._strategy_paused_until[pos.strategy] = now + self.cfg.streak_pause_s
            self._streak[pos.strategy] = 0
            self.sink({"type": "pause", "ts": now, "reason": f"{pos.strategy}: {self.cfg.streak_losses} net losses in a row, paused {self.cfg.streak_pause_s:.0f}s"})
        self.recent = (self.recent + [rec])[-200:]
        self.sink(rec)

    # ------------------------------------------------------------------ open-position management (every event)
    def net_if_sold(self, pos: Position, st: SymbolState) -> float:
        px = st.bid * (1 - self.ex.slippage_bps / 10_000)
        return pos.qty * px * (1 - self.ex.taker_fee) - pos.entry_fill * pos.entry_qty * (pos.qty / pos.entry_qty if pos.entry_qty else 1) - pos.entry_fee * (pos.qty / pos.entry_qty if pos.entry_qty else 1)

    def _manage(self, pos: Position, st: SymbolState, now: float) -> None:
        if pos.state == "entering":
            o = self.ex.orders.get(pos.buy_id)
            if o is not None and o.status == "rejected":
                self.positions.pop(pos.symbol, None)
                self._reject(f"entry_rejected:{o.reason}")
            elif now - pos.sent_ts > self.cfg.entry_timeout_s:
                self.ex.cancel(pos.buy_id, now)
                self.positions.pop(pos.symbol, None)
                self._reject("entry_timeout")
            return
        if pos.exiting:
            o = self.ex.orders.get(pos.exit_id)
            if o is not None and o.status == "rejected" and now - pos.exit_sent >= self.cfg.exit_retry_s:
                pos.exiting = ""                              # try again on the next update
            return
        t = self.ex.orders.get(pos.target_id)
        if t is None or t.status in ("cancelled", "rejected"):
            self._place_target(pos, now)
        if not st.fresh(now, self.cfg.stale_exit_s):
            return
        reason = ""
        if st.bid <= pos.stop_px:
            reason = "stop"
        else:
            net = self.net_if_sold(pos, st)
            pos.peak_net = max(pos.peak_net, net)
            notional = pos.entry_fill * pos.qty
            arm = pos.take_net_bps / 10_000 * notional
            if pos.peak_net >= arm and net > 0 and net <= pos.peak_net * (1 - self.cfg.give_back):
                reason = "net_profit_trail"
            elif now - pos.opened_ts >= pos.max_hold_s:
                reason = "time_profit" if net > 0 else "time"
        if reason:
            self._exit(pos, reason, now)

    def _exit(self, pos: Position, reason: str, now: float) -> None:
        pos.exit_n += 1
        pos.exit_id = f"{pos.buy_id}:x{pos.exit_n}"
        pos.exiting, pos.exit_sent = reason, now
        held = self.ex.holdings.get(pos.symbol, 0.0)
        if pos.target_id:
            self.ex.cancel(pos.target_id, now)
        try:
            self.ex.market_sell(pos.exit_id, pos.symbol, min(pos.qty, held) if held > 0 else pos.qty, now)
        except DuplicateOrder:
            pos.exit_n += 1

    # ------------------------------------------------------------------ reporting + restart
    def summary(self, now: float | None = None) -> dict[str, Any]:
        now = now or self.last_ts
        n = self.stats["closed"]
        s = self.sums
        hours = max((now - self.first_ts) / 3600.0, 1e-9) if self.first_ts else 0.0
        eq = self.equity()
        return {
            "equity": eq, "initial_equity": self.initial_equity, "net": s["net"], "gross": s["gross"], "fees": s["fees"],
            "friction_est": s["friction"], "closed": n, "wins": self.stats["wins"], "losses": self.stats["losses"],
            "win_rate": self.stats["wins"] / n if n else None,
            "profit_factor": s["wins_net"] / s["losses_net"] if s["losses_net"] > 0 else None,
            "avg_hold_s": s["hold_s"] / n if n else None, "avg_net_bps": s["net"] / s["notional"] * 1e4 if s["notional"] else None,
            "trades_per_hour": n / hours if hours else None, "hours": hours, "max_drawdown": self.max_dd,
            "open": len(self.positions), "max_open": self.max_open,
            "quotes": self.stats["quotes"], "trades_seen": self.stats["trades"], "scans": self.stats["scans"],
            "signals": self.stats["signals"], "signals_by": dict(self.signals_by), "rejects": dict(self.rejects),
            "exits": dict(self.exits), "entries": self.stats["entries"],
            "avg_event_us": self.proc_ns_total / max(1, self.stats["quotes"] + self.stats["trades"]) / 1000.0,
            "max_event_us": self.proc_ns_max / 1000.0, "halt": self.halt, "pause_day": self.pause_day,
            "by_strategy": {k: dict(v) for k, v in self.by_strategy.items()},
        }

    def snapshot(self) -> dict:
        return {"cash": self.ex.cash, "holdings": dict(self.ex.holdings), "peak_equity": self.peak_equity,
                "initial_equity": self.initial_equity, "day": self.day, "day_start_equity": self.day_start_equity,
                "halt": self.halt, "pause_day": self.pause_day, "max_dd": self.max_dd, "sums": dict(self.sums),
                "stats": dict(self.stats), "exits": dict(self.exits), "max_open": self.max_open,
                "positions": [asdict(p) for p in self.positions.values() if p.state == "open"]}

    def restore(self, snap: dict) -> None:
        self.ex.cash = snap["cash"]
        self.ex.holdings = {k: v for k, v in snap.get("holdings", {}).items() if v > 1e-12}
        self.peak_equity, self.initial_equity = snap["peak_equity"], snap["initial_equity"]
        self.day, self.day_start_equity = snap.get("day", 0), snap.get("day_start_equity", self.initial_equity)
        self.halt, self.pause_day, self.max_dd = snap.get("halt", ""), snap.get("pause_day", 0), snap.get("max_dd", 0.0)
        self.sums.update(snap.get("sums", {}))
        self.stats.update(snap.get("stats", {}))
        self.exits.update(snap.get("exits", {}))
        self.max_open = snap.get("max_open", 0)
        for d in snap.get("positions", []):
            p = Position(**d)
            p.exiting, p.exit_id, p.target_id = "", "", ""       # in-flight orders do not survive a restart: re-issued on the first quote
            p.buy_id = f"{p.buy_id}:r{int(time.time())}"
            self.positions[p.symbol] = p
            self._pid = max(self._pid, p.id)
