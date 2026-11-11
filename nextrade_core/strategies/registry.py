from .baseline import RandomEntry
from .breakout import Breakout
from .mean_reversion import MeanReversion
from .rotation import MomentumRotation
from .trend import TrendFollowing
from .trend_filter import TrendFilter

STRATEGIES = {cls.code: cls for cls in (TrendFollowing, MeanReversion, Breakout, RandomEntry, TrendFilter,
                                        MomentumRotation)}
TRADABLE = ("trend", "meanrev", "breakout")
PREREGISTERED = ("trendfilter", "rotation")   # ADR 0006


def make(code: str, **overrides):
    cls = STRATEGIES[code]
    params = cls.default_params()
    if overrides:
        from dataclasses import replace
        params = replace(params, **overrides)
    return cls(params)
