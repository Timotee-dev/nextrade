"""Validate the six scalping strategies on many pairs and timeframes, with costs, and report honestly.

    python manage.py validate_scalping --venue bybit --days 30
    python manage.py validate_scalping --venue bybit --timeframes 5m --strategies scalp_momentum

For each timeframe (1m, 3m, 5m) and strategy, with FIXED pre-registered parameters:
  1. walk-forward on the development window (older data), after fees + spread + slippage;
  2. only if it passes the decision gate, one run on the untouched HOLD-OUT window (latest days).
Reports signals/day, executed trades/day, net profit, win rate, profit factor, max drawdown,
simultaneous positions and the decision gate. Writes JSON and markdown under research/validation/.
Caveats printed on every report: 18 tests = multiple-testing risk; the pair list is ranked by
today's turnover (survivorship); a short history is a weak sample.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.market.models import Instrument
from apps.market.store import CandleStore
from apps.research.models import ExperimentRun
from nextrade_core.backtest.experiments import git_commit
from nextrade_core.backtest.research import ResearchConfig, evaluate_strategy
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, with_features
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits, scaled_limits
from nextrade_core.scalping.validation import covered_days, measure, sustains_frequency
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, make

now_utc = lambda: datetime.now(UTC)  # noqa: E731


def _sub(frames: dict[str, pd.DataFrame], end: pd.Timestamp | None) -> dict[str, pd.DataFrame]:
    return {s: (f[f.index < end] if end is not None else f) for s, f in frames.items()}


def evaluate(code, frames, domain, costs, risk, cfg):
    """One strategy run -> (ResearchResult, measured dict)."""
    res = evaluate_strategy(make(code), frames, domain, costs, risk, cfg)
    days = covered_days(res.folds, cfg.holdout_from)
    return res, measure(res.trades, days, float(cfg.initial_cash))


def _fmt(m: dict) -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"{m['trades']:>5} trades  {m['trades_per_day']:>6.1f}/day  net {m['net_pnl']:>+9.2f} ({m['net_return']:+.1%})  "
            f"win {m['win_rate']:.1%}  PF {pf}  maxDD {m['max_drawdown']:.1%}  open peak {m['peak_open']} "
            f"mean {m['mean_open']:.1f}  symbols {m['symbols_traded']}")


class Command(BaseCommand):
    help = "Validate the scalping strategies across pairs and 1m/3m/5m with realistic costs."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default="bybit", choices=["bybit", "gate", "binance"])
        parser.add_argument("--costs", default=None, choices=["bybit", "gate", "binance"], help="default: the venue")
        parser.add_argument("--timeframes", default="1m,3m,5m")
        parser.add_argument("--strategies", default=",".join(SCALP_PREREGISTERED))
        parser.add_argument("--symbols", default=None, help="default: every stored symbol of the venue")
        parser.add_argument("--exclude", default="USDYUSDT,XAUTUSDT,BBSOLUSDT,PAXGUSDT",
                            help="pegged/derivative tokens that do not move like tradable crypto (they only add costs)")
        parser.add_argument("--days", type=float, default=None, help="use only the latest N days of stored data")
        parser.add_argument("--holdout-days", type=float, default=10)
        parser.add_argument("--train-days", type=int, default=7)
        parser.add_argument("--test-days", type=int, default=3)
        parser.add_argument("--min-trades", type=int, default=100)
        parser.add_argument("--min-trades-per-day", type=float, default=20)
        parser.add_argument("--capital", default="1000", help="research cash (results are also shown in R)")
        parser.add_argument("--risk-per-trade", default=None)
        parser.add_argument("--no-record", action="store_true", help="do not write ExperimentRun rows")

    def handle(self, *args, **o):
        venue, cost_venue = o["venue"], o["costs"] or o["venue"]
        qs = Instrument.objects.filter(venue=venue)
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        skip = {x.strip().upper() for x in o["exclude"].split(",") if x.strip()}
        instruments = {i.symbol: i for i in qs if i.candles.exists() and i.symbol not in skip}
        if not instruments:
            raise CommandError(f"No stored {venue} candles. Run load_{venue}_history first.")
        store = CandleStore(instruments)
        tfs = [Timeframe(t.strip()) for t in o["timeframes"].split(",") if t.strip()]
        codes = [c.strip() for c in o["strategies"].split(",") if c.strip()]
        first = min(i.candles.order_by("open_time").first().open_time for i in instruments.values())
        end = now_utc()
        start = max(first, end - timedelta(days=o["days"])) if o["days"] else first
        cutoff = pd.Timestamp(end - timedelta(days=o["holdout_days"]))
        if (cutoff.to_pydatetime() - start).days < o["train_days"] + o["test_days"] + 1:
            raise CommandError(f"Only {(end - start).days} days of data: need more than {o['train_days'] + o['test_days'] + 1}"
                               f" development days plus {o['holdout_days']:g} hold-out days. Load more history.")
        limits = scaled_limits(Decimal(o["risk_per_trade"]) if o["risk_per_trade"] else RiskLimits().risk_per_trade,
                               cost_venue)
        risk = RiskEngine(limits)
        cfg = ResearchConfig(train=pd.Timedelta(days=o["train_days"]), test=pd.Timedelta(days=o["test_days"]),
                             initial_cash=Decimal(o["capital"]), min_trades=o["min_trades"])
        hold_cfg = ResearchConfig(train=cfg.train, test=cfg.test, initial_cash=cfg.initial_cash,
                                  min_trades=max(30, o["min_trades"] // 3), holdout_from=cutoff)
        self.stdout.write(f"{len(instruments)} symbols on {venue}; {start:%Y-%m-%d} -> {end:%Y-%m-%d}; "
                          f"hold-out = last {o['holdout_days']:g} days (from {cutoff:%Y-%m-%d %H:%M}), "
                          f"opened only for strategies that pass development.")
        results: list[dict] = []
        for tf in tfs:
            frames = {}
            for sym in instruments:
                raw = store.load_frame(sym, tf, start, end)
                if len(raw) > FeatureParams().warmup_bars + 100:
                    frames[sym] = with_features(raw, tf)
            if not frames:
                self.stdout.write(self.style.WARNING(f"{tf.value}: not enough bars for any symbol, skipped"))
                continue
            domain = {s: instruments[s].to_domain() for s in frames}
            costs = cost_model_for(cost_venue)
            self.stdout.write(f"\n=== {tf.value}: {len(frames)} symbols, fee {costs.fee_rate:.2%}/side, spread "
                              f"{costs.spread_bps:g} bps, slippage {costs.slippage_bps:g} bps/side ===")
            dev_frames = _sub(frames, cutoff)
            for code in codes:
                strat = make(code)
                if getattr(strat, "needs_shorting", False):
                    note = "spot cannot short: signals are generated but cannot be traded; skipped"
                    self.stdout.write(f"  {code}: {note}")
                    results.append({"strategy": code, "timeframe": tf.value, "skipped": note})
                    continue
                dev_res, dev = evaluate(code, dev_frames, domain, costs, risk, cfg)
                row = {"strategy": code, "timeframe": tf.value, "signals_per_day": dev_res.diagnostics.get("signals_per_day"),
                       "dev": dev, "dev_gate": dev_res.gate, "baseline_trades": dev_res.baseline_summary.get("trades", 0),
                       "baseline_mean_r": dev_res.baseline_summary.get("expectancy_r"),
                       "funnel": _sum_dicts(f.get("funnel", {}) for f in dev_res.folds),
                       "rejections": _sum_dicts(f.get("rejections", {}) for f in dev_res.folds),
                       "hold": None, "hold_gate": None}
                self.stdout.write(f"  {code}: signals {row['signals_per_day']}/day")
                self.stdout.write(f"    development: {_fmt(dev)}")
                self.stdout.write(f"    gate: {'PASS' if dev_res.gate['pass'] else 'FAIL'}"
                                  + ("" if dev_res.gate["pass"] else "  - " + "; ".join(dev_res.gate["reasons"])))
                hold_res = None
                if dev_res.gate["pass"]:
                    hold_res, hold = evaluate(code, frames, domain, costs, risk, hold_cfg)
                    row["hold"], row["hold_gate"] = hold, hold_res.gate
                    self.stdout.write(f"    HOLD-OUT:    {_fmt(hold)}")
                    self.stdout.write(f"    hold-out gate: {'PASS' if hold_res.gate['pass'] else 'FAIL'}"
                                      + ("" if hold_res.gate["pass"] else "  - " + "; ".join(hold_res.gate["reasons"])))
                ok, why = sustains_frequency(dev_res.gate["pass"], dev, row["hold"],
                                             hold_res.gate["pass"] if hold_res else None, o["min_trades_per_day"])
                row["sustains_frequency"], row["why_not"] = ok, why
                self.stdout.write("    => " + ("SUSTAINS frequency after costs" if ok else "does NOT qualify: " + "; ".join(why)))
                results.append(row)
                if not o["no_record"]:
                    final = hold_res or dev_res
                    ExperimentRun.objects.create(
                        kind="scalp_validation", strategy_version=final.strategy, model_kind="rule", timeframe=tf.value,
                        symbols=sorted(frames), period_start=start, period_end=end, dataset_hash=final.dataset_hash,
                        git_commit=git_commit(Path(settings.BASE_DIR)) or "",
                        config={"venue": venue, "costs": cost_venue, "holdout_from": str(cutoff), "train": o["train_days"],
                                "test": o["test_days"], "sustains": ok},
                        summary=json.loads(json.dumps(row["hold"] or dev, default=str)),
                        baseline_summary=json.loads(json.dumps(dev_res.baseline_summary, default=str)),
                        gate_pass=bool(ok), gate=json.loads(json.dumps({"dev": dev_res.gate, "hold": row["hold_gate"],
                                                                        "reasons": why}, default=str)),
                        folds=json.loads(json.dumps(dev_res.folds, default=str)))
            del frames, dev_frames
        path = self._write(results, o, venue, cost_venue, len(instruments), start, end, cutoff)
        winners = [r for r in results if r.get("sustains_frequency")]
        self.stdout.write("\n" + (self.style.SUCCESS(f"{len(winners)} strategy/timeframe combinations passed everything: "
                                                    + ", ".join(f"{r['strategy']}@{r['timeframe']}" for r in winners))
                                  if winners else self.style.WARNING(
                                      "No strategy passed development + hold-out with enough trades per day after costs. "
                                      "That is a result, not a failure of the test.")))
        self.stdout.write(f"Report: {path}")

    def _write(self, results, o, venue, cost_venue, n_sym, start, end, cutoff) -> Path:
        out = Path(settings.BASE_DIR) / "research" / "validation"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (out / f"scalping-{stamp}.json").write_text(json.dumps(results, indent=2, default=str))
        n_tests = sum(1 for r in results if "skipped" not in r)
        lines = [f"# Scalping validation {stamp} UTC", "",
                 f"Data: {venue}, {n_sym} pairs, {start:%Y-%m-%d} to {end:%Y-%m-%d}. Costs: {cost_venue} model (fee + spread + slippage "
                 f"per side). Hold-out: from {cutoff:%Y-%m-%d %H:%M}, opened only for development passers.", "",
                 "| strategy | tf | signals/day | trades/day | net | win rate | PF | max DD | peak open | dev gate | hold-out | verdict |",
                 "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in results:
            if "skipped" in r:
                lines.append(f"| {r['strategy']} | {r['timeframe']} | - | - | - | - | - | - | - | - | - | skipped |")
                continue
            d, h = r["dev"], r["hold"]
            hold = f"{h['net_return']:+.1%} / {h['trades_per_day']:.0f}/day" if h else "not run"
            lines.append(f"| {r['strategy']} | {r['timeframe']} | {r['signals_per_day']} | {d['trades_per_day']} | "
                         f"{d['net_return']:+.1%} | {d['win_rate']:.1%} | {d['profit_factor']:.2f} | {d['max_drawdown']:.1%} | "
                         f"{d['peak_open']} | {'pass' if r['dev_gate']['pass'] else 'fail'} | {hold} | "
                         f"{'SUSTAINS' if r['sustains_frequency'] else 'no'} |")
        lines += ["", "## Caveats", f"- {n_tests} strategy/timeframe tests were run: some passes can be luck. A pass must "
                  "repeat on fresh data (keep the paper run going) before it is trusted.",
                  "- The pair list is the top by TODAY'S turnover: survivorship/selection bias flatters results.",
                  "- Fills are simulated with assumed spreads/slippage until the recorder has measured real ones.",
                  "- Executed trades/day are lower than signals/day because risk limits and minimum sizes reject some.",
                  "- A short history is a weak sample; more days make the verdict more reliable.", ""]
        path = out / f"scalping-{stamp}.md"
        path.write_text("\n".join(lines))
        return path


def _sum_dicts(items) -> dict:
    out: dict = {}
    for d in items:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out
