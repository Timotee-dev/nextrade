"""Check stored 1m data: gaps, duplicates, bad bars, spikes, freshness, and resample accuracy.

Acceptance for Phase 2: completeness >= 99.9% over the checked window, and every
resampled 1h bar identical to Binance's own 1h bar.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from adapters.base import VenueError
from adapters.binance.klines import fetch_closed_klines
from apps.market.models import Instrument
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe
from nextrade_core.data.quality import QualityScanner
from nextrade_core.data.resample import resample

from ._common import make_rest, parse_day, parse_symbols

TF = Timeframe.M1
MAX_EVENTS_PER_KIND = 200
now_utc = lambda: datetime.now(UTC)  # noqa: E731
make_client = make_rest


def compare_hourly(stored_1m, binance_1h) -> list[dict]:
    """Differences between our resampled 1h bars and Binance's 1h bars."""
    ours = {c.open_time: c for c in resample(stored_1m, Timeframe.H1).candles}
    diffs = []
    for ref in binance_1h:
        mine = ours.get(ref.open_time)
        if mine is None:
            diffs.append({"at": ref.open_time.isoformat(), "problem": "missing or incomplete hour"})
            continue
        fields = [f for f in ("open", "high", "low", "close", "volume", "trade_count")
                  if getattr(mine, f) != getattr(ref, f)]
        if fields:
            diffs.append({"at": ref.open_time.isoformat(), "fields": fields})
    return diffs


class Command(BaseCommand):
    help = "Run data-quality checks on stored 1m candles and write a report to reports/."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None)
        parser.add_argument("--days", type=int, default=7, help="window to check, ending now")
        parser.add_argument("--start", help="YYYY-MM-DD (overrides --days)")
        parser.add_argument("--hours-vs-binance", type=int, default=24,
                            help="hours of 1h bars to compare with Binance (0 to skip)")

    def handle(self, *args, **opts):
        symbols = parse_symbols(opts["symbols"])
        now = now_utc()
        end = TF.floor(now)  # bars with open_time < end are closed
        start = parse_day(opts["start"]) if opts["start"] else end - timedelta(days=opts["days"])
        instruments = {i.symbol: i for i in Instrument.objects.filter(symbol__in=symbols)}
        store = CandleStore(instruments, run_label=f"dq-{now:%Y%m%dT%H%M}")
        rest = make_client() if opts["hours_vs_binance"] > 0 else None
        report = {"run_at": now.isoformat(), "window": [start.isoformat(), end.isoformat()], "symbols": []}
        all_pass = True

        self.stdout.write(f"Window {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC\n")
        self.stdout.write(f"{'symbol':<10}{'bars':>9}{'complete':>10}{'gaps':>6}{'missing':>9}"
                          f"{'bad':>5}{'spikes':>7}{'last bar age':>14}{'1h vs Binance':>15}")
        for symbol in symbols:
            if symbol not in instruments:
                self.stdout.write(f"{symbol:<10} no data (run load_binance_history first)")
                all_pass = False
                continue
            scanner = QualityScanner(symbol, TF)
            for chunk in store.iter_candles(symbol, start, end):
                scanner.feed(chunk)
            last = store.last_open_time(symbol)
            age = (now - (last + TF.delta)) if last else None
            completeness = scanner.completeness(start, end)

            hourly = "skipped"
            hourly_diffs: list[dict] = []
            if rest is not None:
                h_end = Timeframe.H1.floor(now)
                h_start = h_end - timedelta(hours=opts["hours_vs_binance"])
                try:
                    ref = fetch_closed_klines(rest, symbol, Timeframe.H1, h_start, now)
                    mine = [c for chunk in store.iter_candles(symbol, h_start, h_end) for c in chunk]
                    hourly_diffs = compare_hourly(mine, [r for r in ref if r.open_time < h_end])
                    hourly = "match" if not hourly_diffs else f"{len(hourly_diffs)} differ"
                except VenueError as exc:
                    hourly = "unreachable"
                    hourly_diffs = [{"error": str(exc)}]

            s = scanner.summary()
            bad = sum(v for k, v in s["issues"].items() if k in ("duplicate", "out_of_order",
                      "ohlc_inconsistent", "non_positive_price", "negative_volume"))
            ok = completeness >= 0.999 and bad == 0 and hourly in ("match", "skipped")
            all_pass &= ok
            age_txt = f"{age.total_seconds() / 60:.0f} min" if age is not None else "-"
            self.stdout.write(f"{symbol:<10}{s['rows']:>9}{completeness:>10.3%}{s['issues'].get('gap', 0):>6}"
                              f"{s['missing_bars']:>9}{bad:>5}{s['issues'].get('spike', 0):>7}"
                              f"{age_txt:>14}{hourly:>15}")

            per_kind: dict[str, int] = {}
            for issue in scanner.issues:
                per_kind[issue.kind] = per_kind.get(issue.kind, 0) + 1
                if per_kind[issue.kind] <= MAX_EVENTS_PER_KIND:
                    store.record_issue(issue.kind.value, symbol, issue.detail, timeframe=TF.value, at=issue.at)
            report["symbols"].append({**s, "completeness_in_window": completeness, "pass": ok,
                                      "last_bar_age_s": age.total_seconds() if age else None,
                                      "hourly_vs_binance": hourly, "hourly_diffs": hourly_diffs[:50],
                                      "gaps": [{"from": i.at.isoformat(), **i.detail}
                                               for i in scanner.issues if i.kind == "gap"][:100]})
        if rest is not None:
            rest.close()

        out_dir = Path(settings.BASE_DIR) / "reports"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"data_quality_{now:%Y%m%dT%H%M%SZ}.json"
        report["pass"] = all_pass
        out.write_text(json.dumps(report, indent=2, default=str))
        verdict = self.style.SUCCESS("PASS") if all_pass else self.style.ERROR("FAIL")
        self.stdout.write(f"\nResult: {verdict} (needs >= 99.9% complete, no bad bars, 1h bars match)")
        self.stdout.write(f"Report: {out.relative_to(settings.BASE_DIR)}")
