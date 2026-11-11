"""Django implementation of the daemon's TradingStore and MarketSource ports."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.market.models import Candle as CandleRow
from apps.market.models import Instrument, QuoteSample
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe
from nextrade_core.strategies.base import Side, Signal

from .models import (Alert, ControlRequest, EquitySnapshot, Heartbeat, LedgerEntry, OpportunityRecord, OrderEventRecord,
                     OrderRecord, Position, RiskAssessmentRecord, SignalRecord)


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(repr(float(x)))


class DjangoTradingStore:
    def record_signal(self, run_id, signal, regime):
        close_old_connections()
        row, _ = SignalRecord.objects.get_or_create(run_id=run_id, key=signal.key, defaults=dict(
            strategy_version=signal.strategy_version, symbol=signal.symbol, timeframe=signal.timeframe.value,
            bar_time=signal.bar_time, entry=_d(signal.entry), stop=_d(signal.stop), target=_d(signal.target),
            max_hold_bars=signal.max_hold_bars, regime=regime or "", features=signal.features))
        return row.pk

    def record_opportunity(self, signal_id, opp):
        row, _ = OpportunityRecord.objects.update_or_create(signal_id=signal_id, defaults=dict(
            accepted=opp.accepted, reasons=[r.value for r in opp.reasons], p=opp.p, p_source=opp.p_source,
            uncertainty=opp.uncertainty, cost_r=opp.cost_r, expected_net_r=opp.expected_net_r, score=opp.score,
            explanation=opp.explanation))
        return row.pk

    def record_risk(self, opportunity_id, decision):
        RiskAssessmentRecord.objects.update_or_create(opportunity_id=opportunity_id, defaults=dict(
            approved=decision.approved, qty=decision.qty, risk_amount=decision.risk_amount,
            notional=decision.notional, limits_version=decision.limits_version,
            rules=[{"rule": r.rule, "passed": r.passed, "detail": r.detail} for r in decision.rules]))

    @transaction.atomic
    def save_order(self, run_id, order, purpose, position_id):
        close_old_connections()
        r = order.request
        row, _ = OrderRecord.objects.update_or_create(client_order_id=order.client_order_id, defaults=dict(
            run_id=run_id, position_id=position_id, purpose=purpose, symbol=r.symbol, side=r.side.value,
            type=r.type.value, qty=r.qty, price=r.price, stop_price=r.stop_price, status=order.status.value,
            filled_qty=order.filled_qty, avg_price=order.avg_price, fee=order.fee,
            venue_order_id=order.venue_order_id or ""))
        known = row.events.count()
        for ev in order.events[known:]:
            OrderEventRecord.objects.create(order=row, at=ev.at, from_status=ev.from_status.value,
                                            to_status=ev.to_status.value, filled_qty=ev.filled_qty, note=ev.note)

    def open_position(self, run_id, **f):
        sig = f.pop("signal_obj", None)
        signal_row = SignalRecord.objects.filter(run_id=run_id, key=sig.key).first() if sig else None
        row = Position.objects.create(run_id=run_id, signal=signal_row, **f)
        return row.pk

    def update_position(self, position_id, **f):
        Position.objects.filter(pk=position_id).update(**f)

    def close_position(self, position_id, **f):
        Position.objects.filter(pk=position_id).update(status=Position.Status.CLOSED, **f)

    def open_positions(self, run_id):
        close_old_connections()
        out = []
        for p in Position.objects.filter(run_id=run_id, status=Position.Status.OPEN).select_related("signal"):
            d = {k: getattr(p, k) for k in ("id", "symbol", "strategy", "strategy_version", "qty", "entry_price",
                                            "entry_fee", "stop", "target", "initial_risk", "thesis", "health",
                                            "bars_held", "exit_list_id", "opened_at")}
            if p.signal is not None:
                s, t = p.signal, p.thesis or {}
                d["signal_obj"] = Signal(p.strategy, s.strategy_version, s.symbol, Timeframe(s.timeframe),
                                         s.bar_time, Side.LONG, float(s.entry), float(s.stop), float(s.target),
                                         s.max_hold_bars, t.get("invalidation", ""), t.get("invalidation_level"),
                                         s.features)
            out.append(d)
        return out

    def recent_closed_pnls(self, run_id, limit):
        return list(Position.objects.filter(run_id=run_id, status=Position.Status.CLOSED)
                    .exclude(realized_pnl=None).order_by("-closed_at").values_list("realized_pnl", flat=True)[:limit])

    def ledger(self, run_id, at, asset, amount, kind, ref):
        LedgerEntry.objects.create(run_id=run_id, at=at, asset=asset, amount=amount, kind=kind, ref=ref)

    def equity_snapshot(self, run_id, **f):
        EquitySnapshot.objects.create(run_id=run_id, **f)

    def equity_history(self, run_id):
        return list(EquitySnapshot.objects.filter(run_id=run_id).values_list("at", "equity"))

    def heartbeat(self, process, at, detail):
        close_old_connections()
        Heartbeat.objects.update_or_create(process=process, defaults={"at": at, "detail": detail})

    def alert(self, level, kind, message):
        Alert.objects.create(level=level, kind=kind, message=message)


class DjangoMarketSource:
    """Reads what the recorder (record_live) writes: 1m candles and sampled quotes."""

    def __init__(self, instruments: dict[str, Instrument], max_quote_age_s: int = 120,
                 max_bar_age_min: int = 3) -> None:
        self.instruments = instruments
        self.candles = CandleStore(instruments)
        self.max_quote_age = timedelta(seconds=max_quote_age_s)
        self.max_bar_age = timedelta(minutes=max_bar_age_min)

    def frame(self, symbol, timeframe, bars, now):
        start = timeframe.floor(now) - timeframe.delta * (bars + 1)
        f = self.candles.load_frame(symbol, timeframe, start, timeframe.floor(now))
        return f.iloc[-bars:]

    def last_closed_bar(self, symbol, timeframe, now):
        last_minute = self.candles.last_open_time(symbol)
        if last_minute is None:
            return None
        return timeframe.floor(last_minute + timedelta(minutes=1)) - timeframe.delta

    def latest_quote(self, symbol):
        q = (QuoteSample.objects.filter(instrument=self.instruments[symbol]).order_by("-ts")
             .values_list("bid", "ask", "ts").first())
        if q is None or timezone.now() - q[2] > self.max_quote_age:
            return None
        return q

    def data_ok(self, symbol, now):
        last = self.candles.last_open_time(symbol)
        if last is None or now - (last + timedelta(minutes=1)) > self.max_bar_age:
            return False
        recent = CandleRow.objects.filter(instrument=self.instruments[symbol], timeframe="1m",
                                          open_time__gte=now - timedelta(minutes=60)).count()
        return recent >= 57   # at most 3 missing minutes in the last hour


class OffLoop:
    """Run every method of `target` on one dedicated worker thread.

    The trading daemon is async; Django's ORM refuses to run inside an event loop.
    Calls are made from the loop and block it until the worker returns: database
    calls are short, and the daemon's cycle is sequential by design. One worker
    thread means one database connection and strictly ordered writes.
    """

    def __init__(self, target, name: str = "db") -> None:
        from concurrent.futures import ThreadPoolExecutor
        self._target = target
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"nextrade-{name}")

    def __getattr__(self, item):
        attr = getattr(self._target, item)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            return self._pool.submit(attr, *args, **kwargs).result()
        return call

    def close(self) -> None:
        def _close():
            from django.db import connection
            connection.close()
        self._pool.submit(_close).result()
        self._pool.shutdown(wait=True)


class ControlPoller:
    """Control source for the daemon: unprocessed ControlRequests, plus any EMERGENCY or
    RISK_SHUTDOWN the watchdog recorded since the trader started (the trader adopts it)."""

    def __init__(self, trader_started_at) -> None:
        self.watchdog_seen = trader_started_at

    def poll(self):
        from apps.ops.models import SystemStateTransition
        from nextrade_core.controller.state_machine import Actor, TradingState

        out = []
        for req in ControlRequest.objects.filter(processed_at=None):
            out.append((TradingState(req.target_state), Actor.OPERATOR, f"{req.source}: {req.reason}"))
            req.processed_at = timezone.now()
            req.result = "applied by trader"
            req.save(update_fields=["processed_at", "result"])
        for t in SystemStateTransition.objects.filter(actor="watchdog", at__gt=self.watchdog_seen).order_by("at"):
            out.append((TradingState(t.to_state), Actor.WATCHDOG, f"adopted from watchdog: {t.reason}"))
            self.watchdog_seen = t.at
        return out
