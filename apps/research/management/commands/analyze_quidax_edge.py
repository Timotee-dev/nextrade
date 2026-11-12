"""Measure the two Quidax microstructure edges on recorded order books (see record_books).

    python manage.py analyze_quidax_edge --days 14

EXPLORATORY: the gap thresholds are a sweep. Whatever looks good here must be written down
as fixed rules and confirmed on NEW recordings before any paper trading (as ADR 0006-0010).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.market.models import BookSnapshot, Instrument
from nextrade_core.backtest.experiments import write_record
from nextrade_core.micro.edge import align_world, gap_summary, gap_sweep, simulate_market_making, top_of_book


def _rows(venue: str, symbol: str, since: datetime):
    return list(BookSnapshot.objects.filter(venue=venue, symbol=symbol, ts__gte=since)
                .order_by("ts").values("ts", "bids", "asks"))


def _tick(symbol: str, rows) -> float:
    inst = Instrument.objects.filter(venue="quidax", symbol=symbol).first()
    if inst and inst.tick_size > 0:
        return float(inst.tick_size)
    decimals = max((len(p.split(".")[1]) if "." in p else 0)
                   for r in rows[:200] for p in [x[0] for x in r["bids"] + r["asks"]])
    return float(Decimal(1).scaleb(-decimals))


class Command(BaseCommand):
    help = "Price gaps vs world prices and simulated market making, from recorded Quidax books."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=float, default=14.0)
        parser.add_argument("--taker-fee", type=float, default=0.0015, help="Quidax taker fee per side")
        parser.add_argument("--maker-fee", type=float, default=0.0015,
                            help="Quidax maker fee per side (unconfirmed: same as taker by default)")
        parser.add_argument("--order-ngn", type=float, default=1250.0)
        parser.add_argument("--max-inventory-ngn", type=float, default=6250.0)

    def handle(self, *args, **o):
        since = datetime.now(UTC) - timedelta(days=o["days"])
        usdt = top_of_book(_rows("quidax", "USDTNGN", since))
        markets = sorted(set(BookSnapshot.objects.filter(venue="quidax", ts__gte=since)
                             .exclude(symbol="USDTNGN").values_list("symbol", flat=True)))
        if not markets or usdt.empty:
            raise CommandError("No recorded books yet. Run: python manage.py record_books  (leave it running)")
        self.stdout.write(f"Quidax books since {since:%Y-%m-%d %H:%M} UTC; taker fee {o['taker_fee']:.2%}, "
                          f"maker fee {o['maker_fee']:.2%} per side; orders of NGN {o['order_ngn']:,.0f}")
        record = {"since": str(since), "params": {k: o[k] for k in ("taker_fee", "maker_fee", "order_ngn",
                                                                      "max_inventory_ngn")}, "markets": {}}
        self.stdout.write("gap = Quidax price vs world price (Gate.io x USDT/NGN); table: buy when Quidax is k bps "
                          "cheaper, sell when the gap closes or after 60 min.\n"
                          "market making = quotes one tick inside the spread; fills only when price trades through.\n")
        for m in markets:
            rows = _rows("quidax", m, since)
            book = top_of_book(rows)
            base = m.removesuffix("NGN")
            world = top_of_book(_rows("gate", f"{base}USDT", since))
            out = {}
            self.stdout.write(f"== {m}  ({len(book):,} snapshots)")
            if not world.empty and len(book) > 10:
                joined = align_world(book, world, usdt)
                if len(joined) > 10:
                    s = gap_summary(joined, o["taker_fee"])
                    sweep = gap_sweep(joined, (25, 50, 75, 100, 150, 200), o["taker_fee"])
                    out["gap"] = {"summary": s, "sweep": json.loads(sweep.to_json(orient="records"))}
                    self.stdout.write(
                        f"  gap: median {s['gap_median_bps']:+.1f} bps (5%-95%: {s['gap_p05_bps']:+.1f} to "
                        f"{s['gap_p95_bps']:+.1f}); spread {s['spread_median_bps']:.1f}; round trip cost "
                        f"{s['round_trip_cost_median_bps']:.1f}; gap beyond cost {s['share_gap_beyond_cost']:.1%} of the time")
                    self.stdout.write("  " + sweep.to_string(index=False).replace("\n", "\n  "))
            if len(book) > 10:
                mm = simulate_market_making(book, tick=_tick(m, rows), order_ngn=o["order_ngn"],
                                            max_inventory_ngn=o["max_inventory_ngn"], maker_fee=o["maker_fee"])
                out["market_making"] = mm.__dict__
                self.stdout.write(
                    f"  market making: {mm.bid_fills} buys, {mm.ask_fills} sells over {mm.days:.1f} days; "
                    f"spread earned NGN {mm.spread_earned_ngn:,.0f}, inventory P&L NGN {mm.inventory_pnl_ngn:,.0f}, "
                    f"net NGN {mm.net_ngn:,.0f} (NGN {mm.per_day_ngn:,.0f}/day), max inventory NGN {mm.max_inventory_ngn:,.0f}")
            record["markets"][m] = out
        path = write_record(Path(settings.BASE_DIR) / "research" / "experiments", "quidax_edge", record)
        self.stdout.write(f"\nEXPLORATORY: confirm any rule on NEW recordings before paper trading. "
                          f"Record: {path.relative_to(settings.BASE_DIR)}")
