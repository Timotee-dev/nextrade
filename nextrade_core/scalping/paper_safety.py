"""Hard guarantees that a paper-trading process can never place a real exchange order.

Three independent layers, so no single mistake can open a path to real money:

1. Configuration: NEXTRADE_LIVE_ENABLED must not be "yes"; live approvals must not exist.
2. Structure: the venue client used for data has no order, cancel or withdrawal method; the only
   broker handed to the trader is the in-memory PaperBroker.
3. Runtime: `install_write_guard()` makes every non-GET HTTP request to a known exchange host raise
   before it leaves the machine. Reading prices (GET) and Telegram messages (POST to api.telegram.org)
   still work.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

EXCHANGE_HOSTS = ("bybit.", "binance.", "bnbstatic.", "gate.io", "gateio.", "quidax.", "okx.", "kraken.", "kucoin.",
                  "coinbase.", "mexc.", "bitget.", "htx.", "huobi.")
ORDER_WORDS = re.compile(r"(order|trade|cancel|withdraw|transfer|place|submit|buy|sell)", re.I)
CREDENTIAL_VARS = re.compile(r"^(BYBIT|BINANCE|GATE|GATEIO|QUIDAX|OKX|KUCOIN|MEXC|BITGET)_.*(KEY|SECRET|PASSPHRASE|TOKEN)", re.I)


class LiveOrderBlocked(RuntimeError):
    """Raised when something tries to send a state-changing request to an exchange."""


def is_exchange_host(host: str) -> bool:
    h = (host or "").lower()
    return any(m in h for m in EXCHANGE_HOSTS)


def _guard(method: str, url) -> None:
    if method.upper() not in ("GET", "HEAD", "OPTIONS") and is_exchange_host(urlparse(str(url)).hostname or ""):
        raise LiveOrderBlocked(f"blocked {method.upper()} {url}: paper mode may only READ from exchanges")


_installed = False


def install_write_guard() -> None:
    """Patch httpx once per process (sync and async clients)."""
    global _installed
    if _installed:
        return
    import httpx

    orig_send, orig_asend = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, *a, **kw):
        _guard(request.method, request.url)
        return orig_send(self, request, *a, **kw)

    async def asend(self, request, *a, **kw):
        _guard(request.method, request.url)
        return await orig_asend(self, request, *a, **kw)

    httpx.Client.send, httpx.AsyncClient.send = send, asend
    _installed = True


def order_methods(cls) -> list[str]:
    """Public methods of a venue client whose names suggest placing or moving funds."""
    return sorted(n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n)) and ORDER_WORDS.search(n))


Check = tuple[str, bool, str]


def safety_checks(base: Path, env: Mapping[str, str] | None = None, *, broker=None) -> list[Check]:
    from adapters.bybit.client import BybitClient
    from adapters.gate.client import GateClient
    from adapters.paper.broker import PaperBroker
    from nextrade_core.scalping import approvals

    env = os.environ if env is None else env
    out: list[Check] = []
    live = approvals.live_enabled(dict(env))
    out.append(("live trading flag is off", not live,
                "NEXTRADE_LIVE_ENABLED is not 'yes'" if not live else "NEXTRADE_LIVE_ENABLED=yes: REFUSING to run"))
    live_approved = [k for k, v in approvals.load(base).items() if v.live]
    out.append(("no strategy approved for live", not live_approved,
                "none" if not live_approved else f"approved for live: {live_approved}"))
    creds = sorted(k for k in env if CREDENTIAL_VARS.match(k) and env[k])
    out.append(("no exchange API credentials in the environment (warning only)", True,
                "none (paper trading needs no keys)" if not creds else
                f"WARNING: {creds} are set; paper mode never uses them, but remove them if you do not need them"))
    bad = {c.__name__: order_methods(c) for c in (BybitClient, GateClient) if order_methods(c)}
    out.append(("venue data clients have no order/withdraw methods", not bad,
                "BybitClient and GateClient expose read-only market data only" if not bad else f"found {bad}"))
    if broker is not None:
        out.append(("the broker is the in-memory PaperBroker", type(broker) is PaperBroker, type(broker).__name__))
    out.append(("write guard installed (non-GET to exchanges raises)", _installed,
                "active" if _installed else "NOT installed in this process"))
    return out


def all_ok(checks: list[Check]) -> bool:
    return all(ok for _, ok, _ in checks)
