from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.market.models import Instrument, QuoteSample
from apps.market.store import CandleStore
from apps.ops.models import SystemStateTransition
from apps.trading.management.commands.run_watchdog import watch_once
from apps.trading.models import Alert, EquitySnapshot, Heartbeat, TradingRun
from apps.trading.store import DjangoMarketSource
from nextrade_core.data.candles import Candle, Timeframe
from tests.helpers import random_walk_frame

pytestmark = pytest.mark.django_db


def seed_recent(symbol: str, days: int = 8, seed: int = 1) -> Instrument:
    inst = Instrument.objects.create(venue="binance", symbol=symbol, base=symbol[:-4], quote="USDT",
                                     asset_class="crypto", tick_size=Decimal("0.0001"), step_size=Decimal("0.01"),
                                     min_qty=Decimal("0.01"), min_notional=Decimal("1"))
    end = Timeframe.M1.floor(datetime.now(UTC)) - timedelta(minutes=1)
    n = days * 1440
    df = random_walk_frame(n, seed=seed, start=end - timedelta(minutes=n - 1))
    q = lambda x: Decimal(f"{x:.4f}")  # noqa: E731
    CandleStore({symbol: inst}).save_candles(
        [Candle(symbol, Timeframe.M1, t.to_pydatetime(), q(r.open), q(r.high), q(r.low), q(r.close),
                q(r.volume), q(r.quote_volume), int(r.trades)) for t, r in df.iterrows()], "archive")
    last = q(df.close.iloc[-1])
    QuoteSample.objects.create(instrument=inst, ts=timezone.now(), bid=last * Decimal("0.9999"),
                               ask=last * Decimal("1.0001"), bid_qty=1, ask_qty=1)
    return inst


def test_market_source_reads_recorder_output():
    inst = seed_recent("AAAUSDT", days=2)
    src = DjangoMarketSource({"AAAUSDT": inst})
    now = datetime.now(UTC)
    assert src.data_ok("AAAUSDT", now)
    assert src.latest_quote("AAAUSDT") is not None
    last = src.last_closed_bar("AAAUSDT", Timeframe.M15, now)
    assert last is not None and last + Timeframe.M15.delta <= now
    f = src.frame("AAAUSDT", Timeframe.M15, 50, now)
    assert len(f) == 50 and f.index[-1] == last


@pytest.mark.django_db(transaction=True)   # the daemon writes from its database worker thread
def test_run_trader_starts_reconciles_and_writes_state(capsys):
    seed_recent("AAAUSDT")
    call_command("run_trader", new_run=True, capital="1000", strategies="trend,breakout", poll=0, max_steps=2)
    out = capsys.readouterr().out
    assert "New paper run" in out and "Reconciliation" in out
    run = TradingRun.objects.get()
    assert run.mode == "paper" and run.initial_equity == 1000
    assert Heartbeat.objects.filter(process="trader").exists()
    assert EquitySnapshot.objects.filter(run=run).exists()
    states = list(SystemStateTransition.objects.order_by("at", "id").values_list("to_state", flat=True))
    assert states[:3] == ["data_init", "analysis", "paper"] and states[-1] == "analysis"


def test_live_mode_is_blocked_here():
    with pytest.raises(CommandError, match="run_live"):
        call_command("run_trader", mode="live")


def test_watchdog_declares_emergency_and_trader_refuses_until_acknowledged(capsys):
    seed_recent("AAAUSDT", days=1)
    from apps.ops.models import record_transition
    from nextrade_core.controller.state_machine import Actor, TradingState, TradingStateMachine
    m = TradingStateMachine(recorder=record_transition)
    for st in (TradingState.DATA_INIT, TradingState.ANALYSIS, TradingState.PAPER):
        m.transition(st, Actor.OPERATOR, "test")
    Heartbeat.objects.create(process="trader", at=timezone.now() - timedelta(minutes=5), detail={})
    assert "silent" in watch_once(60)
    assert Alert.objects.filter(kind="watchdog", level="critical").exists()
    with pytest.raises(CommandError, match="acknowledge_halt"):
        call_command("run_trader", poll=0, max_steps=1)
    call_command("acknowledge_halt", reason="checked the PC, trader had crashed")
    assert SystemStateTransition.objects.order_by("-at", "-id").first().to_state == "analysis"
    with pytest.raises(CommandError, match="Nothing to acknowledge"):
        call_command("acknowledge_halt", reason="again")
