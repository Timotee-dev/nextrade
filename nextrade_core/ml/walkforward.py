"""Out-of-sample predictions by purged walk-forward, with model-disagreement uncertainty."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from nextrade_core.features.feature_set import prune_correlated

from .models import CalibratedModel, evaluate, train_calibrated
from .splits import walk_forward


@dataclass
class WalkForwardResult:
    kind: str
    predictions: pd.DataFrame            # index aligned to the dataset: p, uncertainty, fold
    folds: list[dict] = field(default_factory=list)
    models: list[CalibratedModel] = field(default_factory=list)

    def metrics(self, dataset: pd.DataFrame) -> dict:
        joined = self.predictions.join(dataset[["y"]])
        return evaluate(joined["y"].to_numpy(), joined["p"].to_numpy()) if len(joined) else {}


def walk_forward_predict(dataset: pd.DataFrame, kind: str, feature_columns: list[str], *,
                         train: pd.Timedelta, test: pd.Timedelta, embargo: pd.Timedelta = pd.Timedelta(0),
                         ensemble: int = 3, prune: bool = True) -> WalkForwardResult:
    preds = []
    result = WalkForwardResult(kind, pd.DataFrame(columns=["p", "uncertainty", "fold"]))
    history: list[CalibratedModel] = []
    for fold in walk_forward(dataset["t"], dataset["t_end"], train, test, embargo=embargo):
        tr, te = dataset.iloc[fold.train_idx], dataset.iloc[fold.test_idx]
        feats = prune_correlated(tr, feature_columns) if (prune and kind != "hitrate") else feature_columns
        model = train_calibrated(tr, tr["y"].to_numpy(), kind, feats, seed=fold.number)
        history.append(model)
        p = model.predict(te)
        recent = history[-ensemble:]
        unc = np.std([m.predict(te) for m in recent], axis=0) if len(recent) > 1 else np.zeros(len(te))
        preds.append(pd.DataFrame({"p": p, "uncertainty": unc, "fold": fold.number}, index=te.index))
        result.folds.append({"fold": fold.number, "train_start": str(fold.train_start),
                             "test_start": str(fold.test_start), "n_train": len(tr), "n_test": len(te),
                             "features": len(feats), "notes": model.notes})
    result.models = history
    if preds:
        result.predictions = pd.concat(preds)
    return result
