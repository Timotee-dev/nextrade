"""The engine's scalping mechanics, demonstrated on simulated data (no claim about profit)."""

import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pandas as pd
import pytest
from django.core.management import call_command

from nextrade_core.scalping import behaviour
from nextrade_core.scalping.reentry import ReentryConfig, ReentryGuard
from tests.unit.test_trader_daemon import SYMBOLS, TF, build, drive

pytestmark = pytest.mark.django_db


def test_behaviour_command_on_synthetic_data_demonstrates_the_mechanics(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    call_command("verify_scalping_behaviour", synthetic=True, days=20, timeframe="5m",
                 strategies="scalp_momentum,scalp_trend,scalp_meanrev,scalp_breakout")
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "say nothing about whether it makes money" in out
    assert "[PASS] multiple simultaneous positions" in out
    assert "[PASS] automatic closure" in out
    assert "[PASS] every entry uses a fresh signal" in out
    assert "[PASS] re-entry respects the cooldown" in out
    assert "[PASS] new positions opened while others are still open" in out
    assert "[PASS] re-entry guard used by the backtester" in out and "[PASS] re-entry guard used by the trader loop" in out
    # the net-of-cost entry gate and the net-profit exit are now wired into the simulator and the trader loop
    assert "[PASS] net-of-cost entry gate used by the backtester" in out
    assert "[PASS] net-profit exit used by the backtester" in out and "[PASS] net-profit exit used by the trader loop" in out
    assert "[PASS] short signals skipped on spot, counted as skipped" in out
    assert list((tmp_path / "research" / "validation").glob("behaviour-*.json"))


def test_checks_fail_when_behaviour_is_wrong():
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    row = lambda sym, key, e, x, pnl, why: dict(symbol=sym, signal_key=key, entry_time=t0 + pd.Timedelta(minutes=e),  # noqa: E731
                                                 exit_time=t0 + pd.Timedelta(minutes=x), pnl=pnl, reason=why)
    bad = pd.DataFrame([row("A", "k1", 0, 30, 1, "target"), row("A", "k1", 20, 60, -1, "stop")])   # reused key, reopened while open
    sigs = {"k1": t0}
    res = {n: ok for n, ok, _ in behaviour.evaluate(bad, sigs, n_signal_symbols=1, rejections={}, funnel={},
                                                     cooldown=timedelta(minutes=15), days=1)}
    assert res["every entry uses a fresh signal (no signal reused)"] is False
    assert res["re-entry after closing, never while still open"] is False
    assert behaviour.evaluate(pd.DataFrame(), {}, n_signal_symbols=0, rejections={}, funnel={},
                              cooldown=timedelta(minutes=15), days=1)[0][1] is False


def test_trader_loop_holds_several_positions_exits_and_reenters_under_the_guard():
    daemon, source, broker, store, clock, frames = build(rate=0.9, seed=3)
    daemon.reentry = ReentryGuard(ReentryConfig(cooldown=timedelta(minutes=30), after_loss=timedelta(minutes=30),
                                                flip_cooldown=timedelta(minutes=30), loss_streak=99))
    open_counts, entries_per_symbol = [], {}

    async def go():
        for _ in range(220):
            clock.advance_to(clock.now() + TF.delta)
            now = clock.now()
            for s, f in frames.items():
                closed = f[f.index + TF.delta <= now]
                c = D(repr(float(closed["close"].iloc[-1])))
                source.quotes[s] = (c * D("0.9999"), c * D("1.0001"), now)
            await daemon.step()
            op = store.open_positions(1)
            open_counts.append(len(op))
    asyncio.run(go())
    f = daemon.funnel.as_table()
    assert max(open_counts) >= 2                            # independent positions on different symbols at once
    assert f["positions_closed"] >= 3                       # exits happened automatically (stop, target or time)
    assert f["orders_filled"] > f["positions_closed"] - 1   # new positions kept opening after others closed
    assert f["rejected_by_reentry"] > 0                     # the guard blocked entries inside the cooldown
    assert {p["symbol"] for p in store.positions.values()} >= set(SYMBOLS)
