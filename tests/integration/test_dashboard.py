from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.trading import reporting as rep
from apps.trading.models import (EquitySnapshot, LedgerEntry, OpportunityRecord, OrderRecord, Position,
                                 RiskAssessmentRecord, SignalRecord, TradingRun)

pytestmark = pytest.mark.django_db
PAGES = ["overview", "crypto", "forex", "naia", "nctpi", "risk", "performance"]


def make_run(mode="paper", pnls=(D("2"), D("-1"), D("3"))):
    run = TradingRun.objects.create(mode=mode, initial_equity=D(1000))
    now = timezone.now()
    eq = D(1000)
    for i, pnl in enumerate(pnls):
        sig = SignalRecord.objects.create(run=run, key=f"k{i}", strategy_version="trend-v1-x", symbol="BTCUSDT",
                                          timeframe="15m", bar_time=now - timedelta(hours=10 - i), entry=100,
                                          stop=98, target=104, max_hold_bars=10, regime="strong_bull")
        opp = OpportunityRecord.objects.create(signal=sig, accepted=True, reasons=[], p=0.5, p_source="hit_rate",
                                               cost_r=0.1, expected_net_r=0.4, score=0.4)
        RiskAssessmentRecord.objects.create(opportunity=opp, approved=True, qty=1, risk_amount=2, notional=100,
                                            rules=[], limits_version="limits-1")
        pos = Position.objects.create(run=run, signal=sig, symbol="BTCUSDT", strategy="trend",
                                      strategy_version="trend-v1-x", qty=1, entry_price=100, stop=98, target=104,
                                      initial_risk=2, opened_at=now - timedelta(hours=9 - i),
                                      status="closed", closed_at=now - timedelta(hours=8 - i),
                                      realized_pnl=pnl, fees=D("0.2"), exit_reason="target" if pnl > 0 else "stop")
        OrderRecord.objects.create(run=run, position=pos, client_order_id=f"{mode}-c{i}", purpose="entry",
                                   symbol="BTCUSDT", side="buy", type="market", qty=1, status="filled", filled_qty=1)
        LedgerEntry.objects.create(run=run, at=now, asset="USDT", amount=pnl, kind="trade", ref="BTCUSDT")
        eq += pnl
        EquitySnapshot.objects.create(run=run, at=now - timedelta(hours=8 - i), equity=eq, cash=eq, open_risk=0,
                                      peak_equity=max(eq, D(1000)), drawdown=0)
    rejected = SignalRecord.objects.create(run=run, key="rej", strategy_version="trend-v1-x", symbol="BTCUSDT",
                                           timeframe="15m", bar_time=now, entry=100, stop=98, target=104,
                                           max_hold_bars=10)
    OpportunityRecord.objects.create(signal=rejected, accepted=False, reasons=["cost_hurdle"], p=None,
                                     p_source="none", cost_r=2.5)
    return run


def test_summary_numbers_tie_to_records():
    run = make_run()
    s = rep.summary(run)
    assert s["realized_pnl"] == D(4) and s["total_trades"] == 3 and s["win_rate"] == pytest.approx(2 / 3)
    assert s["profit_factor"] == D(5) and s["total_capital"] == D(1004)
    assert s["expectancy_r"] == pytest.approx((1 - 0.5 + 1.5) / 3)
    assert rep.ledger_balances(run)["USDT"] == D(1004)
    assert rep.max_drawdown(run) == pytest.approx(1 / 1002)
    f = rep.funnel(run)
    assert f["opportunities_discovered"] == 4 and f["rejected_by_naia"] == 1 and f["orders_filled"] == 3
    assert f["net_profitable"] == 2 and f["net_losing"] == 1
    assert rep.rejection_reasons(run) == {"cost_hurdle": 1}
    assert rep.by_strategy(run)[0]["trades"] == 3
    assert rep.period_report(run, timezone.now() - timedelta(days=1))["pnl"] == D(4)


def test_pages_require_login(client):
    for page in PAGES:
        r = client.get(f"/dashboard/{'' if page == 'overview' else page + '/'}")
        assert r.status_code == 302 and "/admin/login/" in r["Location"]
    assert client.get("/dashboard/api/summary/").status_code == 403


def test_every_page_renders_and_paper_live_stay_separate(client):
    User.objects.create_user("op", password="pw-long-enough-123")
    client.login(username="op", password="pw-long-enough-123")
    make_run("paper")
    live = make_run("live", pnls=(D("-7"),))
    for page in PAGES:
        url = f"/dashboard/{'' if page == 'overview' else page + '/'}"
        assert client.get(url).status_code == 200, page
    paper_page = client.get("/dashboard/").content.decode()
    assert "1,004.00" in paper_page and "PAPER" in paper_page
    live_page = client.get("/dashboard/?mode=live").content.decode()
    assert "993.00" in live_page and "1,004.00" not in live_page
    api = client.get(f"/dashboard/api/summary/?mode=live&run={live.pk}").json()
    assert api["summary"]["mode"] == "live" and api["summary"]["total_trades"] == 1
    assert len(client.get("/dashboard/api/equity/?mode=paper").json()) == 3
    assert len(client.get("/dashboard/api/positions/?status=closed").json()) == 3
    assert client.get("/dashboard/api/funnel/").json()["orders_filled"] == 3


def test_empty_state_renders(client):
    User.objects.create_user("op", password="pw-long-enough-123")
    client.login(username="op", password="pw-long-enough-123")
    for page in PAGES:
        url = f"/dashboard/{'' if page == 'overview' else page + '/'}"
        assert client.get(url).status_code == 200
    assert "No paper run yet" in client.get("/dashboard/").content.decode()
