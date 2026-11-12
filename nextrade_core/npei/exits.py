"""NPEI: is an open position still justified? (spec 7)

The venue stop and target always stay in place; NPEI can only tighten protection
or close early. Layers, in priority order (the backtester and the live daemon
check the venue stop/target and NARA's mandatory exits before calling this):

  3. thesis invalidation (the strategy's own rule)
  4. time stop (max holding bars) - enforced by the caller
  5. trailing stop, if enabled: after +trail_after_r, the stop follows price at
     trail_atr x ATR behind the close, never loosened
  6. model-based exit: not active. It runs in shadow mode only until it beats
     layers 1-5 out of sample (see compare_exit_policies).

Health labels describe measured conditions; they do not predict the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from nextrade_core.strategies.base import Signal, Strategy


class Health(StrEnum):
    HEALTHY = "healthy"
    DEVELOPING = "developing"
    DETERIORATING = "deteriorating"
    EXIT_RECOMMENDED = "exit_recommended"
    MANDATORY_EXIT = "mandatory_risk_exit"


@dataclass(frozen=True)
class ExitConfig:
    use_invalidation: bool = True
    trail_after_r: float | None = None     # None = no trailing (default until it proves itself)
    trail_atr: float = 2.0
    developing_band_r: float = 0.5
    deteriorating_r: float = 0.5


@dataclass(frozen=True)
class Assessment:
    health: Health
    r_now: float
    exit_reason: str | None = None
    new_stop: float | None = None
    notes: tuple[str, ...] = ()


def thesis(signal: Signal, *, p: float | None = None, expected_r: float | None = None,
           regime: str | None = None) -> dict:
    """What justified the entry; stored with the position and compared against later."""
    return {"strategy_version": signal.strategy_version, "invalidation": signal.invalidation,
            "invalidation_level": signal.invalidation_level, "stop": signal.stop, "target": signal.target,
            "max_hold_bars": signal.max_hold_bars, "p": p, "expected_r": expected_r, "regime": regime,
            "features": signal.features}


def assess(signal: Signal, strategy: Strategy, *, fill: float, current_stop: float, bars_held: int,
           bar: pd.Series, entry_regime: str | None = None, mandatory: bool = False,
           config: ExitConfig = ExitConfig()) -> Assessment:
    sign = signal.sign
    risk_unit = sign * (fill - signal.stop)
    close = float(bar["close"])
    r_now = sign * (close - fill) / risk_unit
    notes: list[str] = []
    if mandatory:
        return Assessment(Health.MANDATORY_EXIT, r_now, "risk_exit")
    if config.use_invalidation and strategy.is_invalidated(signal, bar):
        return Assessment(Health.EXIT_RECOMMENDED, r_now, "invalidated", notes=(signal.invalidation,))

    new_stop = None
    if config.trail_after_r is not None and r_now >= config.trail_after_r and "atr" in bar and pd.notna(bar["atr"]):
        trail = close - sign * config.trail_atr * float(bar["atr"])
        if sign * (trail - current_stop) > 0:
            new_stop = trail
            notes.append(f"trailing stop raised to {trail:.6g}")

    regime_now = bar.get("regime") if hasattr(bar, "get") else None
    regime_changed = entry_regime is not None and regime_now is not None and regime_now != entry_regime
    half_time = bars_held < signal.max_hold_bars / 2
    if r_now <= -config.deteriorating_r or regime_changed:
        health = Health.DETERIORATING
        if regime_changed:
            notes.append(f"regime {entry_regime} -> {regime_now}")
    elif abs(r_now) < config.developing_band_r and half_time:
        health = Health.DEVELOPING
    elif r_now > 0:
        health = Health.HEALTHY
    else:
        health = Health.DEVELOPING
    return Assessment(health, r_now, None, new_stop, tuple(notes))


def make_exit_policy(strategies: dict[str, Strategy], config: ExitConfig = ExitConfig()):
    """Adapter for the backtester's ExitPolicy hook. Applies trailing by raising the
    position's stop (never lowering it) and returns an exit reason when one fires."""
    def policy(position, bar: pd.Series, _i: int) -> str | None:
        strat = strategies[position.signal.strategy]
        a = assess(position.signal, strat, fill=float(position.fill), current_stop=position.stop,
                   bars_held=position.bars_held, bar=bar, config=config)
        if a.new_stop is not None and position.signal.sign * (a.new_stop - position.stop) > 0:
            position.stop = a.new_stop
        return a.exit_reason
    return policy


def compare_exit_policies(signals, frames, instruments, costs, risk, strategies: dict[str, Strategy],
                          configs: dict[str, ExitConfig], initial_cash=None) -> pd.DataFrame:
    """Run the same signals with plain exits (stop/target/time) and with each NPEI config.
    An exit layer is switched on only if it beats 'plain' out of sample."""
    from decimal import Decimal

    from nextrade_core.backtest.engine import Backtester
    from nextrade_core.backtest.metrics import bootstrap_mean_ci

    cash = initial_cash or Decimal(100000)
    rows = []
    variants = {"plain": None, **{name: make_exit_policy(strategies, cfg) for name, cfg in configs.items()}}
    for name, policy in variants.items():
        res = Backtester(frames, instruments, costs, risk, cash, exit_policy=policy).run(signals)
        r = res.trades["r_net"] if len(res.trades) else pd.Series(dtype=float)
        ci = bootstrap_mean_ci(r) if len(r) else None
        rows.append({"variant": name, "trades": len(r), "mean_r": float(r.mean()) if len(r) else None,
                     "ci_low": ci[0] if ci else None,
                     "avg_bars": float(res.trades["bars_held"].mean()) if len(r) else None,
                     "exit_reasons": res.trades["reason"].value_counts().to_dict() if len(r) else {}})
    return pd.DataFrame(rows)
