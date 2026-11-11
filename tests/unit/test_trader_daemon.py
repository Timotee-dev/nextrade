import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from adapters.base import RejectedError
from adapters.binance.filters import instrument_from_symbol
from adapters.paper.broker import PaperBroker
from nextrade_core.clock import SimClock
from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.execution.engine import ExecutionEngine
from nextrade_core.execution.orders import OrderStatus
from nextrade_core.execution.ratelimit import TokenBucket
from nextrade_core.ml.models import CalibratedModel, Calibrator, HitRateModel
from nextrade_core.naia.engine import Naia
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits
from nextrade_core.strategies.baseline import RandomEntry, RandomEntryParams
from services.trader.daemon import DaemonConfig, TradingDaemon
from services.trader.memory import FrameSource, MemoryStore
from tests.helpers import random_walk_frame

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
TF = Timeframe.M15
SYMBOLS = ["AAAUSDT", "BBBUSDT"]


def hit_rate_model(rate):
    m = HitRateModel()
    m.rate = rate
    return CalibratedModel("hitrate", [], m, Calibrator("none"), 100, rate)


# Synthetic bars are calm (stops ~0.5% away), so default limits would size positions near the
# whole account and the stress test would veto every trade. These limits let trades through.
TEST_LIMITS = dict(risk_per_trade=D("0.002"), max_stress_loss=D("0.5"), max_open_risk=D("0.05"),
                   max_cluster_risk=D("0.05"))


def build(rate=0.7, limits=None, broker_cls=PaperBroker, bars=1400, seed=0):
    frames = {s: random_walk_frame(bars, seed=seed + k, minutes_per_bar=15) for k, s in enumerate(SYMBOLS)}
    source = FrameSource(frames)
    broker = broker_cls(balances_={"USDT": D(10000)})
    instruments = {s: instrument_from_symbol(dict(INFO["symbols"][1], symbol=s)) for s in SYMBOLS}  # step 1, min 1 USDT
    strategy = RandomEntry(RandomEntryParams(probability=0.25, cooldown_bars=2, stop_atr=2.0, reward_risk=2.0, max_hold=16))
    naia = Naia(CostModel())
    naia.set_models("random", [hit_rate_model(rate)], "test")
    start = frames[SYMBOLS[0]].index[700].to_pydatetime()
    clock = SimClock(start)
    controller = TradingStateMachine(clock=clock)
    for st in (TradingState.DATA_INIT, TradingState.ANALYSIS, TradingState.PAPER):
        controller.transition(st, Actor.OPERATOR, "test start")
    store = MemoryStore()
    execution = ExecutionEngine(broker, limiter=TokenBucket(1000, 1000), retry_delay_s=0.001, reconcile_delay_s=0.001)
    daemon = TradingDaemon(DaemonConfig(run_id=1, mode="paper", symbols=SYMBOLS, timeframe=TF), source=source,
                           broker=broker, store=store, strategies={"random": strategy}, naia=naia,
                           risk=RiskEngine(limits or RiskLimits(**TEST_LIMITS)), costs=CostModel(),
                           instruments=instruments, controller=controller, clock=clock, execution=execution)
    execution.on_event = daemon._on_order_event
    return daemon, source, broker, store, clock, frames


def drive(daemon, source, clock, frames, steps):
    async def go():
        for _ in range(steps):
            clock.advance_to(clock.now() + TF.delta)
            now = clock.now()
            for s, f in frames.items():
                closed = f[f.index + TF.delta <= now]
                c = D(repr(float(closed["close"].iloc[-1])))
                source.quotes[s] = (c * D("0.9999"), c * D("1.0001"), now)
            await daemon.step()
    asyncio.run(go())


def test_paper_session_end_to_end():
    daemon, source, broker, store, clock, frames = build()
    drive(daemon, source, clock, frames, 200)
    closed = [p for p in store.positions.values() if p["status"] == "closed"]
    open_ = [p for p in store.positions.values() if p["status"] == "open"]
    f = daemon.funnel.as_table()
    assert f["signals"] > 40 and f["orders_filled"] >= 10 and len(closed) >= 5
    assert {p["exit_reason"] for p in closed} <= {"stop", "target", "time", "invalidated"}
    # every open position is protected at the venue
    for p in open_:
        legs = [broker.orders[f"{p['exit_list_id']}-{leg}"].status for leg in ("tp", "sl")]
        assert all(s is OrderStatus.OPEN for s in legs)
    # the ledger reconciles exactly with the venue's balances
    usdt = D(10000) + sum(r[3] for r in store.ledger_rows if r[2] == "USDT")
    assert usdt == broker.balances_["USDT"]
    for s in SYMBOLS:
        base = s[:-4]
        assert sum(r[3] for r in store.ledger_rows if r[2] == base) == broker.balances_.get(base, D(0))
    # no client order id was ever sent twice; heartbeats and equity were written
    assert len(broker.orders) == len(set(broker.orders))
    assert "trader" in store.heartbeats and len(store.equity_rows) >= 190
    assert f["positions_closed"] == f["net_winners"] + f["net_losers"] == len(closed)


def test_negative_expected_value_trades_nothing():
    daemon, source, broker, store, clock, frames = build(rate=0.2)
    drive(daemon, source, clock, frames, 60)
    assert daemon.funnel["signals"] > 0 and daemon.funnel["orders_submitted"] == 0
    assert all(not o["opp"].accepted for o in store.opportunities.values())


def test_reconcile_replaces_missing_protection_and_flags_missing_coins():
    daemon, source, broker, store, clock, frames = build()
    open_ = []
    for _ in range(40):                      # run until a position is open
        drive(daemon, source, clock, frames, 5)
        open_ = [p for p in store.positions.values() if p["status"] == "open"]
        if open_:
            break
    assert open_, "need an open position for this test"
    victim = open_[0]
    old_list_id = victim["exit_list_id"]
    for leg in ("tp", "sl"):
        broker.orders[f"{victim['exit_list_id']}-{leg}"].status = OrderStatus.CANCELED   # protection lost
    ghost = store.open_position(1, symbol="BBBUSDT", strategy="random", strategy_version="x", qty=D(10**6),
                                entry_price=D(1), stop=D("0.9"), target=D("1.2"), initial_risk=D(1),
                                entry_fee=D(0), opened_at=clock.now(), bars_held=0, thesis={}, exit_list_id="none")
    report = asyncio.run(daemon.reconcile())
    assert report["protective_replaced"] >= 1 and report["missing_at_venue"] == 1 and not report["ok"]
    assert store.positions[ghost]["exit_reason"] == "reconcile_missing"
    renewed = store.positions[victim["id"]]["exit_list_id"]
    assert renewed != old_list_id and broker.orders[f"{renewed}-sl"].status is OrderStatus.OPEN


def test_unprotectable_entry_is_closed_immediately():
    class NoOCO(PaperBroker):
        async def place_protective_exit(self, *a, **k):
            raise RejectedError("OCO not supported", code=-1013)

    daemon, source, broker, store, clock, frames = build(broker_cls=NoOCO)
    drive(daemon, source, clock, frames, 60)
    closed = [p for p in store.positions.values() if p["status"] == "closed"]
    assert closed and all(p["exit_reason"] == "no_protection" for p in closed)
    assert not [p for p in store.positions.values() if p["status"] == "open"]
    assert any(kind == "protective_exit_failed" for _, kind, _ in store.alerts)


def test_daily_loss_limit_pauses_trading():
    tight = RiskLimits(**{**TEST_LIMITS, "daily_loss": D("0.0005")})
    daemon, source, broker, store, clock, frames = build(limits=tight)
    drive(daemon, source, clock, frames, 120)
    assert daemon.controller.state in (TradingState.PAUSED, TradingState.RISK_SHUTDOWN)
    assert any(kind == "halt" for _, kind, _ in store.alerts)


def test_controls_pause_resume_and_refuse_resume_from_shutdown():
    daemon, *_ = build()
    requests = []
    daemon.controls = lambda: [requests.pop(0)] if requests else []
    requests.append((TradingState.PAUSED, Actor.OPERATOR, "telegram"))
    assert daemon.apply_controls() == ["paused: ok"] and not daemon.controller.allows_new_entries
    requests.append((TradingState.PAPER, Actor.OPERATOR, "telegram resume"))
    assert daemon.apply_controls() == ["paper: ok"] and daemon.controller.state is TradingState.PAPER
    requests.append((TradingState.EMERGENCY, Actor.WATCHDOG, "adopted"))
    daemon.apply_controls()
    requests.append((TradingState.PAPER, Actor.OPERATOR, "telegram resume"))
    assert daemon.apply_controls()[0].startswith("resume refused")
    assert daemon.controller.state is TradingState.EMERGENCY
