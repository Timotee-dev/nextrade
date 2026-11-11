"""NAIA engine: signal -> calibrated p -> expected value -> opportunity or rejection.

Holds, per strategy, an ensemble of recent calibrated models (newest last). p is
the newest model's prediction; uncertainty is the spread across the ensemble.
With no model for a strategy, p is None and every signal is rejected: NAIA never
invents a probability.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from nextrade_core.costs.model import CostModel
from nextrade_core.ml.models import CalibratedModel
from nextrade_core.strategies.base import Signal

from .scoring import MarketContext, Opportunity, ScoringConfig, score_signal


@dataclass
class Naia:
    costs: CostModel
    config: ScoringConfig = field(default_factory=ScoringConfig)
    models: dict[str, list[CalibratedModel]] = field(default_factory=dict)
    rule_based: set[str] = field(default_factory=set)
    model_versions: dict[str, str] = field(default_factory=dict)

    def set_models(self, strategy: str, ensemble: list[CalibratedModel], version: str) -> None:
        if not ensemble:
            raise ValueError("ensemble must hold at least one model")
        self.models[strategy] = list(ensemble)
        self.model_versions[strategy] = version

    def mark_rule_based(self, codes) -> None:
        """Strategies with rule-based exits (ADR 0006): the hit-rate / model probability does not
        describe them, so only the context checks (data, market, spread, liquidity, costs) apply."""
        self.rule_based |= set(codes)

    def evaluate(self, signal: Signal, context: MarketContext) -> Opportunity:
        cost_r = self.costs.cost_in_r(signal.symbol, signal.entry, signal.stop)
        if signal.strategy in self.rule_based:
            from dataclasses import replace

            from .scoring import Reason
            opp = score_signal(signal, p=None, p_source="rule-based", uncertainty=0.0, cost_r=cost_r,
                               context=context, config=self.config)
            reasons = tuple(r for r in opp.reasons if r is not Reason.LOW_EXPECTED_VALUE)
            explanation = {**opp.explanation, "rejected_for": [r.value for r in reasons],
                           "note": "rule-based strategy (ADR 0006): judged on its tested raw record"}
            return replace(opp, accepted=not reasons, reasons=reasons, score=0.0, explanation=explanation)
        ensemble = self.models.get(signal.strategy)
        if not ensemble:
            return score_signal(signal, p=None, p_source="none", uncertainty=0.0, cost_r=cost_r,
                                context=context, config=self.config)
        x = pd.DataFrame([signal.features])
        preds = [float(m.predict(x)[0]) for m in ensemble]
        newest = ensemble[-1]
        p_source = "hit_rate" if newest.kind == "hitrate" else f"model:{self.model_versions[signal.strategy]}"
        return score_signal(signal, p=preds[-1], p_source=p_source,
                            uncertainty=float(np.std(preds)) if len(preds) > 1 else 0.0,
                            cost_r=cost_r, context=context, config=self.config,
                            contributions=newest.explain(x) if newest.kind != "hitrate" else {})

    def rank(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        """Accepted opportunities, best score first."""
        return sorted((o for o in opportunities if o.accepted), key=lambda o: o.score, reverse=True)
