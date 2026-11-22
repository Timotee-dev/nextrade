"""Live monitoring numbers for the paper scalper, read straight from the database.

Everything here is measured from recorded positions, equity snapshots and the trader's heartbeat.
No figure is estimated except where labelled `est.` (spread and slippage are inside the fill prices; the
estimate shows how much of the gross figure they took).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from apps.market.models import QuoteSample
from apps.trading import reporting as rep
from apps.trading.models import EquitySnapshot, Heartbeat, Position, TradingRun
from nextrade_core.costs.model import cost_model_for
from nextrade_core.scalping.profit import OpenTrade, net_if_closed_now

D0 = Decimal(0)


def max_simultaneous(spans: list[tuple[datetime, datetime | None]], now: datetime) -> int:
    events = []
    for a, b in spans:
        events += [(a, 1), (b or now, -1)]
    cur = peak = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):      # closes before opens at the same instant
        cur += d
        peak = max(peak, cur)
    return peak


def collect(run: TradingRun, now: datetime, venue: str = "bybit") -> dict:
    costs = cost_model_for(venue)
    rows = list(Position.objects.filter(run=run).order_by("opened_at"))
    closed = [p for p in rows if p.status == "closed" and p.realized_pnl is not None]
    opened = [p for p in rows if p.status == "open"]
    nets = [p.realized_pnl for p in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    fees = sum((p.fees for p in closed), D0)
    gross = sum(nets, D0) + fees
    friction = sum(((Decimal(str(costs.spread(p.symbol))) + 2 * Decimal(str(costs.slippage_bps)) / 10_000)
                    * p.qty * p.entry_price for p in closed), D0)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = [p for p in closed if p.closed_at and p.closed_at >= day0]
    holds = [(p.closed_at - p.opened_at).total_seconds() / 60 for p in closed if p.closed_at]
    first = rows[0].opened_at if rows else None
    hours = max((now - first).total_seconds() / 3600, 1e-9) if first else 0.0
    gl = -sum(losses, D0)

    open_view, unreal = [], D0
    for p in opened:
        q = QuoteSample.objects.filter(instrument__venue=venue, instrument__symbol=p.symbol).order_by("-ts").first()
        net = None
        if q is not None:
            mid = float((q.bid + q.ask) / 2)
            net = Decimal(str(net_if_closed_now(OpenTrade(p.symbol, 1, float(p.entry_price), float(p.qty),
                                                          float(p.entry_fee)), mid, costs)))
            unreal += net
        open_view.append({"symbol": p.symbol, "strategy": p.strategy, "entry": p.entry_price, "qty": p.qty,
                          "held_min": int((now - p.opened_at).total_seconds() // 60), "net_if_closed_now": net,
                          "stop": p.stop, "target": p.target})

    hb = Heartbeat.objects.filter(process="trader").first()
    detail = hb.detail if hb else {}
    reasons: dict[str, int] = {}
    for p in closed:
        reasons[p.exit_reason] = reasons.get(p.exit_reason, 0) + 1
    last = EquitySnapshot.objects.filter(run=run).order_by("-at").first()
    return {
        "run_id": run.pk, "now": now, "validated": bool((run.config or {}).get("validated", False)),
        "capital_start_usdt": run.initial_equity, "equity_usdt": last.equity if last else run.initial_equity,
        "capital_ngn": (run.config or {}).get("capital_ngn"),
        "markets_scanned_last_cycle": (detail.get("stats") or {}).get("markets_last_cycle"),
        "markets_scanned_total": (detail.get("stats") or {}).get("markets_scanned"),
        "scans": (detail.get("stats") or {}).get("scans"), "state": detail.get("state"),
        "heartbeat_age_s": int((now - hb.at).total_seconds()) if hb else None,
        "funnel_this_session": detail.get("funnel") or {},
        "opportunities_detected": (detail.get("funnel") or {}).get("signals"),
        "open_positions": open_view, "open_count": len(opened),
        "closed_count": len(closed), "winners": len(wins), "losers": len(losses),
        "gross_pnl": gross, "fees": fees, "spread_slippage_est": friction, "net_pnl": sum(nets, D0),
        "unrealised_net": unreal, "daily_net_pnl": sum((p.realized_pnl for p in today), D0), "closed_today": len(today),
        "win_rate": (len(wins) / len(nets)) if nets else None,
        "profit_factor": (sum(wins, D0) / gl) if gl > 0 else None,
        "max_drawdown": rep.max_drawdown(run), "avg_hold_min": (sum(holds) / len(holds)) if holds else None,
        "trades_per_hour": (len(closed) / hours) if first else None,
        "trades_per_day": (len(closed) / hours * 24) if first else None,
        "max_simultaneous": max_simultaneous([(p.opened_at, p.closed_at) for p in rows], now),
        "exit_reasons": reasons,
    }


def _m(x, nd=4):
    return "n/a" if x is None else f"{float(x):+.{nd}f}"


def render(s: dict) -> str:
    f = s["funnel_this_session"]
    lines = [
        f"NEXTRADE PAPER SCALPER  run {s['run_id']}  {s['now']:%Y-%m-%d %H:%M:%S} UTC  "
        f"{'VALIDATED' if s['validated'] else 'UNVALIDATED EXPLORATORY TRIAL'}  (simulated money, no real orders)",
        f"  engine state {s['state']}, heartbeat {s['heartbeat_age_s']}s ago" if s["heartbeat_age_s"] is not None
        else "  engine: no heartbeat yet (the trader has not started)",
        f"  scanned: {s['markets_scanned_last_cycle']} markets last cycle ({s['markets_scanned_total']} market-scans over {s['scans']} cycles this session); "
        f"opportunities detected {s['opportunities_detected']}; short signals skipped (spot) {f.get('skipped_short_unsupported', 0)}; "
        f"rejected: net-target {f.get('rejected_by_net_target', 0)}, re-entry {f.get('rejected_by_reentry', 0)}, "
        f"duplicate {f.get('duplicate_signal_blocked', 0)}, AI {f.get('rejected_by_naia', 0)}, risk {f.get('rejected_by_nara', 0)}",
        f"  capital: start {float(s['capital_start_usdt']):.2f} USDT (NGN {s['capital_ngn']}), equity now {float(s['equity_usdt']):.4f} USDT",
        f"  positions: {s['open_count']} open, {s['closed_count']} closed ({s['winners']} net winners, {s['losers']} net losers), "
        f"max simultaneous {s['max_simultaneous']}",
        f"  P&L (USDT): gross {_m(s['gross_pnl'])}, fees {float(s['fees']):.4f}, spread+slippage in fills est. {float(s['spread_slippage_est']):.4f}, "
        f"NET {_m(s['net_pnl'])}; unrealised net {_m(s['unrealised_net'])}; today {_m(s['daily_net_pnl'])} ({s['closed_today']} trades)",
        f"  win rate {'n/a' if s['win_rate'] is None else format(s['win_rate'], '.1%')}, profit factor "
        f"{'n/a' if s['profit_factor'] is None else format(float(s['profit_factor']), '.2f')}, max drawdown {s['max_drawdown']:.2%}, "
        f"avg hold {'n/a' if s['avg_hold_min'] is None else format(s['avg_hold_min'], '.1f') + ' min'}, trades/hour "
        f"{'n/a' if s['trades_per_hour'] is None else format(s['trades_per_hour'], '.2f')}, trades/day "
        f"{'n/a' if s['trades_per_day'] is None else format(s['trades_per_day'], '.1f')}",
        f"  exits: {s['exit_reasons'] or 'none yet'}",
    ]
    for p in s["open_positions"]:
        lines.append(f"    OPEN {p['symbol']:<12} {p['strategy']:<15} entry {float(p['entry']):.6g} qty {float(p['qty']):.6g} held "
                     f"{p['held_min']} min  net-if-closed-now {_m(p['net_if_closed_now'])}")
    if s["closed_count"] < 30:
        lines.append(f"  NOTE: only {s['closed_count']} closed trades. Far too few to say whether this makes money after costs.")
    return "\n".join(lines)
