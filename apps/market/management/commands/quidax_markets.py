"""List Quidax markets with their live spread and 24h volume, to choose what to trade.

Thin books cost money on every trade: a market is only worth trading if its spread is
small and enough naira changes hands each day for NEXTRADE's orders to fill.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from adapters.base import VenueError

from ._quidax import make_quidax


class Command(BaseCommand):
    help = "Show Quidax markets ranked by 24h volume (in NGN), with spreads."

    def add_arguments(self, parser):
        parser.add_argument("--quote", default="NGN", help="quote currency to list (NGN or USDT)")
        parser.add_argument("--top", type=int, default=30)

    def handle(self, *args, **o):
        client = make_quidax()
        try:
            tickers = client.tickers()
        except VenueError as exc:
            raise CommandError(f"Cannot reach Quidax: {exc}") from exc
        finally:
            client.close()
        quote = o["quote"].lower()
        usdt_ngn = _dec(tickers.get("usdtngn", {}).get("ticker", {}).get("last"))
        rows = []
        for market, entry in tickers.items():
            if not market.endswith(quote):
                continue
            t = entry.get("ticker", {})
            last, bid, ask, vol = (_dec(t.get(k)) for k in ("last", "buy", "sell", "vol"))
            if not last or not bid or not ask or ask <= bid:
                continue
            spread_bps = float((ask - bid) / ((ask + bid) / 2)) * 1e4
            vol_quote = vol * last if vol else Decimal(0)
            vol_ngn = vol_quote if quote == "ngn" else (vol_quote * usdt_ngn if usdt_ngn else None)
            rows.append((market.upper(), float(last), spread_bps, float(vol_ngn) if vol_ngn is not None else 0.0))
        rows.sort(key=lambda r: -r[3])
        self.stdout.write(f"{'market':<12}{'last':>16}{'spread bps':>12}{'24h volume (NGN)':>20}")
        for m, last, sp, v in rows[: o["top"]]:
            flag = "" if sp <= 60 else "   too wide for NARA (60 bps)"
            self.stdout.write(f"{m:<12}{last:>16,.4f}{sp:>12.1f}{v:>20,.0f}{flag}")
        self.stdout.write("\nTrade only markets with a tight spread AND real volume; pick research coins from these.")


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None
