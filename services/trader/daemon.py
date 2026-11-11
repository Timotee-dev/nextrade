"""NEXTRADE trading daemon: the live/paper loop that ties every subsystem together.

Every cycle (poll):
  1. paper only: feed the latest quotes to the paper venue (fills stops/targets)
  2. sync protective exit orders; close positions whose stop or target filled
  3. if a new bar closed on the signal timeframe:
       a. NPEI: re-assess open positions (invalidation, time stop)
       b. strategies -> signals on the new bar -> NAIA -> NCTPI allocation -> NARA
       c. entries: market buy, then an OCO stop/target at the venue immediately
       d. equity snapshot and NARA halt check
  4. heartbeat (the watchdog halts trading if this stops)

Safety rules:
  * On start, the daemon reconciles with the venue before any entry is allowed.
  * A filled entry that cannot get its protective exit is closed at once.
  * The controller state decides whether entries are allowed; NARA can halt it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from adapters.base import RejectedError, VenueError
from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine, TransitionError
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.domain.ids import make_client_order_id
from nextrade_core.domain.instrument import Instrument
from nextrade_core.execution.engine import ExecutionEngine
from nextrade_core.execution.orders import OrderRequest, OrderSide, OrderStatus, OrderType
from nextrade_core.features.feature_set import FeatureParams, with_features
from nextrade_core.naia.engine import Naia
from nextrade_core.naia.scoring import MarketContext
from nextrade_core.nara.engine import Halt, RiskEngine, TradeProposal
from nextrade_core.nara.portfolio import OpenPosition, PortfolioSnapshot, open_risk
from nextrade_core.nctpi.allocation import Candidate, Funnel, allocate
from nextrade_core.npei.exits import ExitConfig, assess, thesis
from nextrade_core.regime.classifier import classify
from nextrade_core.strategies.base import Signal, Strategy

from .ports import MarketSource, TradingStore

log = logging.getLogger("nextrade.trader")
D0 = Decimal(0)


def dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(repr(float(x)))


@dataclass
class DaemonConfig:
    run_id: int
    mode: str                                   # "paper" or "live"
    symbols: list[str]
    timeframe: Timeframe = Timeframe.M15
    quote_asset: str = "USDT"
    history_bars: int = 0                        # 0 = feature warm-up + 50
    exit_config: ExitConfig = field(default_factory=ExitConfig)
    process_name: str = "trader"

    def bars_needed(self) -> int:
        return self.history_bars or FeatureParams().warmup_bars + 50


class TradingDaemon:
    def __init__(self, cfg: DaemonConfig, *, source: MarketSource, broker, store: TradingStore,
                 strategies: dict[str, Strategy], naia: Naia, risk: RiskEngine, costs: CostModel,
                 instruments: dict[str, Instrument], controller: TradingStateMachine, clock,
                 execution: ExecutionEngine | None = None, controls=None) -> None:
        self.cfg, self.source, self.broker, self.store = cfg, source, broker, store
        self.strategies, self.naia, self.risk, self.costs = strategies, naia, risk, costs
        self.instruments, self.controller, self.clock = instruments, controller, clock
        self.exec = execution or ExecutionEngine(broker, on_event=self._on_order_event)
        # callable returning [(target_state, actor, reason)] requested by other processes
        self.controls = controls
        self.last_bar: datetime | None = None
        self.funnel = Funnel()
        self.peak = self.day_start = self.week_start = None
        self._day = self._week = None
        self._order_position: dict[str, int | None] = {}
        self._order_purpose: dict[str, str] = {}

    # ------------------------------------------------------------------ persistence hook
    async def _on_order_event(self, order, what: str) -> None:
        cid = order.client_order_id
        self.store.save_order(self.cfg.run_id, order, self._order_purpose.get(cid, "entry"),
                              self._order_position.get(cid))
        if what == "reconcile_failed":
            self.store.alert("critical", "reconcile_failed", f"order {cid} state unknown after retries")
            self._halt(TradingState.EMERGENCY, Actor.NARA, f"order {cid} could not be reconciled")

    # ------------------------------------------------------------------ helpers
    def _base(self, symbol: str) -> str:
        return symbol[: -len(self.cfg.quote_asset)]

    def _halt(self, state: TradingState, actor: Actor, reason: str) -> None:
        try:
            self.controller.transition(state, actor, reason)
            self.store.alert("critical" if state is not TradingState.PAUSED else "warning", "halt", reason)
        except TransitionError:
            pass  # already halted at this level or above

    async def _mark(self) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
        """Equity, free quote cash and last prices (mid quotes, else last close)."""
        balances = await self.broker.balances()
        cash = balances.get(self.cfg.quote_asset, D0)
        prices: dict[str, Decimal] = {}
        equity = cash
        for sym in self.cfg.symbols:
            q = self.source.latest_quote(sym)
            if q:
                prices[sym] = (q[0] + q[1]) / 2
            else:
                f = self.source.frame(sym, self.cfg.timeframe, 1, self.clock.now())
                if len(f):
                    prices[sym] = dec(f["close"].iloc[-1])
            held = balances.get(self._base(sym), D0)
            if held and sym in prices:
                equity += held * prices[sym]
        return equity, cash, prices

    def _roll_periods(self, now: datetime, equity: Decimal) -> None:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = day - timedelta(days=day.weekday())
        if self.peak is None:
            history = self.store.equity_history(self.cfg.run_id)
            self.peak = max([e for _, e in history] + [equity])
        if day != self._day:
            self._day, self.day_start = day, equity
        if week != self._week:
            self._week, self.week_start = week, equity
        self.peak = max(self.peak, equity)

    def _consecutive_losses(self) -> int:
        n = 0
        for pnl in self.store.recent_closed_pnls(self.cfg.run_id, 50):
            if pnl is not None and pnl < 0:
                n += 1
            else:
                break
        return n

    async def _snapshot(self, now: datetime) -> PortfolioSnapshot:
        equity, cash, prices = await self._mark()
        self._roll_periods(now, equity)
        positions = tuple(
            OpenPosition(p["symbol"], dec(p["qty"]), dec(p["entry_price"]), dec(p["stop"]),
                         prices.get(p["symbol"], dec(p["entry_price"])))
            for p in self.store.open_positions(self.cfg.run_id))
        return PortfolioSnapshot(equity, cash, self.peak, self.day_start, self.week_start,
                                 self._consecutive_losses(), positions,
                                 {s: "crypto" for s in self.cfg.symbols})

    # ------------------------------------------------------------------ startup reconciliation
    async def reconcile(self) -> dict:
        """Compare stored open positions with the venue. Re-place missing protective
        exits (kill level 3). Positions with no coins at the venue are closed in the
        records and reported; they are never 're-bought'."""
        report = {"positions": 0, "protective_replaced": 0, "missing_at_venue": 0, "ok": True}
        balances = await self.broker.balances()
        for p in self.store.open_positions(self.cfg.run_id):
            report["positions"] += 1
            held = balances.get(self._base(p["symbol"]), D0)
            qty = dec(p["qty"])
            if held < qty * Decimal("0.99"):
                report["missing_at_venue"] += 1
                report["ok"] = False
                self.store.close_position(p["id"], closed_at=self.clock.now(), exit_reason="reconcile_missing",
                                          exit_price=None, realized_pnl=None)
                self.store.alert("critical", "reconcile", f"{p['symbol']}: position not found at venue")
                continue
            legs = [await self.broker.get(p["symbol"], f"{p['exit_list_id']}-{leg}") for leg in ("tp", "sl")]
            live = [s for s in legs if s is not None and s.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)]
            if not live:
                list_id = make_client_order_id(str(self.cfg.run_id), f"{p['id']}-{self.clock.now().isoformat()}", "exit")
                await self.broker.place_protective_exit(p["symbol"], qty, dec(p["stop"]), dec(p["target"]), list_id)
                self.store.update_position(p["id"], exit_list_id=list_id)
                report["protective_replaced"] += 1
                self.store.alert("warning", "reconcile", f"{p['symbol']}: protective exit re-placed")
        return report

    # ------------------------------------------------------------------ protective exits
    async def sync_exits(self) -> int:
        """Close positions whose venue stop or target has filled."""
        closed = 0
        for p in self.store.open_positions(self.cfg.run_id):
            if not p.get("exit_list_id"):
                continue
            for leg, reason in (("sl", "stop"), ("tp", "target")):
                state = await self.broker.get(p["symbol"], f"{p['exit_list_id']}-{leg}")
                if state is not None and state.status is OrderStatus.FILLED:
                    self._book_exit(p, state.avg_price, state.fee, reason)
                    closed += 1
                    break
        return closed

    def _book_exit(self, p: dict, price: Decimal, fee: Decimal, reason: str) -> None:
        now = self.clock.now()
        qty, entry = dec(p["qty"]), dec(p["entry_price"])
        proceeds = qty * price
        pnl = proceeds - fee - qty * entry - dec(p.get("entry_fee", 0))
        self.store.ledger(self.cfg.run_id, now, self.cfg.quote_asset, proceeds, "trade", p["symbol"])
        self.store.ledger(self.cfg.run_id, now, self.cfg.quote_asset, -fee, "fee", p["symbol"])
        self.store.ledger(self.cfg.run_id, now, self._base(p["symbol"]), -qty, "trade", p["symbol"])
        self.store.close_position(p["id"], closed_at=now, exit_price=price, exit_reason=reason,
                                  realized_pnl=pnl, fees=dec(p.get("entry_fee", 0)) + fee)
        self.funnel["positions_closed"] += 1
        self.funnel["net_winners" if pnl > 0 else "net_losers"] += 1
        self.store.alert("info", "position_closed", f"{p['symbol']} closed by {reason}, P&L {pnl:.4f} {self.cfg.quote_asset}")

    async def _exit_now(self, p: dict, reason: str) -> None:
        for leg in ("tp", "sl"):
            try:
                await self.broker.cancel(p["symbol"], f"{p['exit_list_id']}-{leg}")
            except (RejectedError, VenueError):
                pass
        balances = await self.broker.balances()
        inst = self.instruments[p["symbol"]]
        qty = inst.normalize_qty(min(dec(p["qty"]), balances.get(self._base(p["symbol"]), D0)))
        cid = make_client_order_id(str(self.cfg.run_id), f"{p['id']}", f"exit-{reason}")
        self._order_purpose[cid], self._order_position[cid] = "exit", p["id"]
        order = await self.exec.submit(OrderRequest(cid, p["symbol"], OrderSide.SELL, OrderType.MARKET, qty,
                                                    reduce_only=True))
        if order.status is OrderStatus.FILLED:
            self._book_exit({**p, "qty": order.filled_qty}, order.avg_price, order.fee, reason)
        else:
            self.store.alert("critical", "exit_failed", f"{p['symbol']}: exit order ended {order.status}")

    # ------------------------------------------------------------------ the bar cycle
    async def on_bar(self, now: datetime) -> dict:
        tf = self.cfg.timeframe
        frames: dict[str, pd.DataFrame] = {}
        for sym in self.cfg.symbols:
            raw = self.source.frame(sym, tf, self.cfg.bars_needed(), now)
            if len(raw) >= FeatureParams().warmup_bars:
                f = with_features(raw, tf)
                f["regime"] = classify(f)
                frames[sym] = f

        # a. open positions: NPEI and time stop
        for p in self.store.open_positions(self.cfg.run_id):
            f = frames.get(p["symbol"])
            if f is None:
                continue
            bars = p.get("bars_held", 0) + 1
            self.store.update_position(p["id"], bars_held=bars)
            sig = p["signal_obj"] if "signal_obj" in p else None
            strat = self.strategies.get(p["strategy"])
            if sig is None or strat is None:
                continue
            a = assess(sig, strat, fill=float(p["entry_price"]), current_stop=float(p["stop"]), bars_held=bars,
                       bar=f.iloc[-1], entry_regime=p.get("thesis", {}).get("regime"), config=self.cfg.exit_config)
            self.store.update_position(p["id"], health=a.health.value)
            reason = a.exit_reason or ("time" if bars >= sig.max_hold_bars else None)
            if reason:
                await self._exit_now({**p, "bars_held": bars}, reason)

        # b. new signals on this bar
        candidates: list[Candidate] = []
        meta: dict[str, tuple[Signal, int, object]] = {}
        for sym, f in frames.items():
            last_bar = f.index[-1]
            for code, strat in self.strategies.items():
                for sig in strat.generate(f.iloc[-300:] if len(f) > 300 else f, sym, tf):
                    if pd.Timestamp(sig.bar_time) != last_bar:
                        continue
                    self.funnel["signals"] += 1
                    regime = f["regime"].iloc[-1]
                    sid = self.store.record_signal(self.cfg.run_id, sig, regime)
                    q = self.source.latest_quote(sym)
                    spread_bps = float((q[1] - q[0]) / ((q[0] + q[1]) / 2) * 10_000) if q else None
                    ctx = MarketContext(data_ok=self.source.data_ok(sym, now), spread_bps=spread_bps, regime=regime)
                    opp = self.naia.evaluate(sig, ctx)
                    oid = self.store.record_opportunity(sid, opp)
                    if not opp.accepted:
                        self.funnel["rejected_by_naia"] += 1
                        continue
                    entry = dec(q[1]) if q else dec(sig.entry)
                    candidates.append(Candidate(sig.key, sym, opp.score,
                                                TradeProposal(sym, entry, dec(sig.stop),
                                                              dec(self.costs.round_trip_fraction(sym)),
                                                              spread_bps=dec(spread_bps) if spread_bps is not None else None)))
                    meta[sig.key] = (sig, oid, opp)

        # c. allocation + entries
        if candidates:
            snap = await self._snapshot(now)
            alloc = allocate(candidates, snap, self.risk, self.instruments,
                             entries_allowed=self.controller.allows_new_entries,
                             data_fresh={c.symbol: self.source.data_ok(c.symbol, now) for c in candidates})
            for cand, decision, _why in alloc.rejected:
                if decision is not None:
                    self.store.record_risk(meta[cand.key][1], decision)
                self.funnel["rejected_by_nara"] += 1
            for cand, decision in alloc.approved:
                sig, oid, opp = meta[cand.key]
                self.store.record_risk(oid, decision)
                self.funnel["approved"] += 1
                await self._enter(sig, decision.qty, opp, frames[sig.symbol]["regime"].iloc[-1])

        # d. equity + halts
        snap = await self._snapshot(now)
        risk_now = open_risk(snap.positions, self.risk.limits.gap_allowance)
        self.store.equity_snapshot(self.cfg.run_id, at=now, equity=snap.equity, cash=snap.free_cash,
                                   open_risk=risk_now, peak_equity=snap.peak_equity, drawdown=float(snap.drawdown))
        halt, why = self.risk.halts(snap)
        if halt is Halt.RISK_SHUTDOWN:
            self._halt(TradingState.RISK_SHUTDOWN, Actor.NARA, why)
        elif halt in (Halt.PAUSE_DAY, Halt.PAUSE_LOSSES) and self.controller.state in (TradingState.PAPER, TradingState.LIVE):
            self._halt(TradingState.PAUSED, Actor.NARA, why)
        return self.funnel.as_table()

    async def _enter(self, sig: Signal, qty: Decimal, opp, regime) -> None:
        run = str(self.cfg.run_id)
        cid = make_client_order_id(run, sig.key, "entry")
        self._order_purpose[cid] = "entry"
        order = await self.exec.submit(OrderRequest(cid, sig.symbol, OrderSide.BUY, OrderType.MARKET, qty))
        self.funnel["orders_submitted"] += 1
        if order.status is OrderStatus.PARTIALLY_FILLED:
            self.funnel["orders_partially_filled"] += 1
            await self.exec.cancel(order)
        if order.filled_qty <= 0:
            self.funnel["orders_rejected"] += 1
            return
        self.funnel["orders_filled"] += 1
        now = self.clock.now()
        fill, fee = order.avg_price, order.fee
        inst = self.instruments[sig.symbol]
        balances = await self.broker.balances()
        held = inst.normalize_qty(min(order.filled_qty, balances.get(self._base(sig.symbol), D0)))
        self.store.ledger(self.cfg.run_id, now, self.cfg.quote_asset, -(order.filled_qty * fill), "trade", sig.symbol)
        self.store.ledger(self.cfg.run_id, now, self.cfg.quote_asset, -fee, "fee", sig.symbol)
        self.store.ledger(self.cfg.run_id, now, self._base(sig.symbol), order.filled_qty, "trade", sig.symbol)
        pid = self.store.open_position(
            self.cfg.run_id, symbol=sig.symbol, strategy=sig.strategy, strategy_version=sig.strategy_version,
            qty=held, entry_price=fill, stop=dec(sig.stop), target=dec(sig.target),
            initial_risk=held * (fill - dec(sig.stop)), entry_fee=fee, opened_at=now, bars_held=0,
            thesis=thesis(sig, p=opp.p, expected_r=opp.expected_net_r, regime=regime), signal_obj=sig,
            exit_list_id="")
        self._order_position[cid] = pid
        self.store.save_order(self.cfg.run_id, order, "entry", pid)
        list_id = make_client_order_id(run, sig.key, "exit")
        try:
            await self.broker.place_protective_exit(sig.symbol, held, dec(sig.stop), dec(sig.target), list_id)
            self.store.update_position(pid, exit_list_id=list_id)
            self.store.alert("info", "trade_opened", f"{sig.symbol} {sig.strategy}: bought {held} at {fill:.6g}, "
                                                     f"stop {sig.stop:.6g}, target {sig.target:.6g}")
        except (VenueError, RejectedError) as exc:
            self.store.alert("critical", "protective_exit_failed", f"{sig.symbol}: {exc}; closing immediately")
            p = next(x for x in self.store.open_positions(self.cfg.run_id) if x["id"] == pid)
            await self._exit_now(p, "no_protection")

    # ------------------------------------------------------------------ loop
    def apply_controls(self) -> list[str]:
        """Apply operator/watchdog requests. Pausing and halting always work; resuming
        works only from PAUSED (a shutdown needs acknowledge_halt, never a message)."""
        results = []
        for target, actor, reason in (self.controls() if self.controls else []):
            try:
                if target in (TradingState.PAPER, TradingState.LIVE):
                    if self.controller.state is not TradingState.PAUSED:
                        results.append(f"resume refused: state is {self.controller.state.value}")
                        continue
                    target = TradingState.LIVE if self.cfg.mode == "live" else TradingState.PAPER
                    if target is TradingState.LIVE:
                        results.append("resume to live refused here: restart run_live after go_live_check")
                        continue
                self.controller.transition(target, actor, reason)
                results.append(f"{target.value}: ok")
                self.store.alert("warning" if target is TradingState.PAUSED else "critical"
                                 if target in (TradingState.EMERGENCY, TradingState.RISK_SHUTDOWN) else "info",
                                 "control", f"state -> {target.value} ({actor.value}): {reason}")
            except TransitionError as exc:
                results.append(f"{target.value}: {exc}")
        return results

    async def step(self) -> dict | None:
        """One poll: controls, quotes (paper), exits sync, bar cycle if a new bar closed, heartbeat."""
        now = self.clock.now()
        self.apply_controls()
        if self.cfg.mode == "paper" and hasattr(self.broker, "update_quote"):
            for sym in self.cfg.symbols:
                q = self.source.latest_quote(sym)
                if q:
                    self.broker.update_quote(sym, q[0], q[1])
        await self.sync_exits()
        result = None
        closed = [self.source.last_closed_bar(s, self.cfg.timeframe, now) for s in self.cfg.symbols]
        closed = [c for c in closed if c is not None]
        latest = max(closed) if closed else None
        if latest is not None and (self.last_bar is None or latest > self.last_bar):
            self.last_bar = latest
            result = await self.on_bar(now)
        self.store.heartbeat(self.cfg.process_name, now, {"state": self.controller.state.value,
                                                         "last_bar": str(self.last_bar)})
        return result

    async def run(self, stop: asyncio.Event, poll_s: float = 5.0) -> None:
        while not stop.is_set():
            try:
                await self.step()
            except VenueError as exc:
                log.warning("venue error in cycle: %s", exc)
                self.store.alert("warning", "venue_error", str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
            except TimeoutError:
                pass
