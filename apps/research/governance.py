"""Model governance (spec 13): training candidates, shadow evaluation, gated promotion,
rollback and drift checks. Nothing here runs automatically; every state change is an
operator command, and the checks are stored with the model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.market.store import CandleStore
from apps.ops.models import AuditLog
from apps.trading.models import Alert, OpportunityRecord
from nextrade_core.backtest.research import ResearchConfig, evaluate_strategy
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, version_hash, with_features
from nextrade_core.ml.dataset import build_dataset, dataset_hash
from nextrade_core.ml.labels import simulate_exit
from nextrade_core.ml.models import evaluate, train_calibrated
from nextrade_core.ml.registry import save_artifact
from nextrade_core.ml.walkforward import walk_forward_predict
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.strategies.base import Side, Signal
from nextrade_core.strategies.registry import make

from .models import ModelVersion

MIN_SAMPLES = 300
MIN_FOLDS = 3
MIN_SHADOW = 100


def load_frames(instruments, tf: Timeframe, start, end) -> dict[str, pd.DataFrame]:
    store = CandleStore(instruments)
    frames = {}
    for sym in instruments:
        raw = store.load_frame(sym, tf, start, end)
        if len(raw) > FeatureParams().warmup_bars + 100:
            frames[sym] = with_features(raw, tf)
    return frames


def train_candidate(strategy_code: str, kind: str, instruments, tf: Timeframe, start, end, *,
                    train_days: int = 365, test_days: int = 90, ensemble: int = 3,
                    out_dir: Path | None = None) -> ModelVersion:
    strategy = make(strategy_code)
    frames = load_frames(instruments, tf, start, end)
    if not frames:
        raise ValueError("not enough stored history to train")
    costs = CostModel()
    signals = sorted((s for sym, f in frames.items() for s in strategy.generate(f, sym, tf)),
                     key=lambda s: s.decision_time)
    ds = build_dataset(frames, signals, costs)
    if len(ds) < 50:
        raise ValueError(f"only {len(ds)} resolved signals; need more history")
    features = list(signals[0].features)
    train, test = pd.Timedelta(days=train_days), pd.Timedelta(days=test_days)
    wf = walk_forward_predict(ds, kind, features, train=train, test=test, embargo=pd.Timedelta(days=1))
    base = walk_forward_predict(ds, "hitrate", features, train=train, test=test, embargo=pd.Timedelta(days=1))
    metrics = {"model": wf.metrics(ds), "hitrate_baseline": base.metrics(ds), "folds": wf.folds}
    research = evaluate_strategy(strategy, frames, {s: instruments[s].to_domain() for s in frames}, costs,
                                 RiskEngine(), ResearchConfig(train=train, test=test, model_kind=kind))
    # final ensemble: the newest `ensemble` training windows ending at the data's end
    t_end = ds["t"].max()
    models = []
    for k in range(ensemble):
        hi = t_end - test * k
        window = ds[(ds["t"] >= hi - train) & (ds["t_end"] <= hi)]
        if len(window) >= 30:
            models.append(train_calibrated(window, window["y"].to_numpy(), kind, features, seed=k))
    if not models:
        raise ValueError("not enough resolved signals in the newest training window to build a model")
    models.reverse()                                     # newest last
    version = f"{strategy_code}-{kind}-{datetime.now(UTC):%Y%m%d%H%M%S}"
    path, sha = save_artifact(models, out_dir or Path(settings.BASE_DIR) / "research" / "models", version)
    return ModelVersion.objects.create(
        strategy=strategy_code, strategy_version=strategy.version, kind=kind, version=version,
        artifact_path=path, artifact_sha256=sha, dataset_hash=dataset_hash(ds),
        feature_set=version_hash(FeatureParams()), n_samples=len(ds),
        metrics=_jsonable(metrics), backtest_gate=_jsonable(research.gate))


def _jsonable(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def shadow_evaluate(mv: ModelVersion, instruments, tf: Timeframe) -> dict:
    """Score the model on signals the paper trader logged while it was in shadow, using
    outcomes resolved from stored prices. Compared with the production p (or hit rate)
    recorded for the same signals."""
    from nextrade_core.ml.registry import load_artifact

    ensemble = load_artifact(mv.artifact_path, mv.artifact_sha256)
    model = ensemble[-1]
    opps = OpportunityRecord.objects.filter(signal__strategy_version=mv.strategy_version,
                                            signal__bar_time__gte=mv.created_at - timedelta(minutes=1)).select_related("signal")
    rows = []
    store = CandleStore(instruments)
    frames: dict[str, pd.DataFrame] = {}
    for o in opps:
        s = o.signal
        if s.symbol not in frames:
            frames[s.symbol] = store.load_frame(s.symbol, tf, s.bar_time - timedelta(days=1), timezone.now())
        sig = Signal(mv.strategy, s.strategy_version, s.symbol, Timeframe(s.timeframe), s.bar_time, Side.LONG,
                     float(s.entry), float(s.stop), float(s.target), s.max_hold_bars, "", None, s.features)
        out = simulate_exit(frames[s.symbol], sig, CostModel())
        if out is None or o.p is None:
            continue
        rows.append({"y": out.hit_target, "p_new": float(model.predict(pd.DataFrame([s.features]))[0]),
                     "p_live": o.p})
    if not rows:
        return {"n": 0}
    df = pd.DataFrame(rows)
    result = {"n": len(df), "new": evaluate(df.y.to_numpy(), df.p_new.to_numpy()),
              "live": evaluate(df.y.to_numpy(), df.p_live.to_numpy())}
    mv.shadow_metrics = _jsonable(result)
    mv.save(update_fields=["shadow_metrics"])
    return result


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def promotion_checks(mv: ModelVersion, risk_approved: bool) -> list[Check]:
    m = mv.metrics.get("model", {})
    base = mv.metrics.get("hitrate_baseline", {})
    prod = ModelVersion.objects.filter(strategy=mv.strategy, status=ModelVersion.Status.PRODUCTION).first()
    prod_brier = prod.metrics.get("model", {}).get("brier") if prod else base.get("brier")
    shadow = mv.shadow_metrics or {}
    checks = [
        Check("data_quality", bool(mv.dataset_hash) and mv.n_samples >= MIN_SAMPLES,
              f"{mv.n_samples} labelled samples (need {MIN_SAMPLES}), dataset {mv.dataset_hash}"),
        Check("out_of_sample", m.get("brier_skill", -1) > 0 and prod_brier is not None and m.get("brier", 1) < prod_brier,
              f"OOS Brier {m.get('brier')} vs incumbent {prod_brier}; skill {m.get('brier_skill')}"),
        Check("walk_forward", len(mv.metrics.get("folds", [])) >= MIN_FOLDS,
              f"{len(mv.metrics.get('folds', []))} folds (need {MIN_FOLDS})"),
        Check("cost_aware_backtest", bool(mv.backtest_gate.get("pass")),
              "; ".join(mv.backtest_gate.get("reasons", [])) or "decision gate passed"),
        Check("paper_validation", shadow.get("n", 0) >= MIN_SHADOW
              and shadow.get("new", {}).get("brier", 1) <= shadow.get("live", {}).get("brier", 0),
              f"{shadow.get('n', 0)} shadow outcomes (need {MIN_SHADOW})"),
        Check("risk_approval", risk_approved, "operator confirmed risk review" if risk_approved else "not given"),
    ]
    return checks


@transaction.atomic
def promote(mv: ModelVersion, operator: str, reason: str, risk_approved: bool) -> list[Check]:
    checks = promotion_checks(mv, risk_approved)
    mv.checks = [c.__dict__ for c in checks]
    if not all(c.passed for c in checks):
        mv.save(update_fields=["checks"])
        return checks
    previous = ModelVersion.objects.filter(strategy=mv.strategy, status=ModelVersion.Status.PRODUCTION).first()
    if previous:
        previous.status, previous.retired_at = ModelVersion.Status.RETIRED, timezone.now()
        previous.save(update_fields=["status", "retired_at"])
    mv.status, mv.promoted_at, mv.promoted_by, mv.promotion_reason = (
        ModelVersion.Status.PRODUCTION, timezone.now(), operator, reason)
    mv.replaced = previous
    mv.save()
    AuditLog.objects.create(actor=operator, action="promote_model", target=mv.version,
                            details={"reason": reason, "replaced": previous.version if previous else None})
    Alert.objects.create(level="warning", kind="model_promoted",
                         message=f"{mv.version} promoted to production for {mv.strategy}. Restart run_trader to load it.")
    return checks


@transaction.atomic
def rollback(strategy: str, operator: str, reason: str) -> ModelVersion | None:
    current = ModelVersion.objects.filter(strategy=strategy, status=ModelVersion.Status.PRODUCTION).first()
    if current is None:
        return None
    current.status, current.retired_at = ModelVersion.Status.RETIRED, timezone.now()
    current.save(update_fields=["status", "retired_at"])
    restored = current.replaced
    if restored is not None:
        restored.status, restored.retired_at = ModelVersion.Status.PRODUCTION, None
        restored.save(update_fields=["status", "retired_at"])
    AuditLog.objects.create(actor=operator, action="rollback_model", target=current.version,
                            details={"reason": reason, "restored": restored.version if restored else None})
    return restored


def drift_check(strategy: str, tolerance: float = 0.02) -> dict:
    """Compare the production model's recent live Brier score with its training-time OOS Brier."""
    mv = ModelVersion.objects.filter(strategy=strategy, status=ModelVersion.Status.PRODUCTION).first()
    if mv is None or not mv.shadow_metrics.get("n"):
        return {"status": "no data"}
    live = mv.shadow_metrics.get("new", {}).get("brier")
    trained = mv.metrics.get("model", {}).get("brier")
    if live is None or trained is None:
        return {"status": "no data"}
    drifted = live > trained + tolerance
    if drifted:
        Alert.objects.create(level="warning", kind="model_deterioration",
                             message=f"{mv.version}: live Brier {live:.4f} vs {trained:.4f} at training")
    return {"status": "drift" if drifted else "ok", "live_brier": live, "trained_brier": trained}

