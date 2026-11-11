"""Phase 7: walk-forward backtest of the strategies on stored history, with the decision gate.

Example:
    python manage.py run_backtest --timeframe 15m --model hitrate
    python manage.py run_backtest --strategies trend --model lgbm --start 2022-01-01
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.market.models import Instrument
from apps.market.store import CandleStore
from apps.research.models import ExperimentRun
from nextrade_core.backtest.experiments import git_commit, write_record
from nextrade_core.backtest.research import ResearchConfig, evaluate_strategy
from nextrade_core.costs.model import CostModel
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, version_hash, with_features
from nextrade_core.ml.models import MODEL_KINDS
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.strategies.registry import TRADABLE, make


class Command(BaseCommand):
    help = "Walk-forward backtest with costs, baselines and the go/no-go decision gate."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--strategies", default=",".join(TRADABLE))
        parser.add_argument("--timeframe", default="15m", choices=[t.value for t in Timeframe if t is not Timeframe.M1])
        parser.add_argument("--model", default="hitrate", choices=MODEL_KINDS)
        parser.add_argument("--start", help="YYYY-MM-DD (default: earliest stored data)")
        parser.add_argument("--end", help="YYYY-MM-DD (default: now)")
        parser.add_argument("--train-days", type=int, default=365)
        parser.add_argument("--test-days", type=int, default=90)
        parser.add_argument("--capital", default="1000", help="starting cash in USDT for each fold")
        parser.add_argument("--min-trades", type=int, default=100)

    def handle(self, *args, **o):
        tf = Timeframe(o["timeframe"])
        qs = Instrument.objects.filter(venue="binance")
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = {i.symbol: i for i in qs if i.candles.exists()}
        if not instruments:
            raise CommandError("No stored candles. Run load_binance_history first.")
        store = CandleStore(instruments)
        start = datetime.fromisoformat(o["start"]).replace(tzinfo=UTC) if o["start"] else \
            min(i.candles.order_by("open_time").first().open_time for i in instruments.values())
        end = datetime.fromisoformat(o["end"]).replace(tzinfo=UTC) if o["end"] else datetime.now(UTC)

        self.stdout.write(f"Loading {len(instruments)} symbols, {tf.value} bars, {start:%Y-%m-%d} -> {end:%Y-%m-%d}")
        frames = {}
        for sym in instruments:
            raw = store.load_frame(sym, tf, start, end)
            if len(raw) > FeatureParams().warmup_bars + 100:
                frames[sym] = with_features(raw, tf)
        if not frames:
            raise CommandError("Not enough bars for features. Load more history.")
        domain = {s: instruments[s].to_domain() for s in frames}
        cfg = ResearchConfig(train=pd.Timedelta(days=o["train_days"]), test=pd.Timedelta(days=o["test_days"]),
                             model_kind=o["model"], initial_cash=Decimal(o["capital"]), min_trades=o["min_trades"])
        costs, risk = CostModel(), RiskEngine()
        out_dir = Path(settings.BASE_DIR) / "research" / "experiments"

        for code in [c.strip() for c in o["strategies"].split(",") if c.strip()]:
            strategy = make(code)
            self.stdout.write(f"\n== {strategy.version} ({o['model']}) ==")
            res = evaluate_strategy(strategy, frames, domain, costs, risk, cfg)
            s, b, g = res.summary, res.baseline_summary, res.gate
            self.stdout.write(f"  folds: {len(res.folds)}   out-of-sample trades: {s.get('trades', 0)}")
            if s.get("trades"):
                ci = s.get("expectancy_r_ci95")
                self.stdout.write(
                    f"  mean net R: {s['expectancy_r']:+.3f}  95% CI: {ci[0]:+.3f} to {ci[1]:+.3f}" if ci else
                    f"  mean net R: {s['expectancy_r']:+.3f}")
                self.stdout.write(f"  win rate: {s['win_rate']:.1%}   profit factor: {s['profit_factor']}   "
                                  f"fees/gross profit: {s.get('cost_share_of_gross')}")
            if b.get("trades"):
                self.stdout.write(f"  random-entry baseline: {b['trades']} trades, mean net R {b['expectancy_r']:+.3f}")
            verdict = self.style.SUCCESS("PASS") if g["pass"] else self.style.ERROR("FAIL")
            self.stdout.write(f"  decision gate: {verdict}")
            for reason in g["reasons"]:
                self.stdout.write(f"    - {reason}")
            payload = {"strategy": res.strategy, "model": o["model"], "timeframe": tf.value,
                       "symbols": sorted(frames), "period": [str(start), str(end)],
                       "feature_set": version_hash(FeatureParams()), "dataset_hash": res.dataset_hash,
                       "config": {k: str(v) for k, v in asdict(cfg).items()}, "summary": s,
                       "baseline": b, "gate": g, "folds": res.folds}
            path = write_record(out_dir, code, payload)
            ExperimentRun.objects.create(
                strategy_version=res.strategy, model_kind=o["model"], timeframe=tf.value, symbols=sorted(frames),
                period_start=start, period_end=end, dataset_hash=res.dataset_hash,
                git_commit=git_commit(Path(settings.BASE_DIR)) or "",
                config=payload["config"], summary=json.loads(json.dumps(s, default=str)),
                baseline_summary=json.loads(json.dumps(b, default=str)), gate_pass=g["pass"],
                gate=json.loads(json.dumps(g, default=str)), folds=json.loads(json.dumps(res.folds, default=str)))
            self.stdout.write(f"  record: {path.relative_to(settings.BASE_DIR)}")
