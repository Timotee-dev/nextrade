"""Dashboard pages (spec 17). Login required. Paper and live are never mixed: every
page shows ONE run, picked with ?mode=paper|live&run=<id>."""

from __future__ import annotations

from datetime import datetime, timezone as dt_tz

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.research.models import ExperimentRun
from apps.trading import reporting as rep
from apps.trading.models import Alert, Position, TradingRun
from nextrade_core.data.sessions import ForexCalendar
from nextrade_core.nara.limits import RiskLimits


def _ctx(request, page: str) -> dict:
    mode = request.GET.get("mode", "paper")
    if mode not in ("paper", "live"):
        mode = "paper"
    run_id = request.GET.get("run")
    run = rep.current_run(mode, int(run_id) if run_id and run_id.isdigit() else None)
    return {"page": page, "mode": mode, "run": run, "runs": TradingRun.objects.filter(mode=mode)[:20],
            "status": rep.system_status()}


@login_required
def overview(request):
    ctx = _ctx(request, "overview")
    if ctx["run"]:
        ctx["summary"] = rep.summary(ctx["run"])
        ctx["balances"] = rep.ledger_balances(ctx["run"])
    ctx["alerts"] = Alert.objects.all()[:10]
    return render(request, "dashboard/overview.html", ctx)


@login_required
def crypto(request):
    ctx = _ctx(request, "crypto")
    ctx["health"] = rep.data_health()
    if ctx["run"]:
        ctx["open_positions"] = Position.objects.filter(run=ctx["run"], status="open")
        ctx["closed_positions"] = Position.objects.filter(run=ctx["run"], status="closed")[:50]
        ctx["opportunities"] = rep.recent_opportunities(ctx["run"], 30)
    return render(request, "dashboard/crypto.html", ctx)


@login_required
def forex(request):
    ctx = _ctx(request, "forex")
    now = datetime.now(dt_tz.utc)
    cal = ForexCalendar()
    ctx.update(forex_open=cal.is_open(now), sessions=cal.active_sessions(now), now=now)
    return render(request, "dashboard/forex.html", ctx)


@login_required
def naia(request):
    ctx = _ctx(request, "naia")
    if ctx["run"]:
        ctx["opportunities"] = rep.recent_opportunities(ctx["run"], 100)
        ctx["reasons"] = rep.rejection_reasons(ctx["run"])
    ctx["experiments"] = ExperimentRun.objects.all()[:10]
    return render(request, "dashboard/naia.html", ctx)


@login_required
def nctpi(request):
    ctx = _ctx(request, "nctpi")
    if ctx["run"]:
        ctx["funnel"] = rep.funnel(ctx["run"])
        ctx["summary"] = rep.summary(ctx["run"])
    return render(request, "dashboard/nctpi.html", ctx)


@login_required
def risk(request):
    ctx = _ctx(request, "risk")
    ctx["limits"] = RiskLimits().as_dict()
    ctx["transitions"] = rep.recent_transitions(20)
    if ctx["run"]:
        ctx["summary"] = rep.summary(ctx["run"])
        last = ctx["run"].equity.order_by("-at").first()
        ctx["open_risk"] = last.open_risk if last else 0
    return render(request, "dashboard/risk.html", ctx)


@login_required
def performance(request):
    ctx = _ctx(request, "performance")
    if ctx["run"]:
        ctx["summary"] = rep.summary(ctx["run"])
        ctx["daily"] = rep.period_returns(ctx["run"], "day")[-30:]
        ctx["weekly"] = rep.period_returns(ctx["run"], "week")[-12:]
        ctx["monthly"] = rep.period_returns(ctx["run"], "month")
        ctx["strategies"] = rep.by_strategy(ctx["run"])
    return render(request, "dashboard/performance.html", ctx)
