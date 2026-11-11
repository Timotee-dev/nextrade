from django.db import models


class ExperimentRun(models.Model):
    """One research run: what was tested, on which data, with which code, and the result."""

    created_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=32, default="walk_forward")
    strategy_version = models.CharField(max_length=64)
    model_kind = models.CharField(max_length=16)
    timeframe = models.CharField(max_length=4)
    symbols = models.JSONField(default=list)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    dataset_hash = models.CharField(max_length=32)
    git_commit = models.CharField(max_length=40, blank=True)
    config = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    baseline_summary = models.JSONField(default=dict)
    gate_pass = models.BooleanField(default=False)
    gate = models.JSONField(default=dict)
    folds = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.strategy_version} gate={'PASS' if self.gate_pass else 'FAIL'}"


class ModelVersion(models.Model):
    """A trained NAIA model ensemble for one strategy, and its path to production.

    candidate -> shadow -> production -> retired. Promotion is ALWAYS an operator command
    with every check recorded (spec 13). Models never touch risk limits: there is no
    code path from this table to RiskLimits.
    """

    class Status(models.TextChoices):
        CANDIDATE = "candidate"
        SHADOW = "shadow"
        PRODUCTION = "production"
        RETIRED = "retired"

    created_at = models.DateTimeField(auto_now_add=True)
    strategy = models.CharField(max_length=24)
    strategy_version = models.CharField(max_length=64)
    kind = models.CharField(max_length=16)
    version = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CANDIDATE)
    artifact_path = models.CharField(max_length=300)
    artifact_sha256 = models.CharField(max_length=64)
    dataset_hash = models.CharField(max_length=32)
    feature_set = models.CharField(max_length=40)
    n_samples = models.IntegerField()
    metrics = models.JSONField(default=dict)          # walk-forward out-of-sample probability metrics
    backtest_gate = models.JSONField(default=dict)    # cost-aware backtest decision gate
    shadow_metrics = models.JSONField(default=dict)   # paper/shadow evaluation
    checks = models.JSONField(default=list)           # promotion checklist results
    promoted_at = models.DateTimeField(null=True, blank=True)
    promoted_by = models.CharField(max_length=64, blank=True)
    promotion_reason = models.TextField(blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    replaced = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.version} ({self.status})"

    @classmethod
    def production_ensemble(cls, strategy: str):
        from nextrade_core.ml.registry import load_artifact
        mv = cls.objects.filter(strategy=strategy, status=cls.Status.PRODUCTION).first()
        if mv is None:
            return None
        return load_artifact(mv.artifact_path, mv.artifact_sha256), mv.version
