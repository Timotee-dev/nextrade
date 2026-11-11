from .baseline import RandomEntry
from .breakout import Breakout
from .mean_reversion import MeanReversion
from .trend import TrendFollowing

STRATEGIES = {cls.code: cls for cls in (TrendFollowing, MeanReversion, Breakout, RandomEntry)}
TRADABLE = ("trend", "meanrev", "breakout")


def make(code: str, **overrides):
    cls = STRATEGIES[code]
    params = cls.default_params()
    if overrides:
        from dataclasses import replace
        params = replace(params, **overrides)
    return cls(params)
