"""Download forex history from the MetaTrader 5 terminal on this PC (Windows only).

Open MT5 and log in to your DEMO account first. Then:

    python manage.py load_mt5_history --suffix c          (Exness cent account: EURUSDc ...)

Resumable: each symbol/timeframe continues from the newest stored bar. The broker's spread
on each bar is kept as a quote sample, so backtests use your broker's real spreads.
How far back you get depends on the broker and on MT5's "Max bars in chart" setting
(Tools > Options > Charts: set it to Unlimited for the most history).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from statistics import median

from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError
from adapters.mt5.local import FOREX_MAJORS, MT5Local
from apps.market.models import Instrument
from apps.market.services import save_instrument
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe

now_utc = lambda: datetime.now(UTC)  # noqa: E731


def make_mt5(**kw) -> MT5Local:
    return MT5Local(**kw)


class Command(BaseCommand):
    help = "Download forex candles and spreads from the local MetaTrader 5 terminal (research data)."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=",".join(FOREX_MAJORS))
        parser.add_argument("--timeframes", default="15m,1h,4h,1d")
        parser.add_argument("--years", type=float, default=5.0, help="how far back on the first run")
        parser.add_argument("--suffix", default="", help="broker symbol suffix, e.g. c for EURUSDc")
        parser.add_argument("--utc-offset", type=int, default=None,
                            help="broker server time minus UTC in hours (auto-detected while the market is open)")

    def handle(self, *args, **o):
        symbols = [s.strip().upper() for s in o["symbols"].split(",") if s.strip()]
        tfs = [Timeframe(t.strip()) for t in o["timeframes"].split(",") if t.strip()]
        now = now_utc()
        login = os.environ.get("MT5_LOGIN")
        try:
            client = make_mt5(suffix=o["suffix"], server_offset_hours=o["utc_offset"],
                              login=int(login) if login else None,
                              password=os.environ.get("MT5_PASSWORD") or None,
                              server=os.environ.get("MT5_SERVER") or None)
        except VenueError as exc:
            raise CommandError(str(exc)) from exc
        try:
            acct = client.account()
            mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(acct.get("trade_mode"), "?")
            self.stdout.write(f"MT5 account {acct['login']} on {acct['server']} ({acct.get('company')}): "
                              f"{mode}, {acct['balance']} {acct['currency']}, leverage 1:{acct['leverage']}")
            if mode == "REAL":
                self.stdout.write(self.style.WARNING(
                    "This is a REAL account. Downloading history is read-only and safe, "
                    "but NEXTRADE will only paper trade on a DEMO account."))
            points = {}
            for s in symbols:
                try:
                    inst = client.instrument(s)
                except VenueError as exc:
                    raise CommandError(str(exc)) from exc
                save_instrument(inst)
                points[s] = inst.tick_size
            store = CandleStore({i.symbol: i for i in Instrument.objects.filter(venue="mt5", symbol__in=symbols)},
                                run_label="load_mt5")
            offset = client.server_offset(symbols[0], now)
            self.stdout.write(f"Broker server clock: UTC{offset:+d} (bar times are stored in real UTC)")
            for s in symbols:
                for tf in tfs:
                    last = store.last_open_time(s, tf)
                    start = last + tf.delta if last else tf.floor(now - timedelta(days=365 * o["years"]))
                    try:
                        bars, spreads = client.rates(s, tf, start, tf.floor(now), now)
                    except VenueError as exc:
                        self.stdout.write(self.style.ERROR(f"  {s} {tf.value}: {exc}"))
                        continue
                    store.save_candles(bars, "rest")
                    if tf is Timeframe.H1 and bars:
                        recent = [(c, sp) for c, sp in zip(bars, spreads, strict=True)
                                  if c.open_time >= now - timedelta(days=30)]
                        if recent:
                            store.save_quotes(client.quotes_from_bars(
                                s, [c for c, _ in recent], [sp for _, sp in recent], points[s]))
                    span = f" ({bars[0].open_time:%Y-%m-%d} -> {bars[-1].open_time:%Y-%m-%d})" if bars else ""
                    typical = ""
                    if bars and any(spreads):
                        bps = [float(sp * points[s] / c.close) * 1e4 for c, sp in zip(bars, spreads, strict=True)
                               if sp > 0 and c.close > 0]
                        typical = f", typical spread {median(bps):.2f} bps" if bps else ""
                    self.stdout.write(f"  {s} {tf.value}: {len(bars)} new bars{span}{typical}")
        finally:
            client.close()
        self.stdout.write(self.style.SUCCESS("Done. Forex research needs short trades and leverage; "
                                             "those come in the next NEXTRADE batch."))

