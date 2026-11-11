"""Probability models for NAIA: P(target before stop) for a given trade plan.

Three model kinds, simplest first (spec 4.6: complex models only if they beat simple ones):
* hitrate - the strategy's historical hit rate. No features. The baseline everything must beat.
* logreg  - standardised logistic regression.
* lgbm    - LightGBM gradient boosting, shallow and heavily regularised.

Every model is calibrated on the most recent slice of its training window (isotonic
or Platt scaling), because a ranking model's raw scores are not probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODEL_KINDS = ("hitrate", "logreg", "lgbm")


class HitRateModel:
    def __init__(self, prior: float = 0.5, prior_weight: float = 10.0) -> None:
        self.prior, self.prior_weight, self.rate = prior, prior_weight, prior

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> HitRateModel:
        # shrink toward the prior so a handful of trades cannot produce 0% or 100%
        self.rate = (y.sum() + self.prior * self.prior_weight) / (len(y) + self.prior_weight)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p = np.full(len(X), self.rate)
        return np.column_stack([1 - p, p])


def make_base(kind: str, seed: int = 0):
    if kind == "hitrate":
        return HitRateModel()
    if kind == "logreg":
        return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
    if kind == "lgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(n_estimators=200, learning_rate=0.03, num_leaves=7, max_depth=3,
                              min_child_samples=40, subsample=0.8, subsample_freq=1,
                              colsample_bytree=0.8, reg_lambda=1.0, random_state=seed, verbose=-1)
    raise ValueError(f"unknown model kind {kind!r}")


class Calibrator:
    def __init__(self, method: str = "isotonic") -> None:
        if method not in ("isotonic", "platt", "none"):
            raise ValueError(method)
        self.method = method
        self._iso: IsotonicRegression | None = None
        self._platt: LogisticRegression | None = None

    def fit(self, raw: np.ndarray, y: np.ndarray) -> Calibrator:
        if self.method == "isotonic":
            self._iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99).fit(raw, y)
        elif self.method == "platt":
            self._platt = LogisticRegression().fit(_logit(raw).reshape(-1, 1), y)
        return self

    def transform(self, raw: np.ndarray) -> np.ndarray:
        if self.method == "isotonic":
            return self._iso.predict(raw)
        if self.method == "platt":
            return self._platt.predict_proba(_logit(raw).reshape(-1, 1))[:, 1]
        return raw


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


@dataclass
class CalibratedModel:
    kind: str
    features: list[str]
    base: object
    calibrator: Calibrator
    n_train: int
    base_rate: float
    notes: list[str] = field(default_factory=list)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raw = self.base.predict_proba(X[self.features] if self.features else X)[:, 1]
        return np.clip(self.calibrator.transform(raw), 0.0, 1.0)

    def explain(self, x: pd.DataFrame, top: int = 5) -> dict[str, float]:
        """Largest per-feature contributions (log-odds) for ONE row."""
        if self.kind == "logreg":
            scaler, lr = self.base.named_steps["standardscaler"], self.base.named_steps["logisticregression"]
            contrib = scaler.transform(x[self.features])[0] * lr.coef_[0]
        elif self.kind == "lgbm":
            contrib = self.base.predict(x[self.features], pred_contrib=True)[0][:-1]
        else:
            return {}
        order = np.argsort(-np.abs(contrib))[:top]
        return {self.features[i]: round(float(contrib[i]), 4) for i in order}


def train_calibrated(X: pd.DataFrame, y: np.ndarray, kind: str, features: list[str],
                     calib_fraction: float = 0.25, calibration: str = "isotonic",
                     seed: int = 0) -> CalibratedModel:
    """Fit on the older part of the window, calibrate on the newest part (time order kept)."""
    y = np.asarray(y)
    n = len(y)
    if kind == "hitrate":
        base = HitRateModel().fit(X, y)
        return CalibratedModel(kind, [], base, Calibrator("none"), n, float(y.mean()))
    cut = int(n * (1 - calib_fraction))
    notes = []
    if cut < 50 or len(np.unique(y[:cut])) < 2 or len(np.unique(y[cut:])) < 2:
        notes.append("too little data for this model; fell back to hit rate")
        model = train_calibrated(X, y, "hitrate", features)
        model.notes = notes
        return model
    base = make_base(kind, seed).fit(X[features].iloc[:cut], y[:cut])
    raw_cal = base.predict_proba(X[features].iloc[cut:])[:, 1]
    cal = Calibrator(calibration).fit(raw_cal, y[cut:])
    return CalibratedModel(kind, list(features), base, cal, n, float(y.mean()), notes)


def evaluate(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict:
    """Probability-quality metrics. Accuracy is deliberately not reported (spec 4.6)."""
    y, p = np.asarray(y), np.clip(np.asarray(p), 1e-6, 1 - 1e-6)
    base = y.mean()
    brier = brier_score_loss(y, p)
    brier_ref = brier_score_loss(y, np.full(len(y), base))
    out = {"n": int(len(y)), "base_rate": float(base), "brier": float(brier),
           "brier_skill": float(1 - brier / brier_ref) if brier_ref > 0 else 0.0,
           "log_loss": float(log_loss(y, p, labels=[0, 1])),
           "auc": float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else None}
    edges = np.linspace(0, 1, bins + 1)
    which = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    out["calibration"] = [
        {"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int((which == b).sum()),
         "mean_p": float(p[which == b].mean()), "hit_rate": float(y[which == b].mean())}
        for b in range(bins) if (which == b).any()
    ]
    return out
