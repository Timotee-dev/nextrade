"""Model governance from the command line (spec 13). Nothing is ever promoted automatically.

    python manage.py models train --strategy trend --kind lgbm
    python manage.py models list
    python manage.py models shadow --model-version <v>       # evaluate on signals logged in paper trading
    python manage.py models promote --model-version <v> --reason "..." --risk-approved
    python manage.py models rollback --strategy trend --reason "..."
    python manage.py models drift --strategy trend
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from apps.market.models import Instrument
from apps.research import governance as gov
from apps.research.models import ModelVersion
from nextrade_core.data.candles import Timeframe


class Command(BaseCommand):
    help = "Train, evaluate, promote and roll back NAIA models."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["train", "list", "shadow", "promote", "rollback", "drift"])
        parser.add_argument("--strategy")
        parser.add_argument("--kind", default="lgbm", choices=["hitrate", "logreg", "lgbm"])
        parser.add_argument("--model-version", dest="model_version")
        parser.add_argument("--reason", default="")
        parser.add_argument("--risk-approved", action="store_true")
        parser.add_argument("--timeframe", default="15m")
        parser.add_argument("--start")
        parser.add_argument("--train-days", type=int, default=365)
        parser.add_argument("--test-days", type=int, default=90)

    def _instruments(self):
        inst = {i.symbol: i for i in Instrument.objects.filter(venue="binance") if i.candles.exists()}
        if not inst:
            raise CommandError("No stored candles.")
        return inst

    def handle(self, *args, action, **o):
        tf = Timeframe(o["timeframe"])
        if action == "train":
            if not o["strategy"]:
                raise CommandError("--strategy is required")
            inst = self._instruments()
            start = datetime.fromisoformat(o["start"]).replace(tzinfo=UTC) if o["start"] else datetime(2020, 1, 1, tzinfo=UTC)
            mv = gov.train_candidate(o["strategy"], o["kind"], inst, tf, start, datetime.now(UTC),
                                     train_days=o["train_days"], test_days=o["test_days"])
            m, b = mv.metrics["model"], mv.metrics["hitrate_baseline"]
            self.stdout.write(f"Candidate {mv.version}: {mv.n_samples} samples")
            self.stdout.write(f"  OOS Brier {m.get('brier'):.4f} (hit-rate baseline {b.get('brier'):.4f}), "
                              f"skill {m.get('brier_skill'):+.4f}, AUC {m.get('auc')}")
            self.stdout.write(f"  backtest gate: {'PASS' if mv.backtest_gate.get('pass') else 'FAIL'} "
                              f"{mv.backtest_gate.get('reasons', [])}")
        elif action == "list":
            for mv in ModelVersion.objects.all()[:30]:
                self.stdout.write(f"{mv.version:40} {mv.status:10} Brier {mv.metrics.get('model', {}).get('brier')}")
        elif action == "shadow":
            mv = self._get(o)
            mv.status = ModelVersion.Status.SHADOW if mv.status == ModelVersion.Status.CANDIDATE else mv.status
            mv.save(update_fields=["status"])
            res = gov.shadow_evaluate(mv, self._instruments(), tf)
            self.stdout.write(f"Shadow evaluation of {mv.version}: {res.get('n', 0)} resolved signals")
            if res.get("n"):
                self.stdout.write(f"  Brier: candidate {res['new']['brier']:.4f} vs live {res['live']['brier']:.4f}")
        elif action == "promote":
            mv = self._get(o)
            if not o["reason"]:
                raise CommandError("--reason is required")
            checks = gov.promote(mv, "operator", o["reason"], o["risk_approved"])
            for c in checks:
                self.stdout.write(f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}")
            if all(c.passed for c in checks):
                self.stdout.write(self.style.SUCCESS(f"{mv.version} is now production. Restart run_trader."))
            else:
                raise CommandError("Promotion refused: not every check passed.")
        elif action == "rollback":
            restored = gov.rollback(o["strategy"], "operator", o["reason"] or "rollback")
            self.stdout.write(f"Rolled back. Now in production: {restored.version if restored else 'none (hit rate)'}")
        elif action == "drift":
            self.stdout.write(str(gov.drift_check(o["strategy"])))

    def _get(self, o) -> ModelVersion:
        mv = ModelVersion.objects.filter(version=o["model_version"]).first()
        if mv is None:
            raise CommandError(f"no model version {o['model_version']!r}")
        return mv
