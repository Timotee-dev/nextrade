"""Phase 7: walk-forward backtest of the strategies on stored history, with the decision gate.

Example:
    python manage.py run_backtest --timeframe 15m --model hitrate
    python manage.py run_backtest --strategies trend --model lgbm --start 2022-01-01
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
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
from nextrade_core.backtest.research import ResearchConfig, evaluate_strategy, parameter_sweep
from apps.market.services import measured_spreads_bps
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe
from nextrade_core.features.feature_set import FeatureParams, version_hash, with_features
from nextrade_core.ml.models import MODEL_KINDS
from nextrade_core.nara.engine import RiskEngine
from nextrade_core.nara.limits import RiskLimits, scaled_limits
from nextrade_core.strategies.registry import TRADABLE, make


class Command(BaseCommand):
    help = "Walk-forward backtest with costs, baselines and the go/no-go decision gate."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default=settings.NEXTRADE["VENUE"], choices=["quidax", "binance", "gate", "mt5"],
                            help="where the price history comes from")
        parser.add_argument("--costs", default=None, choices=["quidax", "binance", "mt5"],
                            help="whose fees and spreads to charge (default: quidax for gate data, else the venue)")
        parser.add_argument("--ngn-per-usdt", default=None,
                            help="rate for Quidax-cost research on USDT data (default: latest stored USDT/NGN, else 1400)")
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--strategies", default=",".join(TRADABLE))
        parser.add_argument("--timeframe", default="15m", choices=[t.value for t in Timeframe if t is not Timeframe.M1])
        parser.add_argument("--model", default="hitrate", choices=MODEL_KINDS)
        parser.add_argument("--start", help="YYYY-MM-DD (default: earliest stored data)")
        parser.add_argument("--end", help="YYYY-MM-DD (default: now)")
        parser.add_argument("--train-days", type=int, default=365)
        parser.add_argument("--test-days", type=int, default=90)
        parser.add_argument("--capital", default=None,
                            help="starting cash per fold in the quote currency (default: 20000 NGN on quidax, 1000 USDT on binance)")
        parser.add_argument("--min-trades", type=int, default=100)
        parser.add_argument("--holdout-from", default=None,
                            help="YYYY-MM-DD: score only trades from this date (the unseen confirmation period)")
        parser.add_argument("--leverage", default="200",
                            help="margin accounts (mt5 forex): account leverage, e.g. 200 for 1:200")
        parser.add_argument("--expanding", action="store_true",
                            help="each test window trains on ALL earlier data (use for rare daily signals)")
        parser.add_argument("--risk-per-trade", default=None,
                            help="fraction of equity risked per trade, e.g. 0.01 = 1%% (default 0.0025). "
                                 "Total-open, cluster and stress limits scale with it; loss limits do not.")
        parser.add_argument("--sweep", default=None,
                            help="one parameter across values for ONE strategy, e.g. reward_risk=2,3,4 "
                                 "(look for a plateau, not the single best value)")

    def handle(self, *args, **o):
        tf = Timeframe(o["timeframe"])
        venue = o["venue"]
        cost_venue = o["costs"] or ("quidax" if venue == "gate" else venue)
        proxy = venue != cost_venue == "quidax"      # e.g. Gate.io prices, Quidax costs
        capital = o["capital"] or {"quidax": "20000", "mt5": "10000"}.get(venue, "1000")
        qs = Instrument.objects.filter(venue=venue)
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = {i.symbol: i for i in qs if i.candles.exists()}
        if not instruments:
            raise CommandError(f"No stored {venue} candles. Run load_{venue}_history first.")
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
        quotes_by_ccy = {i.quote for i in instruments.values() if i.symbol in frames}
        if venue == "mt5":
            quotes_by_ccy = {"USD"}            # forex profits are converted to USD inside the backtester
        if len(quotes_by_ccy) > 1 and not o["symbols"]:
            home = "NGN" if venue == "quidax" else "USDT"
            frames = {s: f for s, f in frames.items() if instruments[s].quote == home}
            self.stdout.write(f"Using {home}-quoted symbols only: {', '.join(sorted(frames))}")
            quotes_by_ccy = {home}
            domain = {s: instruments[s].to_domain() for s in frames}
        if len(quotes_by_ccy) > 1:
            raise CommandError(f"Mixed quote currencies {sorted(quotes_by_ccy)}: pass --symbols with one quote "
                               "currency (e.g. ETHNGN,USDTNGN,BTCNGN,SOLNGN).")
        spreads = measured_spreads_bps({s: instruments[s] for s in frames})
        if proxy:
            # Simulate trading on Quidax: NGN 20,000 and the NGN 1,250 minimum, converted to USDT;
            # spreads measured on the matching Quidax NGN pair (e.g. ETHUSDT data -> ETHNGN spread).
            rate = _ngn_per_usdt(o["ngn_per_usdt"])
            capital = o["capital"] or str((Decimal(20000) / rate).quantize(Decimal("0.01")))
            min_usdt = (Decimal(1250) / rate).quantize(Decimal("0.01"))
            domain = {s: replace(d, min_notional=min_usdt) for s, d in domain.items()}
            qx = {i.base: i for i in Instrument.objects.filter(venue="quidax", quote="NGN")}
            qx_spreads = measured_spreads_bps({b: i for b, i in qx.items()})
            spreads = {s: qx_spreads[instruments[s].base] for s in frames if instruments[s].base in qx_spreads}
            self.stdout.write(f"{venue} prices, Quidax costs: capital {capital} USDT (NGN 20,000 at NGN {rate}/USDT), "
                              f"minimum order {min_usdt} USDT")
        cfg = ResearchConfig(train=pd.Timedelta(days=o["train_days"]), test=pd.Timedelta(days=o["test_days"]),
                             model_kind=o["model"], initial_cash=Decimal(capital), min_trades=o["min_trades"],
                             expanding=o["expanding"], leverage=Decimal(o["leverage"]),
                             holdout_from=pd.Timestamp(o["holdout_from"], tz="UTC") if o["holdout_from"] else None)
        if o["holdout_from"]:
            self.stdout.write(f"HOLD-OUT CONFIRMATION: only trades decided from {o['holdout_from']} count")
        limits = scaled_limits(Decimal(o["risk_per_trade"]) if o["risk_per_trade"] else RiskLimits().risk_per_trade,
                               cost_venue)
        costs, risk = cost_model_for(cost_venue, spreads), RiskEngine(limits)
        self.stdout.write(f"Risk: {limits.risk_per_trade:.2%} per trade, open risk {limits.max_open_risk:.1%}, "
                          f"cluster {limits.max_cluster_risk:.1%}, stress {limits.max_stress_loss:.0%}, "
                          f"daily loss {limits.daily_loss:.0%}, drawdown stop {limits.max_drawdown:.0%}")
        charged = {k: round(v, 2) for k, v in costs.spread_bps_by_symbol.items()}
        self.stdout.write(f"Costs: fee {costs.fee_rate:.2%}/side, spread {costs.spread_bps:.0f} bps default, "
                          f"slippage {costs.slippage_bps} bps/side"
                          + (f", charged spreads {charged}" if charged else " (no recorded spreads yet)")
                          + (" (measured x2)" if charged and charged != spreads else ""))
        if venue == "mt5":
            self.stdout.write(f"Forex margin account: {capital} USD at 1:{o['leverage']}; longs and shorts; "
                              "results in R (multiples of the amount risked), so the edge does not depend on "
                              "account size. Measured bar spreads are doubled.")
        out_dir = Path(settings.BASE_DIR) / "research" / "experiments"

        if o["sweep"]:
            codes = [c.strip() for c in o["strategies"].split(",") if c.strip()]
            if len(codes) != 1 or "=" not in o["sweep"]:
                raise CommandError("--sweep needs exactly one --strategies value and the form param=v1,v2,...")
            param, raw_values = o["sweep"].split("=", 1)
            values = [int(v) if v.strip().lstrip("-").isdigit() else float(v) for v in raw_values.split(",")]
            try:
                make(codes[0], **{param.strip(): values[0]})
            except TypeError as exc:
                raise CommandError(f"{codes[0]} has no parameter {param!r}") from exc
            table = parameter_sweep(codes[0], param.strip(), values, frames, domain, costs, risk, cfg)
            self.stdout.write(f"\nSweep of {codes[0]}.{param.strip()} (out of sample, after costs):")
            self.stdout.write(table.to_string(index=False))
            write_record(out_dir, f"{codes[0]}_sweep_{param.strip()}",
                         {"strategy": codes[0], "param": param.strip(), "timeframe": tf.value,
                          "symbols": sorted(frames), "rows": json.loads(table.to_json(orient="records"))})
            return

        for code in [c.strip() for c in o["strategies"].split(",") if c.strip()]:
            strategy = make(code)
            self.stdout.write(f"\n== {strategy.version} ({o['model']}) ==")
            res = evaluate_strategy(strategy, frames, domain, costs, risk, cfg)
            s, b, g = res.summary, res.baseline_summary, res.gate
            d, raw = res.diagnostics, res.raw_summary
            self.stdout.write(f"  signals: {d.get('signals', 0)} ({d.get('signals_per_day') or 0} per day)"
                              + (f"   hit rate after costs: {d['label_hit_rate']:.1%}, mean {d['label_mean_r_net']:+.3f} R"
                                 if d.get("label_hit_rate") is not None and d.get("label_mean_r_net") is not None else ""))
            if d.get("judged_raw"):
                self.stdout.write("  judged on raw trades (rule-based exits; NAIA filter not applied, ADR 0006)")
            elif raw.get("trades"):
                self.stdout.write(f"  without the NAIA filter: {raw['trades']} trades, mean net R {raw['expectancy_r']:+.3f}, "
                                  f"win rate {raw['win_rate']:.1%}")
            if d.get("naia_rejections"):
                top = sorted(d["naia_rejections"].items(), key=lambda kv: -kv[1])[:4]
                self.stdout.write("  NAIA rejected: " + ", ".join(f"{k.split(':', 1)[-1]} x{v}" for k, v in top))
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
            if g.get("halves_mean_r"):
                h1, h2 = g["halves_mean_r"]
                self.stdout.write(f"  first half of test period: {h1:+.3f} R   second half: {h2:+.3f} R")

            verdict = self.style.SUCCESS("PASS") if g["pass"] else self.style.ERROR("FAIL")
            self.stdout.write(f"  decision gate: {verdict}")
            for reason in g["reasons"]:
                self.stdout.write(f"    - {reason}")
            payload = {"strategy": res.strategy, "model": o["model"], "timeframe": tf.value,
                       "symbols": sorted(frames), "period": [str(start), str(end)],
                       "feature_set": version_hash(FeatureParams()), "dataset_hash": res.dataset_hash,
                       "config": {k: str(v) for k, v in asdict(cfg).items()}, "summary": s,
                       "baseline": b, "gate": g, "folds": res.folds, "raw": raw, "diagnostics": d}
            path = write_record(out_dir, code, payload)
            ExperimentRun.objects.create(
                strategy_version=res.strategy, model_kind=o["model"], timeframe=tf.value, symbols=sorted(frames),
                period_start=start, period_end=end, dataset_hash=res.dataset_hash,
                git_commit=git_commit(Path(settings.BASE_DIR)) or "",
                config=payload["config"], summary=json.loads(json.dumps(s, default=str)),
                baseline_summary=json.loads(json.dumps(b, default=str)), gate_pass=g["pass"],
                gate=json.loads(json.dumps(g, default=str)), folds=json.loads(json.dumps(res.folds, default=str)))
            self.stdout.write(f"  record: {path.relative_to(settings.BASE_DIR)}")


def _ngn_per_usdt(given: str | None) -> Decimal:
    if given:
        return Decimal(given)
    last = (Instrument.objects.filter(venue="quidax", symbol="USDTNGN").first())
    if last:
        row = last.candles.order_by("-open_time").first()
        if row:
            return Decimal(row.close).quantize(Decimal("0.01"))
    return Decimal(1400)
