"""Read-side reporting shared by the dashboard, the API, Telegram and scheduled reports.

Every figure is computed from stored records (ledger, positions, equity snapshots),
never from in-memory daemon state, so all surfaces agree. A run is either paper or
live; nothing here ever mixes the two.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.market.models import Candle, Instrument
from apps.ops.models import SystemStateTransition

from .models import Alert, EquitySnapshot, Heartbeat, LedgerEntry, OpportunityRecord, OrderRecord, Position, TradingRun

D0 = Decimal(0)


def current_run(mode: str = "paper", run_id: int | None = None) -> TradingRun | None:
    qs = TradingRun.objects.filter(mode=mode)
    return qs.filter(pk=run_id).first() if run_id else qs.first()


def summary(run: TradingRun) -> dict:
    closed = Position.objects.filter(run=run, status=Position.Status.CLOSED).exclude(realized_pnl=None)
    pnl = [p.realized_pnl for p in closed]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    last = EquitySnapshot.objects.filter(run=run).order_by("-at").first()
    equity = last.equity if last else run.initial_equity
    cash = last.cash if last else run.initial_equity
    realized = sum(pnl, D0)
    peak = max([s for s in EquitySnapshot.objects.filter(run=run).values_list("equity", flat=True)] + [run.initial_equity])
    gross_loss = -sum(losses, D0)
    r_values = [p.r_multiple for p in closed if p.r_multiple is not None]
    return {
        "run_id": run.pk, "mode": run.mode, "currency": run.account_currency,
        "initial_equity": run.initial_equity, "total_capital": equity, "available_capital": cash,
        "invested_capital": equity - cash, "realized_pnl": realized,
        "unrealized_pnl": equity - run.initial_equity - realized,
        "net_return": (equity / run.initial_equity - 1) if run.initial_equity else None,
        "active_positions": Position.objects.filter(run=run, status=Position.Status.OPEN).count(),
        "total_trades": len(pnl), "win_rate": (len(wins) / len(pnl)) if pnl else None,
        "profit_factor": (sum(wins, D0) / gross_loss) if gross_loss > 0 else None,
        "expectancy": (realized / len(pnl)) if pnl else None,
        "expectancy_r": (sum(r_values) / len(r_values)) if r_values else None,
        "max_drawdown": max_drawdown(run),
        "current_drawdown": float(1 - equity / peak) if peak else 0.0,
        "fees": sum(Position.objects.filter(run=run).values_list("fees", flat=True), D0),
    }


def max_drawdown(run: TradingRun) -> float:
    peak, worst = None, 0.0
    for e in EquitySnapshot.objects.filter(run=run).order_by("at").values_list("equity", flat=True):
        peak = e if peak is None or e > peak else peak
        if peak:
            worst = max(worst, float(1 - e / peak))
    return worst


def ledger_balances(run: TradingRun) -> dict[str, Decimal]:
    out = {run.account_currency: run.initial_equity}
    for row in LedgerEntry.objects.filter(run=run).values("asset").annotate(total=Sum("amount")):
        out[row["asset"]] = out.get(row["asset"], D0) + row["total"]
    return out


def equity_series(run: TradingRun) -> list[dict]:
    rows = EquitySnapshot.objects.filter(run=run).order_by("at").values_list("at", "equity", "drawdown")
    return [{"time": int(at.timestamp()), "equity": float(eq), "drawdown": dd} for at, eq, dd in rows]


def period_returns(run: TradingRun, period: str = "day") -> list[dict]:
    """Return per day/week/month from the last equity snapshot of each period."""
    fmt = {"day": "%Y-%m-%d", "week": "%G-W%V", "month": "%Y-%m"}[period]
    last: dict[str, Decimal] = {}
    for at, eq in EquitySnapshot.objects.filter(run=run).order_by("at").values_list("at", "equity"):
        last[at.strftime(fmt)] = eq
    out, prev = [], run.initial_equity
    for key, eq in last.items():
        out.append({"period": key, "equity": eq, "return": float(eq / prev - 1) if prev else None})
        prev = eq
    return out


def funnel(run: TradingRun) -> dict:
    opp = OpportunityRecord.objects.filter(signal__run=run)
    orders = OrderRecord.objects.filter(run=run, purpose="entry")
    closed = Position.objects.filter(run=run, status=Position.Status.CLOSED).exclude(realized_pnl=None)
    return {
        "opportunities_discovered": opp.count(),
        "rejected_by_naia": opp.filter(accepted=False).count(),
        "approved_by_naia": opp.filter(accepted=True).count(),
        "rejected_by_nara": opp.filter(accepted=True, risk__approved=False).count(),
        "approved_by_nara": opp.filter(risk__approved=True).count(),
        "orders_submitted": orders.count(),
        "orders_filled": orders.filter(status="filled").count(),
        "orders_partially_filled": orders.filter(status__in=["partially_filled", "canceled"], filled_qty__gt=0).count(),
        "orders_rejected": orders.filter(status="rejected").count(),
        "positions_closed": closed.count(),
        "net_profitable": closed.filter(realized_pnl__gt=0).count(),
        "net_losing": closed.filter(realized_pnl__lte=0).count(),
    }


def rejection_reasons(run: TradingRun, limit: int = 500) -> dict:
    counts: Counter = Counter()
    for reasons in OpportunityRecord.objects.filter(signal__run=run, accepted=False).values_list("reasons", flat=True)[:limit]:
        counts.update(reasons)
    for rules in OpportunityRecord.objects.filter(signal__run=run, risk__approved=False).values_list("risk__rules", flat=True)[:limit]:
        counts.update(f"nara:{r['rule']}" for r in rules if not r["passed"])
    return dict(counts.most_common())


def by_strategy(run: TradingRun) -> list[dict]:
    rows = []
    for code in Position.objects.filter(run=run).values_list("strategy", flat=True).distinct():
        closed = [p for p in Position.objects.filter(run=run, strategy=code, status=Position.Status.CLOSED)
                  if p.realized_pnl is not None]
        rs = [p.r_multiple for p in closed if p.r_multiple is not None]
        rows.append({"strategy": code, "trades": len(closed),
                     "win_rate": (sum(1 for p in closed if p.realized_pnl > 0) / len(closed)) if closed else None,
                     "pnl": sum((p.realized_pnl for p in closed), D0),
                     "mean_r": (sum(rs) / len(rs)) if rs else None})
    return rows


def system_status() -> dict:
    now = timezone.now()
    state = SystemStateTransition.objects.order_by("-at", "-id").first()
    beats = {h.process: (now - h.at).total_seconds() for h in Heartbeat.objects.all()}
    return {"state": state.to_state if state else "stopped", "since": state.at if state else None,
            "reason": state.reason if state else "", "heartbeat_age_s": beats,
            "open_critical_alerts": Alert.objects.filter(level="critical",
                                                         created_at__gte=now - timedelta(days=1)).count()}


def data_health(symbols: list[str] | None = None) -> list[dict]:
    now = timezone.now()
    out = []
    qs = Instrument.objects.filter(venue="binance")
    if symbols:
        qs = qs.filter(symbol__in=symbols)
    for inst in qs:
        last = Candle.objects.filter(instrument=inst, timeframe="1m").order_by("-open_time").values_list("open_time", flat=True).first()
        out.append({"symbol": inst.symbol, "last_bar": last,
                    "age_min": ((now - last).total_seconds() / 60 - 1) if last else None,
                    "min_notional": inst.min_notional, "tradable": inst.tradable})
    return out


def recent_transitions(limit: int = 20):
    return SystemStateTransition.objects.order_by("-at", "-id")[:limit]


def recent_opportunities(run: TradingRun, limit: int = 50):
    return (OpportunityRecord.objects.filter(signal__run=run).select_related("signal")
            .order_by("-signal__bar_time")[:limit])


def period_report(run: TradingRun, since: datetime) -> dict:
    """Numbers for the daily/weekly/monthly Telegram reports."""
    closed = Position.objects.filter(run=run, status=Position.Status.CLOSED, closed_at__gte=since).exclude(realized_pnl=None)
    pnl = sum(closed.values_list("realized_pnl", flat=True), D0)
    agg = closed.aggregate(n=Count("id"), wins=Count("id", filter=Q(realized_pnl__gt=0)))
    return {"since": since, "trades": agg["n"], "wins": agg["wins"], "pnl": pnl,
            "opened": Position.objects.filter(run=run, opened_at__gte=since).count()}
