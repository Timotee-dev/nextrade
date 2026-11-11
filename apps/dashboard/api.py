"""JSON API (Django REST Framework) for the dashboard charts and external tools."""

from __future__ import annotations

from rest_framework import permissions, serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.trading import reporting as rep
from apps.trading.models import Position


class PositionSerializer(serializers.ModelSerializer):
    r_multiple = serializers.FloatField(read_only=True)

    class Meta:
        model = Position
        fields = ["id", "symbol", "strategy", "qty", "entry_price", "stop", "target", "status", "health",
                  "opened_at", "closed_at", "exit_price", "exit_reason", "realized_pnl", "fees", "r_multiple"]


def _run(request):
    mode = request.query_params.get("mode", "paper")
    run_id = request.query_params.get("run")
    return rep.current_run(mode if mode in ("paper", "live") else "paper",
                           int(run_id) if run_id and run_id.isdigit() else None)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def summary(request):
    run = _run(request)
    return Response({"status": rep.system_status(), "summary": rep.summary(run) if run else None})


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def equity(request):
    run = _run(request)
    return Response(rep.equity_series(run) if run else [])


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def positions(request):
    run = _run(request)
    if run is None:
        return Response([])
    qs = Position.objects.filter(run=run)
    if request.query_params.get("status") in ("open", "closed"):
        qs = qs.filter(status=request.query_params["status"])
    return Response(PositionSerializer(qs[:500], many=True).data)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def funnel(request):
    run = _run(request)
    return Response(rep.funnel(run) if run else {})
