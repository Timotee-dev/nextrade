"""Data-quality report for stored 1-minute candles. Run it BEFORE validate_scalping.

    python manage.py check_data --venue bybit
    python manage.py check_data --venue bybit --days 30 --min-days 21

Per pair: candles, date range, missing bars and gaps, duplicates, invalid OHLCV rows, and whether
there is enough clean data for 1m, 3m and 5m testing. Writes research/validation/data-quality-*.md/json.
Failing pairs are listed with reasons; validate_scalping leaves them out and says so.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.market.coverage import check_pairs
from apps.market.models import Instrument
from nextrade_core.data.coverage import PairReport, Thresholds

now_utc = lambda: datetime.now(UTC)  # noqa: E731


def write_report(reports: list[PairReport], th: Thresholds, venue: str, now: datetime) -> Path:
    out = Path(settings.BASE_DIR) / "research" / "validation"
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M")
    (out / f"data-quality-{stamp}.json").write_text(json.dumps(
        {"venue": venue, "as_of": now.isoformat(), "thresholds": asdict(th), "pairs": [asdict(r) for r in reports]},
        indent=2, default=str))
    ok = [r for r in reports if r.usable]
    lines = [f"# Data quality: {venue}, {now:%Y-%m-%d %H:%M} UTC", "",
             f"{len(ok)} of {len(reports)} pairs fit for 1m/3m/5m testing. Thresholds: at least {th.min_days:g} days, "
             f"{th.min_completeness:.0%} of bars present, no gap over {th.max_gap_minutes} min, newest bar under "
             f"{th.max_age_hours:g} h old, invalid rows under {th.max_invalid_share:.2%}.", "",
             "| pair | candles | from | to | days | missing | complete | gaps (largest min) | dupes | invalid | 1m | 3m | 5m | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(reports, key=lambda r: r.symbol):
        tf = lambda t: f"{r.bars_by_tf.get(t, 0):,} {'ok' if r.ok_by_tf.get(t) else 'NO'}"  # noqa: E731
        lines.append(f"| {r.symbol} | {r.candles:,} | {r.first[:16]} | {r.last[:16]} | {r.days:g} | {r.missing:,} | "
                     f"{r.completeness:.2%} | {r.gaps} ({r.largest_gap_min}) | {r.duplicates} | "
                     f"{r.invalid_total} | {tf('1m')} | {tf('3m')} | {tf('5m')} | "
                     f"{'USABLE' if r.usable else 'EXCLUDED'} |")
    bad = [r for r in reports if not r.usable]
    if bad:
        lines += ["", "## Excluded pairs and why"]
        lines += [f"- {r.symbol}: " + "; ".join(r.reasons) for r in bad]
    path = out / f"data-quality-{stamp}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


class Command(BaseCommand):
    help = "Report coverage, gaps, duplicates and invalid rows for stored 1m candles."

    def add_arguments(self, parser):
        parser.add_argument("--venue", default="bybit")
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--days", type=float, default=None, help="only look at the latest N days")
        parser.add_argument("--min-days", type=float, default=21.0)
        parser.add_argument("--min-completeness", type=float, default=0.98)
        parser.add_argument("--max-gap-minutes", type=int, default=120)
        parser.add_argument("--max-age-hours", type=float, default=12.0)

    def handle(self, *args, **o):
        qs = Instrument.objects.filter(venue=o["venue"], candles__isnull=False).distinct().order_by("symbol")
        if o["symbols"]:
            qs = qs.filter(symbol__in=[s.strip().upper() for s in o["symbols"].split(",")])
        instruments = list(qs)
        if not instruments:
            raise CommandError(f"No stored {o['venue']} candles. Run load_{o['venue']}_history first.")
        th = Thresholds(o["min_days"], o["min_completeness"], o["max_gap_minutes"], o["max_age_hours"])
        now = now_utc()
        reports = check_pairs(instruments, now, window_days=o["days"], th=th)
        for r in reports:
            self.stdout.write(f"  {r.symbol:14s} {r.candles:>7,} candles  {r.days:>5g} d  {r.completeness:>7.2%} complete  "
                              f"gaps {r.gaps:>3} (max {r.largest_gap_min} min)  dupes {r.duplicates}  invalid {r.invalid_total}  "
                              + ("USABLE" if r.usable else "EXCLUDED: " + "; ".join(r.reasons)))
        ok = sum(r.usable for r in reports)
        path = write_report(reports, th, o["venue"], now)
        style = self.style.SUCCESS if ok == len(reports) else self.style.WARNING
        self.stdout.write(style(f"{ok} of {len(reports)} pairs fit for 1m/3m/5m testing. Report: {path}"))
