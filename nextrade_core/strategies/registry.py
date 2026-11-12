from .baseline import RandomEntry
from .breakout import Breakout
from .forex import FxLondon, FxLondonAI, FxMeanRev, FxSweep, FxTrend, FxTsmom, FxXsmom
from .mean_reversion import MeanReversion
from .rotation import MomentumRotation
from .trend import TrendFollowing
from .trend_filter import TrendFilter

STRATEGIES = {cls.code: cls for cls in (TrendFollowing, MeanReversion, Breakout, RandomEntry, TrendFilter,
                                        MomentumRotation, FxTrend, FxMeanRev, FxLondon,
                                        FxSweep, FxTsmom, FxXsmom, FxLondonAI)}
TRADABLE = ("trend", "meanrev", "breakout")
PREREGISTERED = ("trendfilter", "rotation")   # ADR 0006
FOREX_PREREGISTERED = {"fx_trend": "1h", "fx_meanrev": "1h", "fx_london": "15m",      # ADR 0007: failed
                       "fx_sweep": "1h", "fx_tsmom": "1d", "fx_xsmom": "1d",          # ADR 0008: failed
                       "fx_london_ai": "15m"}                                        # ADR 0009


def make(code: str, **overrides):
    cls = STRATEGIES[code]
    params = cls.default_params()
    if overrides:
        from dataclasses import replace
        params = replace(params, **overrides)
    return cls(params)
