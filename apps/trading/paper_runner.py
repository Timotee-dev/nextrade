"""Pre-flight checks and data backfill for the paper scalper. Reads public market data only."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from adapters.base import VenueError
from adapters.bybit.client import is_pegged
from apps.market.models import Instrument
from apps.market.store import CandleStore
from apps.trading.models import Alert
from nextrade_core.data.candles import Timeframe
from nextrade_core.scalping import approvals, paper_safety
from nextrade_core.strategies.registry import SCALP_PREREGISTERED

Check = tuple[str, bool, str]


def eligible_instruments(venue: str) -> dict[str, Instrument]:
    out = {}
    for i in Instrument.objects.filter(venue=venue, tradable=True, candles__isnull=False).distinct():
        base = i.symbol[:-4] if i.symbol.endswith("USDT") else i.symbol
        if i.symbol.endswith("USDT") and not is_pegged(base):
            out[i.symbol] = i
    return out


def backfill(client, store: CandleStore, instruments: dict[str, Instrument], now: datetime,
             max_days: float = 3.0) -> dict:
    """Fetch every missing closed 1m bar from the last stored bar up to now (resumable, public data)."""
    tf, end = Timeframe.M1, Timeframe.M1.floor(now)
    got = failed = 0
    errors: list[str] = []
    for sym in instruments:
        last = store.last_open_time(sym, tf)
        start = max((last + tf.delta) if last else end - timedelta(minutes=30), end - timedelta(days=max_days))
        if start >= end:
            continue
        try:
            bars = client.candles(sym, tf, start, end)
            store.save_candles(bars, "rest")
            got += len(bars)
        except VenueError as exc:
            failed += 1
            errors.append(f"{sym}: {str(exc)[:80]}")
    return {"bars": got, "failed": failed, "errors": errors[:5]}


def freshness(instruments: dict[str, Instrument], now: datetime, max_bar_age_min: int = 3) -> dict:
    """How many markets currently pass the trader's own data check (last bar fresh, >=57 of the last 60 minutes)."""
    from apps.market.models import Candle as CandleRow
    store = CandleStore(instruments)
    fresh, stale = 0, []
    for sym, inst in instruments.items():
        last = store.last_open_time(sym)
        recent = CandleRow.objects.filter(instrument=inst, timeframe="1m", open_time__gte=now - timedelta(minutes=60)).count()
        if last is not None and now - (last + timedelta(minutes=1)) <= timedelta(minutes=max_bar_age_min) and recent >= 57:
            fresh += 1
        else:
            stale.append(sym)
    return {"fresh": fresh, "total": len(instruments), "stale": stale}


def telegram_status(env=None) -> tuple[bool, str]:
    from apps.notify.telegram import TOKEN_FORMAT
    env = os.environ if env is None else env
    tok, chats = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    if not tok or not chats.strip():
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_CHAT_IDS not set in .env: no phone notifications"
    if not TOKEN_FORMAT.match(tok):
        return False, "TELEGRAM_BOT_TOKEN is not a full bot token (copy it again from @BotFather): no phone notifications"
    if len(env.get("TELEGRAM_PIN", "")) < 6:
        return False, "TELEGRAM_PIN (6+ characters) not set in .env: the bot will not start"
    return True, "configured"


def clear_old_alerts(now: datetime) -> int:
    """A fresh start must not replay hundreds of old alerts to the phone."""
    return Alert.objects.filter(delivered_at=None, created_at__lt=now).update(delivered_at=now)


def preflight(base: Path, venue: str, client, now: datetime, env=None) -> tuple[list[Check], dict]:
    checks: list[Check] = list(paper_safety.safety_checks(base, env))
    ap = approvals.load(base)
    scalp = [c for c in SCALP_PREREGISTERED if c in ap and ap[c].paper]
    checks.append(("scalp strategies authorised for paper", bool(scalp),
                   ", ".join(scalp) if scalp else "none: run authorise_paper_trial"))
    inst = eligible_instruments(venue)
    checks.append(("eligible markets stored (pegged coins excluded)", len(inst) >= 1,
                   f"{len(inst)} markets" + ("" if len(inst) >= 20 else " (fewer than the ~28 expected: load more with load_bybit_history)")))
    info: dict = {"instruments": inst, "strategies": scalp}
    try:
        quotes = client.quotes()
        live = [s for s in inst if s in quotes]
        checks.append((f"{venue} live feed reachable (public REST)", len(live) >= max(1, int(0.9 * len(inst))),
                       f"live bid/ask for {len(live)} of {len(inst)} markets"))
        info["live_quotes"] = len(live)
    except VenueError as exc:
        checks.append((f"{venue} live feed reachable (public REST)", False, f"{exc}. Run probe_venues; check DNS (1.1.1.1)"))
    return checks, info


def report(checks: list[Check]) -> str:
    return "\n".join(f"  [{'OK' if ok else 'FAIL'}] {n}: {d}" for n, ok, d in checks)

