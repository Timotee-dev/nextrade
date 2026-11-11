"""Wiring for run_trader: models for NAIA, paper balances, controller state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.db.models import Sum

from apps.market.store import CandleStore
from apps.ops.models import SystemStateTransition, record_transition
from nextrade_core.controller.state_machine import TradingState, TradingStateMachine
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, with_features
from nextrade_core.ml.dataset import build_dataset
from nextrade_core.ml.models import train_calibrated
from nextrade_core.naia.engine import Naia

from .models import LedgerEntry, TradingRun

LATCHED = {TradingState.RISK_SHUTDOWN.value, TradingState.EMERGENCY.value}


def last_state() -> str | None:
    row = SystemStateTransition.objects.order_by("-at", "-id").first()
    return row.to_state if row else None


class _TransitionWriter:
    def record(self, t):
        return record_transition(t)


def make_controller(clock, threaded: bool = False) -> TradingStateMachine:
    """Always starts STOPPED in a new process; a latched halt must be acknowledged first.
    threaded=True routes the database write through a worker thread (for async callers)."""
    if threaded:
        from .store import OffLoop
        return TradingStateMachine(recorder=OffLoop(_TransitionWriter(), "state").record, clock=clock)
    return TradingStateMachine(recorder=record_transition, clock=clock)


def paper_balances(run: TradingRun) -> dict[str, Decimal]:
    """Rebuild the paper venue's balances from the ledger (paper state survives restarts)."""
    balances = {run.account_currency: run.initial_equity}
    for row in LedgerEntry.objects.filter(run=run).values("asset").annotate(total=Sum("amount")):
        balances[row["asset"]] = balances.get(row["asset"], Decimal(0)) + row["total"]
    return balances


def hit_rate_naia(store: CandleStore, strategies: dict, symbols: list[str], tf: Timeframe, costs: CostModel,
                  lookback_days: int = 180, model_lookup=None) -> tuple[Naia, dict[str, str]]:
    """NAIA with, per strategy, the promoted production model if one exists, otherwise the
    strategy's hit rate measured on recent stored history (clearly labelled as such)."""
    naia = Naia(costs)
    sources = {}
    end = datetime.now(UTC)
    frames = {}
    for sym in symbols:
        raw = store.load_frame(sym, tf, end - timedelta(days=lookback_days), end)
        if len(raw) > FeatureParams().warmup_bars + 50:
            frames[sym] = with_features(raw, tf)
    for code, strat in strategies.items():
        if model_lookup is not None:
            found = model_lookup(code)
            if found is not None:
                ensemble, version = found
                naia.set_models(code, ensemble, version)
                sources[code] = f"model:{version}"
                continue
        signals = [s for sym, f in frames.items() for s in strat.generate(f, sym, tf)]
        ds = build_dataset(frames, signals, costs) if signals else None
        if ds is None or len(ds) < 30:
            sources[code] = "none (fewer than 30 resolved historical signals; all signals will be rejected)"
            continue
        model = train_calibrated(ds, ds["y"].to_numpy(), "hitrate", [])
        naia.set_models(code, [model], f"hitrate-{len(ds)}")
        sources[code] = f"hit_rate over {len(ds)} signals: {model.base.rate:.3f}"
    return naia, sources
