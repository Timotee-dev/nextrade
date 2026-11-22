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

from adapters.bybit.client import is_pegged
from apps.market.coverage import check_pairs
from apps.market.management.commands.check_data import write_report as write_quality
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
from nextrade_core.data.coverage import Thresholds
from nextrade_core.scalping.reentry import ReentryConfig
from nextrade_core.scalping.validation import (add_costs, breakdown, by_pair, covered_days, sustains_frequency)
from nextrade_core.strategies.registry import SCALP_PREREGISTERED, make

now_utc = lambda: datetime.now(UTC)  # noqa: E731


def _sub(frames: dict[str, pd.DataFrame], end: pd.Timestamp | None) -> dict[str, pd.DataFrame]:
    return {s: (f[f.index < end] if end is not None else f) for s, f in frames.items()}


def evaluate(code, frames, domain, costs, risk, cfg):
    """One strategy run -> (ResearchResult, measured dict, per-pair rows)."""
    res = evaluate_strategy(make(code), frames, domain, costs, risk, cfg)
    days = covered_days(res.folds, cfg.holdout_from)
    costed = add_costs(res.trades, costs)
    return res, breakdown(costed, days, float(cfg.initial_cash)), by_pair(costed, days, float(cfg.initial_cash))


def _fmt(m: dict) -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"{m['trades']:>5} trades  {m['trades_per_day']:>6.1f}/day  gross {m['gross_pnl']:>+9.2f}  fees {m['fees']:.2f}  "
            f"spread+slip~{m['friction']:.2f}  NET {m['net_pnl']:>+9.2f} ({m['net_return']:+.1%})  win {m['win_rate']:.1%}  "
            f"PF {pf}  maxDD {m['max_drawdown']:.1%}  avg hold {m['avg_hold_min']:.0f}m  open peak {m['peak_open']} "
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
        instruments = {i.symbol: i for i in qs if i.candles.exists() and i.symbol not in skip and not is_pegged(i.base)}
        if not instruments:
            raise CommandError(f"No stored {venue} candles. Run load_{venue}_history first.")
        th = Thresholds(min_days=o["train_days"] + o["test_days"] + o["holdout_days"] + 1, max_age_hours=12)
        quality = check_pairs(list(instruments.values()), now_utc(), window_days=o["days"], th=th)
        qpath = write_quality(quality, th, venue, now_utc())
        usable = {r.symbol: r for r in quality if r.usable}
        dropped = [r for r in quality if not r.usable]
        self.stdout.write(f"Data quality: {len(usable)} of {len(quality)} pairs usable (report {qpath.name}).")
        for r in dropped:
            self.stdout.write(self.style.WARNING(f"  EXCLUDED {r.symbol}: " + "; ".join(r.reasons)))
        if not usable:
            raise CommandError("No pair has enough clean data. See the data-quality report; load more history.")
        ok_tf = {tf: {s for s, r in usable.items() if r.ok_by_tf.get(tf)} for tf in ("1m", "3m", "5m")}
        instruments = {s: i for s, i in instruments.items() if s in usable}
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
        rcfg = ReentryConfig()
        cfg = ResearchConfig(train=pd.Timedelta(days=o["train_days"]), test=pd.Timedelta(days=o["test_days"]),
                             initial_cash=Decimal(o["capital"]), min_trades=o["min_trades"], reentry=rcfg)
        hold_cfg = ResearchConfig(train=cfg.train, test=cfg.test, initial_cash=cfg.initial_cash,
                                  min_trades=max(30, o["min_trades"] // 3), holdout_from=cutoff, reentry=rcfg)
        self.stdout.write(f"{len(instruments)} symbols on {venue}; {start:%Y-%m-%d} -> {end:%Y-%m-%d}; "
                          f"hold-out = last {o['holdout_days']:g} days (from {cutoff:%Y-%m-%d %H:%M}), "
                          f"opened only for strategies that pass development.")
        results: list[dict] = []
        for tf in tfs:
            frames = {}
            for sym in instruments:
                if sym not in ok_tf.get(tf.value, set(instruments)):
                    continue
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
                dev_res, dev, dev_pairs = evaluate(code, dev_frames, domain, costs, risk, cfg)
                row = {"strategy": code, "timeframe": tf.value, "signals_per_day": dev_res.diagnostics.get("signals_per_day"),
                       "dev": dev, "dev_gate": dev_res.gate, "baseline_trades": dev_res.baseline_summary.get("trades", 0),
                       "baseline_mean_r": dev_res.baseline_summary.get("expectancy_r"),
                       "funnel": _sum_dicts(f.get("funnel", {}) for f in dev_res.folds),
                       "rejections": _sum_dicts(f.get("rejections", {}) for f in dev_res.folds),
                       "dev_pairs": dev_pairs, "symbols_tested": len(frames), "hold": None, "hold_gate": None,
                       "hold_pairs": None}
                top_rej = sorted(row["rejections"].items(), key=lambda kv: -kv[1])[:3]
                self.stdout.write(f"  {code}: signals {row['signals_per_day']}/day"
                                  + (f"   skipped: {', '.join(f'{k} x{v}' for k, v in top_rej)}" if top_rej else ""))
                self.stdout.write(f"    development: {_fmt(dev)}")
                self.stdout.write(f"    gate: {'PASS' if dev_res.gate['pass'] else 'FAIL'}"
                                  + ("" if dev_res.gate["pass"] else "  - " + "; ".join(dev_res.gate["reasons"])))
                hold_res = None
                if dev_res.gate["pass"]:
                    hold_res, hold, hold_pairs = evaluate(code, frames, domain, costs, risk, hold_cfg)
                    row["hold"], row["hold_gate"], row["hold_pairs"] = hold, hold_res.gate, hold_pairs
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
        rows = []
        for r in results:
            for period, key in (("development", "dev_pairs"), ("hold-out", "hold_pairs")):
                for pr in r.get(key) or []:
                    rows.append({"strategy": r["strategy"], "timeframe": r["timeframe"], "period": period, **pr})
        pd.DataFrame(rows).to_csv(out / f"scalping-{stamp}-by-pair.csv", index=False)
        n_tests = sum(1 for r in results if "skipped" not in r)
        lines = [f"# Scalping validation {stamp} UTC", "",
                 f"Data: {venue}, {n_sym} usable pairs, {start:%Y-%m-%d} to {end:%Y-%m-%d}. Costs: {cost_venue} model (fee + spread + "
                 f"slippage per side). Walk-forward on development data; hold-out from {cutoff:%Y-%m-%d %H:%M}, opened only for "
                 "development passers. Parameters fixed in advance, never tuned on the hold-out. Re-entry guard on.", "",
                 "Gross = before fees and spread/slippage. Fees are exact; spread+slippage is estimated from the cost model.", "",
                 "| strategy | tf | signals/day | trades/day | gross | fees | spread+slip | NET | win | PF | max DD | avg hold | peak open | dev gate | hold-out net / trades/day | verdict |",
                 "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in results:
            if "skipped" in r:
                lines.append(f"| {r['strategy']} | {r['timeframe']} | - | - | - | - | - | - | - | - | - | - | - | - | - | skipped |")
                continue
            d, h = r["dev"], r["hold"]
            hold = f"{h['net_return']:+.1%} / {h['trades_per_day']:.0f}" if h else "not run"
            lines.append(f"| {r['strategy']} | {r['timeframe']} | {r['signals_per_day']} | {d['trades_per_day']} | {d['gross_pnl']:+.1f} | "
                         f"{d['fees']:.1f} | {d['friction']:.1f} | {d['net_return']:+.1%} | {d['win_rate']:.1%} | {d['profit_factor']:.2f} | "
                         f"{d['max_drawdown']:.1%} | {d['avg_hold_min']:.0f}m | {d['peak_open']} | "
                         f"{'pass' if r['dev_gate']['pass'] else 'fail'} | {hold} | {'SUSTAINS' if r['sustains_frequency'] else 'no'} |")
        lines += ["", "## Best and worst pairs (development period)"]
        for r in results:
            pairs = [p for p in (r.get("dev_pairs") or []) if p["trades"] >= 5]
            if len(pairs) < 2:
                continue
            fmt = lambda p: f"{p['symbol']} {p['net_pnl']:+.1f} ({p['trades']} trades, win {p['win_rate']:.0%})"  # noqa: E731
            lines.append(f"- {r['strategy']} @ {r['timeframe']}: best " + ", ".join(fmt(p) for p in pairs[:3])
                         + "; worst " + ", ".join(fmt(p) for p in pairs[-3:]))
        lines += ["", "## Caveats", f"- {n_tests} strategy/timeframe tests were run: some passes can be luck. A pass must "
                  "repeat on fresh data (keep the paper run going) before it is trusted.",
                  "- The pair list is the top by TODAY'S turnover: survivorship/selection bias flatters results.",
                  "- Fills are simulated with assumed spreads/slippage until the recorder has measured real ones.",
                  "- Executed trades/day are lower than signals/day because risk limits, the re-entry guard and minimum sizes reject some.",
                  "- Per-pair numbers are small samples: do not pick the best pair afterwards and call it an edge.",
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
