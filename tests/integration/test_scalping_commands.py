"""Scalping commands end to end: backtest the scalp strategies, approvals, mode guards, scan."""

import json
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.research.models import ExperimentRun
from nextrade_core.scalping import approvals
from tests.integration.test_research_command import seed_minutes

pytestmark = pytest.mark.django_db


def test_scalp_strategies_run_through_the_real_decision_gate(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    seed_minutes("AAAUSDT", 40, seed=3)
    call_command("run_backtest", venue="binance", timeframe="5m", strategies="scalp_momentum,scalp_trend",
                 train_days=12, test_days=6, min_trades=5)
    out = capsys.readouterr().out
    assert "decision gate" in out and "scalp_momentum-v1-" in out and "scalp_trend-v1-" in out
    runs = ExperimentRun.objects.all()
    assert runs.count() == 2 and not any(r.gate_pass for r in runs)      # random walk: no edge to find


def test_cannot_approve_a_strategy_that_failed_or_never_ran(settings, tmp_path):
    settings.BASE_DIR = tmp_path
    with pytest.raises(CommandError, match="no recorded backtest"):
        call_command("approve_strategy", "scalp_momentum")
    ExperimentRun.objects.create(strategy_version="scalp_momentum-v1-abcd1234", model_kind="hitrate",
                                 timeframe="5m", symbols=["X"], period_start="2026-01-01T00:00:00Z",
                                 period_end="2026-02-01T00:00:00Z", dataset_hash="h", gate_pass=False,
                                 gate={"reasons": ["does not beat the random-entry baseline"]})
    with pytest.raises(CommandError, match="did NOT pass"):
        call_command("approve_strategy", "scalp_momentum")
    assert approvals.load(tmp_path) == {}


def test_approve_after_gate_pass_then_revoke(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    ExperimentRun.objects.create(strategy_version="scalp_trend-v1-abcd1234", model_kind="hitrate", timeframe="5m",
                                 symbols=["X"], period_start="2026-01-01T00:00:00Z",
                                 period_end="2026-02-01T00:00:00Z", dataset_hash="h", gate_pass=True, gate={})
    call_command("approve_strategy", "scalp_trend")
    ap = approvals.load(tmp_path)["scalp_trend"]
    assert ap.paper and not ap.live and "experiment #" in ap.evidence
    assert json.loads((tmp_path / "config" / "approved_strategies.json").read_text())["scalp_trend"]["live"] is False
    call_command("approve_strategy", "scalp_trend", revoke=True)
    assert approvals.load(tmp_path) == {}
    with pytest.raises(CommandError, match="Unknown strategy"):
        call_command("approve_strategy", "nonsense")


def test_run_scalper_guards(settings, tmp_path):
    settings.BASE_DIR = tmp_path
    with pytest.raises(CommandError, match="live mode is disabled"):
        call_command("run_scalper", mode="live")
    with pytest.raises(CommandError, match="no strategy is approved"):
        call_command("run_scalper", mode="paper")
    approvals.save(tmp_path, {"scalp_trend": approvals.Approval("scalp_trend", True, True, "e", "t")})
    monkey = pytest.MonkeyPatch()
    monkey.setenv("NEXTRADE_LIVE_ENABLED", "yes")
    try:
        with pytest.raises(CommandError, match="no live broker"):
            call_command("run_scalper", mode="live")          # enabled and approved, still no live broker
    finally:
        monkey.undo()


def test_run_scalper_paper_only_passes_approved_strategies(settings, tmp_path, monkeypatch):
    settings.BASE_DIR = tmp_path
    approvals.save(tmp_path, {"scalp_trend": approvals.Approval("scalp_trend", True, False, "e", "t"),
                              "trendfilter": approvals.Approval("trendfilter", True, False, "e", "t")})
    seen = {}
    from apps.trading.management.commands import run_scalper as cmd
    monkeypatch.setattr(cmd, "call_command", lambda name, **kw: seen.update(name=name, **kw))
    call_command("run_scalper", mode="paper")
    assert seen["name"] == "run_trader" and seen["strategies"] == "scalp_trend" and seen["mode"] == "paper"


def test_scan_scalp_reports_waiting_and_places_no_orders(settings, tmp_path, capsys):
    from apps.trading.models import OrderRecord
    settings.BASE_DIR = tmp_path
    seed_minutes("AAAUSDT", 12, seed=3)
    call_command("scan_scalp", venue="binance", timeframe="5m", capital="1000", days=12)
    out = capsys.readouterr().out
    assert "Scanned" in out and "unproven until they pass run_backtest" in out
    assert OrderRecord.objects.count() == 0
    assert "stale or missing data" in out        # seeded candles are old: they must not be scanned


def test_scalp_report_with_no_run_and_with_a_ledger(settings, tmp_path, capsys):
    from datetime import UTC, datetime, timedelta

    from apps.trading.models import Position, TradingRun
    call_command("scalp_report")
    assert "No paper run yet" in capsys.readouterr().out
    run = TradingRun.objects.create(mode="paper", initial_equity=Decimal(100), config={})
    t0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    for i, pnl in enumerate(("1.0", "-0.5", "0.7")):
        Position.objects.create(run=run, symbol="BTCUSDT", strategy="scalp_trend", strategy_version="v", qty=1,
                                entry_price=1, stop=1, target=1, initial_risk=Decimal("0.5"), status="closed",
                                opened_at=t0 + timedelta(hours=i), closed_at=t0 + timedelta(hours=i, minutes=10),
                                realized_pnl=Decimal(pnl), fees=Decimal("0.05"), exit_reason="target")
    call_command("scalp_report")
    out = capsys.readouterr().out
    assert "PAPER run" in out and "simulated, not real money" in out and "3 closed trades" in out
    assert "too few trades to judge" in out
