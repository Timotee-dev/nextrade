"""Which market-data sources can THIS computer reach? Tests public endpoints only (no keys).

    python manage.py probe_venues

Prints reachable / blocked / error for each venue with a 1-minute-history depth note, and which
source to use for research. Run it on the computer that will run the backtests and paper trader.
"""

from __future__ import annotations

import time

import httpx
from django.core.management.base import BaseCommand

# (name, url, history note). Public time or ticker endpoints: cheap and keyless.
PROBES = [
    ("bybit", "https://api.bybit.com/v5/market/time", "1m history back years (deep): best for 1m/3m/5m research"),
    ("gate", "https://api.gateio.ws/api/v4/spot/time", "1m history only ~7 days, 5m ~34 days (10,000-bar limit)"),
    ("okx", "https://www.okx.com/api/v5/public/time", "deep history; adapter not built yet"),
    ("kucoin", "https://api.kucoin.com/api/v1/timestamp", "deep history; adapter not built yet"),
    ("binance api", "https://api.binance.com/api/v3/time", "deepest history; blocked on this network before"),
    ("binance archive", "https://data.binance.vision/", "bulk files; may work even if the API is blocked"),
    ("kraken", "https://api.kraken.com/0/public/Time", "720 bars back only: not suitable"),
]


def probe(client: httpx.Client, url: str) -> tuple[str, float]:
    t0 = time.monotonic()
    try:
        r = client.get(url)
    except httpx.TimeoutException:
        return "TIMEOUT", time.monotonic() - t0
    except httpx.HTTPError as exc:
        return f"ERROR ({type(exc).__name__})", time.monotonic() - t0
    if r.status_code == 200:
        return "reachable", time.monotonic() - t0
    if r.status_code in (403, 451):
        return f"BLOCKED (HTTP {r.status_code})", time.monotonic() - t0
    return f"HTTP {r.status_code}", time.monotonic() - t0


class Command(BaseCommand):
    help = "Test which public market-data endpoints this computer can reach."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=10.0)

    def handle(self, *args, **o):
        results = {}
        with httpx.Client(timeout=o["timeout"], follow_redirects=True) as client:
            for name, url, note in PROBES:
                status, secs = probe(client, url)
                results[name] = status
                self.stdout.write(f"  {name:16s} {status:22s} {secs * 1000:6.0f} ms   {note}")
        if results.get("bybit") == "reachable":
            self.stdout.write(self.style.SUCCESS("Use Bybit for 1m/3m/5m research: load_bybit_history --top 30"))
        elif results.get("gate") == "reachable":
            self.stdout.write(self.style.WARNING(
                "Bybit unreachable. Gate works but only keeps ~7 days of 1m bars: use 5m (~34 days) and keep "
                "record_rest running to build 1m history yourself. Tell me the output and I will add OKX or KuCoin."))
        else:
            self.stdout.write(self.style.ERROR("No usual venue reachable: check the internet connection or VPN."))
