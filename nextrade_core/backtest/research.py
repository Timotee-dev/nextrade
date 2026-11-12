"""Walk-forward research runner and the Phase 7 decision gate.

For each fold: train NAIA's model on the training window's signals only, then run
the portfolio backtest on the test window only. Folds' test trades are pooled into
one out-of-sample record. The same is done for a random-entry baseline that copies
the strategy's exits and fires about as often.

The gate passes only if, out of sample and after costs:
  * there are enough trades to judge (default 100; about 400 are needed to detect 0.1R),
  * the 95% bootstrap interval of mean R lies above zero, and
  * the mean R beats the random-entry baseline's mean R.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from nextrade_core.costs.model import CostModel
from nextrade_core.domain.instrument import Instrument
from nextrade_core.ml.dataset import build_dataset, dataset_hash
from nextrade_core.ml.models import train_calibrated
from nextrade_core.ml.splits import walk_forward
from nextrade_core.naia.engine import Naia
from nextrade_core.naia.scoring import MarketContext, ScoringConfig
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.strategies.base import Signal, Strategy
from nextrade_core.strategies.baseline import RandomEntry

from .engine import Backtester
from .metrics import bootstrap_mean_ci, summarize


@dataclass
class ResearchConfig:
    train: pd.Timedelta = pd.Timedelta(days=365)
    test: pd.Timedelta = pd.Timedelta(days=90)
    embargo: pd.Timedelta = pd.Timedelta(days=1)
    model_kind: str = "hitrate"
    initial_cash: Decimal = Decimal(1000)
    min_trades: int = 100
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    expanding: bool = False        # train on ALL earlier data (for rare signals, e.g. daily bars)
    leverage: Decimal = Decimal(1)  # margin accounts (forex); ignored for spot crypto
    holdout_from: pd.Timestamp | None = None  # count only trades decided on/after this (final confirmation)
    min_train_signals: int = 30


@dataclass
class ResearchResult:
    strategy: str
    folds: list[dict]
    trades: pd.DataFrame
    summary: dict
    baseline_summary: dict
    gate: dict
    dataset_hash: str
    raw_summary: dict = field(default_factory=dict)     # the strategy's own trades, NAIA filter off
    diagnostics: dict = field(default_factory=dict)     # signal counts, rejection reasons, hit rates


def _from(fold, cfg) -> pd.Timestamp:
    start = fold.test_start
    return max(start, cfg.holdout_from) if cfg.holdout_from is not None else start


def _signals_in(signals: list[Signal], start, end) -> list[Signal]:
    return [s for s in signals if start <= pd.Timestamp(s.decision_time) < end]


def evaluate_strategy(strategy: Strategy, frames: dict[str, pd.DataFrame], instruments: dict[str, Instrument],
                      costs: CostModel, risk: RiskEngine, cfg: ResearchConfig) -> ResearchResult:
    prepare = getattr(strategy, "prepare_frames", None)
    if prepare is not None:
        frames = prepare(frames)
    judged_raw = bool(getattr(strategy, "judged_raw", False))
    exit_policy = None
    tf = None
    signals: list[Signal] = []
    for sym, frame in frames.items():
        sigs = strategy.generate(frame, sym, _tf(frame))
        signals += sigs
        tf = tf or _tf(frame)
    signals.sort(key=lambda s: s.decision_time)
    n_bars = sum(len(f) for f in frames.values())
    baseline = RandomEntry.matching(strategy, probability=max(len(signals) / max(n_bars, 1), 1e-4))
    if getattr(strategy, "exit_on_invalidation", False):
        from nextrade_core.npei.exits import ExitConfig, make_exit_policy
        exit_policy = make_exit_policy({strategy.code: strategy, baseline.code: baseline},
                                       ExitConfig(use_invalidation=True))
    base_signals = sorted((s for sym, f in frames.items() for s in baseline.generate(f, sym, _tf(f))),
                          key=lambda s: s.decision_time)

    ds = build_dataset(frames, signals, costs)
    features = list(signals[0].features) if signals else []
    folds, oos_trades, base_trades, raw_trades = [], [], [], []
    rejections_total: dict[str, int] = {}
    if len(ds):
        for fold in walk_forward(ds["t"], ds["t_end"], cfg.train, cfg.test, embargo=cfg.embargo,
                                 expanding=cfg.expanding, min_train=cfg.min_train_signals):
            if cfg.holdout_from is not None and fold.test_end <= cfg.holdout_from:
                continue
            train_ds = ds.iloc[fold.train_idx]
            model = train_calibrated(train_ds, train_ds["y"].to_numpy(), cfg.model_kind, features, seed=fold.number)
            naia = Naia(costs, cfg.scoring)
            naia.set_models(strategy.code, [model], f"{cfg.model_kind}-fold{fold.number}")
            scorer = lambda s, n=naia: n.evaluate(s, MarketContext())  # noqa: E731
            test_frames = {sym: f[(f.index >= fold.test_start - pd.Timedelta(days=0)) & (f.index < fold.test_end)]
                           for sym, f in frames.items()}
            bt = Backtester(test_frames, instruments, costs, risk, cfg.initial_cash, leverage=cfg.leverage,
                            scorer=None if judged_raw else scorer, exit_policy=exit_policy)
            res = bt.run(_signals_in(signals, _from(fold, cfg), fold.test_end))
            raw_res = Backtester(test_frames, instruments, costs, risk, cfg.initial_cash, leverage=cfg.leverage, exit_policy=exit_policy).run(
                _signals_in(signals, _from(fold, cfg), fold.test_end))
            if len(raw_res.trades):
                raw_trades.append(raw_res.trades.assign(fold=fold.number))
            for k, v in res.rejections.items():
                rejections_total[k] = rejections_total.get(k, 0) + v
            base_bt = Backtester(test_frames, instruments, costs, risk, cfg.initial_cash, leverage=cfg.leverage, exit_policy=exit_policy)
            base_res = base_bt.run(_signals_in(base_signals, _from(fold, cfg), fold.test_end))
            if len(res.trades):
                oos_trades.append(res.trades.assign(fold=fold.number))
            if len(base_res.trades):
                base_trades.append(base_res.trades.assign(fold=fold.number))
            folds.append({"fold": fold.number, "test_start": str(fold.test_start), "test_end": str(fold.test_end),
                          "train_signals": len(train_ds), "train_hit_rate": round(model.base_rate, 4),
                          "model": model.kind, "model_notes": model.notes,
                          **{k: v for k, v in summarize(res.trades, res.equity, float(cfg.initial_cash)).items()
                             if k in ("trades", "net_return", "expectancy_r", "max_drawdown", "win_rate")},
                          "funnel": dict(res.funnel), "rejections": dict(res.rejections), "halts": res.events})

    trades = pd.concat(oos_trades, ignore_index=True) if oos_trades else pd.DataFrame()
    btrades = pd.concat(base_trades, ignore_index=True) if base_trades else pd.DataFrame()
    summary = _pooled_summary(trades, cfg)
    base_summary = _pooled_summary(btrades, cfg)
    gate = decision_gate(trades, btrades, cfg.min_trades,
                         stable_halves=getattr(strategy, "require_stable_halves", False))
    rtrades = pd.concat(raw_trades, ignore_index=True) if raw_trades else pd.DataFrame()
    if getattr(strategy, "require_beats_unfiltered", False):
        # an AI filter must earn its place: better than the same rules without it (ADR 0009)
        f_mean = float(trades["r_net"].mean()) if len(trades) else None
        u_mean = float(rtrades["r_net"].mean()) if len(rtrades) else None
        gate["unfiltered_mean_r"] = u_mean
        if f_mean is None or u_mean is None or f_mean <= u_mean:
            gate["reasons"].append("the AI filter does not beat the same rules without it")
            gate["pass"] = False
    labelled = ds if len(ds) else None
    diagnostics = {
        "signals": len(signals), "signals_per_day": _per_day(signals),
        "labelled_signals": int(len(ds)),
        "label_hit_rate": float(labelled["y"].mean()) if labelled is not None else None,
        "label_mean_r_net": float(labelled["r_net"].mean()) if labelled is not None and "r_net" in labelled else None,
        "naia_rejections": rejections_total,
        "judged_raw": judged_raw,
    }
    return ResearchResult(strategy.version, folds, trades, summary, base_summary, gate, dataset_hash(ds),
                          _pooled_summary(rtrades, cfg), diagnostics)


def _per_day(signals: list[Signal]) -> float | None:
    if len(signals) < 2:
        return None
    days = (signals[-1].decision_time - signals[0].decision_time).total_seconds() / 86400
    return round(len(signals) / days, 3) if days > 0 else None


def _pooled_summary(trades: pd.DataFrame, cfg: ResearchConfig) -> dict:
    """Trade-level statistics pooled across folds (each fold restarts with fresh cash)."""
    if trades.empty:
        return {"trades": 0}
    eq = pd.Series(dtype=float)
    out = summarize(trades, eq, float(cfg.initial_cash))
    for k in ("final_equity", "net_return", "max_drawdown", "max_drawdown_duration_days", "sharpe", "sortino"):
        out.pop(k, None)
    return out


def decision_gate(trades: pd.DataFrame, baseline: pd.DataFrame, min_trades: int = 100,
                  stable_halves: bool = False) -> dict:
    reasons = []
    n = len(trades)
    mean_r = float(trades["r_net"].mean()) if n else None
    ci = bootstrap_mean_ci(trades["r_net"]) if n else None
    base_r = float(baseline["r_net"].mean()) if len(baseline) else None
    if n < min_trades:
        reasons.append(f"only {n} out-of-sample trades; need at least {min_trades}")
    if ci is None or ci[0] <= 0:
        reasons.append("95% interval of mean net R does not lie above zero")
    if mean_r is None or base_r is None or mean_r <= base_r:
        reasons.append("does not beat the random-entry baseline")
    halves = None
    if stable_halves and n >= 2:
        ordered = trades.sort_values("exit_time")["r_net"].to_numpy()
        halves = (float(ordered[: n // 2].mean()), float(ordered[n // 2:].mean()))
        if min(halves) <= 0:
            reasons.append(f"not positive in both halves of the test period ({halves[0]:+.3f}, {halves[1]:+.3f})")
    return {"pass": not reasons, "reasons": reasons, "trades": n, "mean_r": mean_r, "mean_r_ci95": ci,
            "baseline_mean_r": base_r, "halves_mean_r": halves}


def _tf(frame: pd.DataFrame):
    from nextrade_core.data.candles import Timeframe
    # The most common bar spacing: forex has weekend and holiday gaps, so the first two bars
    # can be days apart (a Sunday-evening bar followed by Monday's).
    if len(frame) > 1:
        step = pd.Series(frame.index[1:] - frame.index[:-1]).mode().iloc[0]
    else:
        step = pd.Timedelta(minutes=1)
    for tf in Timeframe:
        if tf.delta == step:
            return tf
    raise ValueError(f"cannot infer timeframe from bar spacing {step}")


def parameter_sweep(code: str, param: str, values: list, frames, instruments, costs, risk,
                    cfg: ResearchConfig) -> pd.DataFrame:
    """Out-of-sample results for each value of one parameter. A real edge shows up as a
    smooth plateau across neighbouring values, not a single lucky spike."""
    from nextrade_core.strategies.registry import make

    rows = []
    for v in values:
        res = evaluate_strategy(make(code, **{param: v}), frames, instruments, costs, risk, cfg)
        raw = res.raw_summary
        rows.append({param: v, "trades": res.gate["trades"], "mean_r": res.gate["mean_r"],
                     "ci_low": res.gate["mean_r_ci95"][0] if res.gate["mean_r_ci95"] else None,
                     "raw_trades": raw.get("trades", 0), "raw_mean_r": raw.get("expectancy_r"),
                     "gate": res.gate["pass"]})
    return pd.DataFrame(rows)
