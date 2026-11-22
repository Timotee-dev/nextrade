"""Load Bybit 1-minute history for the most liquid USDT pairs (research data, no keys).

    python manage.py load_bybit_history --top 30 --days 30

3m and 5m bars are built from the stored 1m bars when you backtest, so only 1m is stored.
Resumable and extendable: re-run with a larger --days to go further back. The universe (the top-N
pairs by 24h turnover TODAY) is written to research/universe_bybit.json. Ranking by today's
turnover favours coins that did well and stayed liquid, so results are somewhat optimistic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.bybit.client import BybitClient, pick_top_usdt
from apps.market.models import Instrument
from apps.market.services import save_instrument
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

now_utc = lambda: datetime.now(UTC)  # noqa: E731
make_bybit = lambda: BybitClient()  # noqa: E731


class Command(BaseCommand):
    help = "Download Bybit 1m candles for the top USDT pairs."

    def add_arguments(self, parser):
        parser.add_argument("--top", type=int, default=30)
        parser.add_argument("--symbols", default=None, help="comma list, e.g. BTCUSDT,ETHUSDT (overrides --top)")
        parser.add_argument("--days", type=float, default=30)

    def handle(self, *args, **o):
        now = now_utc()
        client = make_bybit()
        try:
            try:
                if o["symbols"]:
                    symbols = [s.strip().upper() for s in o["symbols"].split(",") if s.strip()]
                else:
                    ranked = pick_top_usdt(client.tickers(), o["top"])
                    symbols = [t["symbol"] for t in ranked]
                    turnover = {t["symbol"]: float(t.get("turnover24h") or 0) for t in ranked}
                infos = client.instruments(symbols)
            except VenueError as exc:
                raise CommandError(f"Cannot reach Bybit: {exc}. Run probe_venues to see what this network allows.") from exc
            for inst in infos:
                save_instrument(inst)
            missing = sorted(set(symbols) - {i.symbol for i in infos})
            if missing:
                self.stdout.write(self.style.WARNING(f"  no instrument info, skipped: {', '.join(missing)}"))
            symbols = [s for s in symbols if s not in missing]
            store = CandleStore({i.symbol: i for i in Instrument.objects.filter(venue="bybit", symbol__in=symbols)},
                                run_label="load_bybit")
            tf, start, end = Timeframe.M1, Timeframe.M1.floor(now - timedelta(days=o["days"])), Timeframe.M1.floor(now)
            if not o["symbols"]:
                path = Path(settings.BASE_DIR) / "research" / "universe_bybit.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"as_of": now.isoformat(), "rule": "top by 24h turnover, USDT spot, "
                                            "no stablecoins or leveraged tokens", "symbols": symbols,
                                            "turnover24h": turnover}, indent=2))
            failed: list[str] = []
            for s in symbols:
                first, last = store.first_open_time(s, tf), store.last_open_time(s, tf)
                ranges = [(start, end)] if last is None else []
                if last is not None:
                    if first > start:
                        ranges.append((start, first))
                    ranges.append((last + tf.delta, end))
                total = 0
                try:
                    for a, b in ranges:
                        if a < b:
                            bars = client.candles(s, tf, a, b)
                            store.save_candles(bars, "rest")
                            total += len(bars)
                except VenueError as exc:
                    failed.append(s)
                    self.stdout.write(self.style.ERROR(f"  {s}: FAILED after retries: {exc}"))
                    continue
                self.stdout.write(f"  {s}: {total:,} new 1m bars")
        finally:
            client.close()
        ok = len(symbols) - len(failed)
        if failed:
            self.stdout.write(self.style.WARNING(
                f"{ok} of {len(symbols)} pairs loaded. FAILED: {', '.join(failed)}. Run the same command again: "
                "it only fetches what is missing."))
        else:
            self.stdout.write(self.style.SUCCESS(f"All {ok} pairs loaded. Next: validate_scalping --venue bybit"))
