from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.market.models import Instrument
from apps.research import governance as gov
from apps.research.models import ModelVersion
from apps.trading.models import Alert, OpportunityRecord, SignalRecord, TradingRun
from nextrade_core.data.candles import Timeframe
from nextrade_core.ml.registry import ArtifactIntegrityError, load_artifact, save_artifact
from nextrade_core.strategies.registry import make
from tests.integration.test_research_command import seed_minutes

pytestmark = pytest.mark.django_db
TF = Timeframe.M15


@pytest.fixture
def candidate(settings, tmp_path):
    settings.BASE_DIR = tmp_path
    seed_minutes("AAAUSDT", 40, seed=9)
    inst = {"AAAUSDT": Instrument.objects.get(symbol="AAAUSDT")}
    mv = gov.train_candidate("trend", "logreg", inst, TF, datetime(2026, 1, 1, tzinfo=UTC),
                             datetime(2026, 3, 1, tzinfo=UTC), train_days=20, test_days=5)
    return mv, inst


def test_artifact_integrity(tmp_path):
    path, sha = save_artifact([1, 2, 3], tmp_path, "v1")
    assert load_artifact(path, sha) == [1, 2, 3]
    Path(path).write_bytes(Path(path).read_bytes() + b"x")
    with pytest.raises(ArtifactIntegrityError):
        load_artifact(path, sha)


def test_training_records_everything_and_promotion_is_refused_without_evidence(candidate):
    mv, _ = candidate
    assert mv.status == "candidate" and mv.n_samples >= 50 and mv.dataset_hash and mv.artifact_sha256
    assert "brier" in mv.metrics["model"] and "brier" in mv.metrics["hitrate_baseline"]
    assert "pass" in mv.backtest_gate
    checks = gov.promote(mv, "operator", "try", risk_approved=True)
    mv.refresh_from_db()
    assert not all(c.passed for c in checks) and mv.status == "candidate"
    names = {c["name"]: c["passed"] for c in mv.checks}
    assert names["paper_validation"] is False and names["cost_aware_backtest"] is False
    with pytest.raises(CommandError, match="refused"):
        call_command("models", "promote", model_version=mv.version, reason="x", risk_approved=True)


def force_pass(mv):
    mv.n_samples = 1000
    mv.metrics = {"model": {"brier": 0.20, "brier_skill": 0.05}, "hitrate_baseline": {"brier": 0.22},
                  "folds": [{}, {}, {}]}
    mv.backtest_gate = {"pass": True, "reasons": []}
    mv.shadow_metrics = {"n": 150, "new": {"brier": 0.21}, "live": {"brier": 0.22}}
    mv.save()


def test_promote_then_rollback_restores_previous(candidate):
    mv, _ = candidate
    force_pass(mv)
    assert gov.promotion_checks(mv, risk_approved=False)[-1].passed is False
    assert all(c.passed for c in gov.promote(mv, "operator", "evidence reviewed", risk_approved=True))
    mv.refresh_from_db()
    assert mv.status == "production" and Alert.objects.filter(kind="model_promoted").exists()
    ensemble, version = ModelVersion.production_ensemble("trend")
    assert version == mv.version and len(ensemble) >= 1

    second = ModelVersion.objects.create(**{f.name: getattr(mv, f.name) for f in ModelVersion._meta.fields
                                            if f.name not in ("id", "version", "status", "replaced", "promoted_at")},
                                         version=mv.version + "-b")
    force_pass(second)
    second.metrics["model"]["brier"] = 0.19
    second.save()
    assert all(c.passed for c in gov.promote(second, "operator", "better", risk_approved=True))
    mv.refresh_from_db()
    assert mv.status == "retired"
    restored = gov.rollback("trend", "operator", "second misbehaved")
    assert restored.pk == mv.pk
    mv.refresh_from_db()
    second.refresh_from_db()
    assert mv.status == "production" and second.status == "retired"


def test_shadow_evaluation_uses_logged_paper_signals(candidate):
    mv, inst = candidate
    frames = gov.load_frames(inst, TF, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))
    signals = make("trend").generate(frames["AAAUSDT"], "AAAUSDT", TF)[-20:]
    run = TradingRun.objects.create(mode="paper", initial_equity=1000)
    ModelVersion.objects.filter(pk=mv.pk).update(created_at=signals[0].bar_time - timedelta(hours=1))
    mv.refresh_from_db()
    for s in signals:
        rec = SignalRecord.objects.create(run=run, key=s.key, strategy_version=s.strategy_version, symbol=s.symbol,
                                          timeframe="15m", bar_time=s.bar_time, entry=s.entry, stop=s.stop,
                                          target=s.target, max_hold_bars=s.max_hold_bars, features=s.features)
        OpportunityRecord.objects.create(signal=rec, accepted=False, reasons=[], p=0.35, p_source="hit_rate", cost_r=0.1)
    res = gov.shadow_evaluate(mv, inst, TF)
    assert res["n"] >= 10 and "brier" in res["new"] and "brier" in res["live"]
    mv.refresh_from_db()
    assert mv.shadow_metrics["n"] == res["n"]


def test_drift_alert():
    mv = ModelVersion.objects.create(strategy="trend", strategy_version="x", kind="logreg", version="v-drift",
                                     status="production", artifact_path="-", artifact_sha256="-", dataset_hash="d",
                                     feature_set="f", n_samples=500, metrics={"model": {"brier": 0.20}},
                                     shadow_metrics={"n": 200, "new": {"brier": 0.25}})
    assert gov.drift_check("trend")["status"] == "drift"
    assert Alert.objects.filter(kind="model_deterioration").exists()
    mv.shadow_metrics = {"n": 200, "new": {"brier": 0.205}}
    mv.save()
    assert gov.drift_check("trend")["status"] == "ok"
    assert gov.drift_check("meanrev")["status"] == "no data"


def test_models_list_command(candidate, capsys):
    call_command("models", "list")
    assert candidate[0].version in capsys.readouterr().out
