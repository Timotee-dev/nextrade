"""Live-market PAPER scalper: safety, supervision, launcher, monitoring. Simulated feeds only."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from adapters.bybit.client import BybitClient
from adapters.gate.client import GateClient
from adapters.paper.broker import PaperBroker
from apps.market.models import Instrument, QuoteSample
from apps.market.store import CandleStore
from apps.trading import paper_status, supervisor
from apps.trading.management.commands import run_paper_scalper
from apps.trading.models import Alert, Heartbeat, Position, TradingRun
from nextrade_core.data.candles import Candle, Timeframe
from nextrade_core.scalping import approvals, paper_safety
from tests.helpers import random_walk_frame

pytestmark = pytest.mark.django_db
SYMS = ("AAAUSDT", "BBBUSDT", "CCCUSDT")


def seed(symbol: str, seed_: int = 1, days: int = 3, venue: str = "bybit") -> Instrument:
    inst = Instrument.objects.create(venue=venue, symbol=symbol, base=symbol[:-4], quote="USDT", asset_class="crypto",
                                     tick_size=Decimal("0.0001"), step_size=Decimal("0.01"), min_qty=Decimal("0.01"),
                                     min_notional=Decimal("1"))
    end = Timeframe.M1.floor(datetime.now(UTC)) - timedelta(minutes=1)
    n = days * 1440
    df = random_walk_frame(n, seed=seed_, start=end - timedelta(minutes=n - 1))
    q = lambda x: Decimal(f"{x:.4f}")  # noqa: E731
    CandleStore({symbol: inst}).save_candles(
        [Candle(symbol, Timeframe.M1, t.to_pydatetime(), q(r.open), q(r.high), q(r.low), q(r.close),
                q(r.volume), q(r.quote_volume), int(r.trades)) for t, r in df.iterrows()], "archive")
    last = q(df.close.iloc[-1])
    QuoteSample.objects.create(instrument=inst, ts=timezone.now(), bid=last * Decimal("0.9999"),
                               ask=last * Decimal("1.0001"), bid_qty=1, ask_qty=1)
    return inst


def feed(request: httpx.Request) -> httpx.Response:
    """A simulated Bybit public API."""
    p = request.url.path
    if p.endswith("/market/tickers"):
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [
            {"symbol": s, "bid1Price": "100", "ask1Price": "100.05", "turnover24h": "9"} for s in SYMS]}})
    if p.endswith("/market/kline"):
        q = dict(request.url.params)
        rows = [[str(t), "100", "100.2", "99.9", "100.1", "5", "500"] for t in range(int(q["start"]), int(q["end"]) + 1, 60_000)]
        return httpx.Response(200, json={"retCode": 0, "result": {"list": rows[::-1]}})
    return httpx.Response(404, json={})


# ---------------------------------------------------------------- safety: no real order can leave the machine
def test_write_guard_blocks_exchange_writes_but_not_reads_or_telegram():
    paper_safety.install_write_guard()
    seen = []
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append((r.method, str(r.url))) or httpx.Response(200, json={})))
    assert ok.get("https://api.bybit.com/v5/market/tickers").status_code == 200
    assert ok.post("https://api.telegram.org/botX/sendMessage", json={}).status_code == 200
    for host in ("api.bybit.com/v5/order/create", "api.binance.com/api/v3/order", "api.gateio.ws/api/v4/spot/orders"):
        with pytest.raises(paper_safety.LiveOrderBlocked):
            ok.post(f"https://{host}", json={"side": "Buy"})
    with pytest.raises(paper_safety.LiveOrderBlocked):
        ok.delete("https://api.bybit.com/v5/order/cancel")
    assert [m for m, _ in seen] == ["GET", "POST"]            # the blocked requests never reached the transport


def test_data_clients_have_no_order_methods_and_checks_pass_for_paper(tmp_path):
    assert paper_safety.order_methods(BybitClient) == [] and paper_safety.order_methods(GateClient) == []
    paper_safety.install_write_guard()
    checks = paper_safety.safety_checks(tmp_path, {}, broker=PaperBroker())
    assert paper_safety.all_ok(checks), checks


def test_safety_refuses_when_live_flag_is_set_or_live_approval_exists_or_broker_is_not_paper(tmp_path):
    paper_safety.install_write_guard()
    assert not paper_safety.all_ok(paper_safety.safety_checks(tmp_path, {"NEXTRADE_LIVE_ENABLED": "yes"}))
    approvals.save(tmp_path, {"scalp_trend": approvals.Approval("scalp_trend", True, True, "e", "t")})
    assert not paper_safety.all_ok(paper_safety.safety_checks(tmp_path, {}))
    approvals.save(tmp_path, {})
    assert not paper_safety.all_ok(paper_safety.safety_checks(tmp_path, {}, broker=object()))
    # exchange credentials are reported, never used
    detail = {n: d for n, _, d in paper_safety.safety_checks(tmp_path, {"BYBIT_API_KEY": "x"})}
    assert any("WARNING" in d for d in detail.values())


# ---------------------------------------------------------------- authorisation: exploratory, paper only
def test_trial_authorisation_is_paper_only_labelled_and_leaves_gate_approvals_alone(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    approvals.save(tmp_path, {"scalp_trend": approvals.Approval("scalp_trend", True, False, "gate evidence", "t")})
    call_command("authorise_paper_trial")
    ap = approvals.load(tmp_path)
    assert set(ap) >= {"scalp_momentum", "scalp_trend", "scalp_meanrev", "scalp_breakout", "scalp_vol", "scalp_short"}
    assert not ap["scalp_trend"].trial and ap["scalp_trend"].evidence == "gate evidence"
    assert all(a.paper and not a.live for a in ap.values())
    assert ap["scalp_momentum"].trial and "not a decision-gate pass" in ap["scalp_momentum"].evidence
    assert ap["scalp_vol"].timeframe == "15m" and ap["scalp_momentum"].timeframe == "5m"
    call_command("authorise_paper_trial", revoke=True)
    after = approvals.load(tmp_path)
    assert "scalp_momentum" not in after and "scalp_trend" in after


def test_live_mode_stays_impossible_even_with_trial_approvals(settings, tmp_path):
    settings.BASE_DIR = tmp_path
    call_command("authorise_paper_trial")
    with pytest.raises(PermissionError):
        approvals.require_mode(tmp_path, approvals.Mode.LIVE, {"NEXTRADE_LIVE_ENABLED": "yes"})   # no strategy approved for live
    with pytest.raises(CommandError, match="run_live"):
        call_command("run_trader", mode="live")


# ---------------------------------------------------------------- the trader on a simulated live feed (database store)
@pytest.mark.django_db(transaction=True)
def test_run_trader_needs_approval_then_trades_the_28_style_universe_with_naira_capital(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    for k, s in enumerate(SYMS):
        seed(s, k + 1)
    seed("USD1USDT", 9)                                    # a stablecoin that must be excluded
    with pytest.raises(CommandError, match="authorise_paper_trial"):
        call_command("run_trader", venue="bybit", strategies="scalp_momentum", timeframe="5m", max_steps=1)
    call_command("authorise_paper_trial")
    capsys.readouterr()
    call_command("run_trader", venue="bybit", strategies="scalp_momentum,scalp_trend,scalp_meanrev,scalp_breakout,scalp_vol,scalp_short",
                 timeframe="5m", poll=0, max_steps=3)
    out = capsys.readouterr().out
    run = TradingRun.objects.get()
    assert run.initial_equity == Decimal("27.03") and run.config["capital_ngn"] == "40000" and run.config["validated"] is False
    assert sorted(run.config["symbols"]) == sorted(SYMS)                           # USD1USDT excluded
    assert "EXPLORATORY PAPER TRIAL" in out and "scalp_vol is designed for 15m" in out
    assert "Markets: 3" in out and "[FAIL]" not in out and "max 5 positions" in out
    hb = Heartbeat.objects.get(process="trader")
    assert hb.detail["stats"]["markets_scanned"] >= 0 and "funnel" in hb.detail
    call_command("run_trader", venue="bybit", strategies="scalp_momentum", timeframe="5m", poll=0, max_steps=1)
    assert TradingRun.objects.count() == 1 and "Resuming paper run" in capsys.readouterr().out      # restart resumes, no new account


@pytest.mark.django_db(transaction=True)
def test_run_trader_stops_cleanly_on_the_stop_file(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    seed("AAAUSDT")
    call_command("authorise_paper_trial")
    stop = tmp_path / "STOP"
    stop.write_text("x")
    call_command("run_trader", venue="bybit", strategies="scalp_momentum", timeframe="5m", poll=0, stop_file=str(stop))
    assert "Stop file found" in capsys.readouterr().out


# ---------------------------------------------------------------- supervisor
class FakeProc:
    def __init__(self, code_after=None):
        self.code, self.terminated = None, False
        self.code_after = code_after

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15

    def kill(self):
        self.code = -9


def test_supervisor_restarts_a_crashed_process_with_backoff_and_reports_it():
    t = [0.0]
    spawned, events = [], []

    def spawn(spec):
        p = FakeProc()
        spawned.append((spec.name, p))
        return p
    sup = supervisor.Supervisor([supervisor.ChildSpec("trader", ["x"])], spawn, clock=lambda: t[0], on_event=events.append)
    sup.tick()
    assert [n for n, _ in spawned] == ["trader"]
    spawned[0][1].code = 1                                  # crash after 0 s
    t[0] = 1
    sup.tick()
    assert "restarting in 5s" in events[-1] and len(spawned) == 1
    t[0] = 4
    sup.tick()
    assert len(spawned) == 1                                # still waiting
    t[0] = 7
    sup.tick()
    assert len(spawned) == 2
    spawned[1][1].code = 1                                  # second quick crash: longer delay
    t[0] = 8
    sup.tick()
    assert "restarting in 10s" in events[-1]
    t[0] = 20
    sup.tick()
    assert len(spawned) == 3
    t[0] = 20 + supervisor.STABLE_RUN_S + 1                 # ran stably: the penalty resets
    sup.tick()
    assert sup.children["trader"].fast_failures == 0


def test_optional_process_that_keeps_failing_is_disabled_not_restarted_forever():
    t, spawned = [0.0], []
    sup = supervisor.Supervisor([supervisor.ChildSpec("telegram", ["x"], optional=True)],
                                lambda s: spawned.append(FakeProc()) or spawned[-1], clock=lambda: t[0], on_event=lambda m: None)
    for _ in range(6):                                       # each run lasts 1 s (a fast failure), then waits out the backoff
        sup.tick()
        if spawned and spawned[-1].code is None:
            t[0] += 1
            spawned[-1].code = 2
        sup.tick()
        t[0] += 130
    assert sup.children["telegram"].disabled and len(spawned) == 3


def test_shutdown_terminates_what_does_not_exit_by_itself():
    p = FakeProc()
    sup = supervisor.Supervisor([supervisor.ChildSpec("x", ["x"])], lambda s: p, clock=iter(range(0, 1000)).__next__,
                                on_event=lambda m: None)
    sup.tick()
    sup.shutdown(grace_s=3, sleep=lambda s: None)
    assert p.terminated


def test_only_one_supervisor_at_a_time(tmp_path):
    supervisor.acquire_lock(tmp_path, now=1000.0)
    assert supervisor.is_running(tmp_path, now=1005.0)
    with pytest.raises(RuntimeError, match="already running"):
        supervisor.acquire_lock(tmp_path, now=1005.0)
    supervisor.acquire_lock(tmp_path, now=1000.0 + supervisor.LOCK_STALE_S + 1)       # stale lock: taken over
    supervisor.release_lock(tmp_path)
    assert not supervisor.is_running(tmp_path)


# ---------------------------------------------------------------- launcher
def _patch_client(monkeypatch, handler=feed):
    monkeypatch.setattr(run_paper_scalper, "BybitClient",
                        lambda: BybitClient(transport=httpx.MockTransport(handler), min_interval_s=0, sleep=lambda s: None))


def test_launcher_check_runs_preflight_and_starts_nothing(settings, tmp_path, monkeypatch, capsys):
    settings.BASE_DIR = tmp_path
    for k, s in enumerate(SYMS):
        seed(s, k + 1)
    _patch_client(monkeypatch)
    with pytest.raises(CommandError, match="Pre-flight failed"):                    # nothing authorised yet
        call_command("run_paper_scalper", check=True)
    assert "authorise_paper_trial" in capsys.readouterr().out
    call_command("authorise_paper_trial")
    call_command("run_paper_scalper", check=True)
    out = capsys.readouterr().out
    assert "No real orders can be sent" in out and "live feed reachable" in out and "live bid/ask for 3 of 3" in out
    assert "scalp_vol (designed for 15m" in out and "have NOT passed validation" in out and "Nothing started" in out
    assert not supervisor.is_running(tmp_path)


def test_launcher_refuses_when_the_live_flag_is_on(settings, tmp_path, monkeypatch):
    settings.BASE_DIR = tmp_path
    monkeypatch.setenv("NEXTRADE_LIVE_ENABLED", "yes")
    seed("AAAUSDT")
    _patch_client(monkeypatch)
    call_command("authorise_paper_trial")
    with pytest.raises(CommandError, match="Pre-flight failed"):
        call_command("run_paper_scalper", check=True)


def test_launcher_starts_supervises_and_stops_cleanly(settings, tmp_path, monkeypatch, capsys):
    settings.BASE_DIR = tmp_path
    for k, s in enumerate(SYMS):
        seed(s, k + 1)
    _patch_client(monkeypatch)
    call_command("authorise_paper_trial")
    started = []; monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)

    class P(FakeProc):
        def poll(self):
            return 0 if supervisor.stop_path(tmp_path).exists() else None

    def spawner(self, base):
        return lambda spec: started.append(spec) or P()
    monkeypatch.setattr(run_paper_scalper.Command, "_spawner", spawner)
    monkeypatch.setattr(run_paper_scalper.time, "sleep", lambda s: None)
    Alert.objects.create(level="info", kind="old", message="stale")
    call_command("run_paper_scalper", max_seconds=0.001, status_every=0)
    out = capsys.readouterr().out
    names = [s.name for s in started]
    assert names[:2] == ["recorder", "trader"] and "telegram" not in names             # no Telegram configured in tests
    trader = next(s for s in started if s.name == "trader").argv
    assert "scalp_momentum" in trader[trader.index("--strategies") + 1] and "scalp_vol" not in trader[trader.index("--strategies") + 1]
    assert "--capital-ngn" in trader and "40000" in trader
    assert "cleared 1 old undelivered alerts" in out and "Stopped." in out
    assert not supervisor.lock_path(tmp_path).exists() and not supervisor.stop_path(tmp_path).exists()


def test_stop_command_when_nothing_runs(settings, tmp_path, capsys):
    settings.BASE_DIR = tmp_path
    call_command("stop_paper_scalper")
    assert "not running" in capsys.readouterr().out


# ---------------------------------------------------------------- monitoring
def test_paper_status_reports_the_requested_numbers_from_recorded_positions(capsys):
    inst = seed("AAAUSDT")
    now = datetime.now(UTC)
    run = TradingRun.objects.create(mode="paper", initial_equity=Decimal("27.03"),
                                    config={"capital_ngn": "40000", "validated": False})
    t0 = now - timedelta(hours=2)

    def pos(sym, opened, closed, pnl, fees, reason="net_profit_trail", status="closed"):
        return Position.objects.create(run=run, symbol=sym, strategy="scalp_trend", strategy_version="v", qty=Decimal(10),
                                       entry_price=Decimal(1), stop=Decimal("0.99"), target=Decimal("1.02"),
                                       initial_risk=Decimal("0.1"), entry_fee=Decimal("0.01"), status=status, opened_at=opened,
                                       closed_at=closed, realized_pnl=pnl, fees=fees, exit_reason=reason)
    pos("AAAUSDT", t0, t0 + timedelta(minutes=10), Decimal("0.30"), Decimal("0.02"))                    # win
    pos("BBBUSDT", t0 + timedelta(minutes=5), t0 + timedelta(minutes=25), Decimal("-0.10"), Decimal("0.02"), "stop")   # loss
    pos("AAAUSDT", now - timedelta(minutes=3), None, None, Decimal(0), "", "open")
    Heartbeat.objects.create(process="trader", at=now, detail={"state": "paper", "stats": {"markets_scanned": 84, "markets_last_cycle": 28, "scans": 3},
                                                              "funnel": {"signals": 12, "skipped_short_unsupported": 7,
                                                                         "rejected_by_net_target": 4}})
    s = paper_status.collect(run, now)
    assert s["closed_count"] == 2 and s["winners"] == 1 and s["losers"] == 1 and s["open_count"] == 1
    assert s["net_pnl"] == Decimal("0.20") and s["fees"] == Decimal("0.04") and s["gross_pnl"] == Decimal("0.24")
    assert s["win_rate"] == 0.5 and s["profit_factor"] == 3 and s["max_simultaneous"] == 2
    assert abs(s["avg_hold_min"] - 15) < 1e-6 and s["trades_per_day"] > 0 and s["markets_scanned_last_cycle"] == 28
    assert s["open_positions"][0]["net_if_closed_now"] is not None
    text = paper_status.render(s)
    for needle in ("UNVALIDATED", "scanned: 28 markets last cycle (84 market-scans over 3 cycles", "opportunities detected 12", "short signals skipped (spot) 7",
                   "net-target 4", "NET +0.2000", "win rate 50.0%", "profit factor 3.00", "max drawdown", "avg hold 15.0 min",
                   "trades/hour", "trades/day", "max simultaneous", "Far too few"):
        assert needle in text, needle
    call_command("paper_status")
    assert "NEXTRADE PAPER SCALPER" in capsys.readouterr().out
    assert inst.symbol == "AAAUSDT" and json.dumps(s["exit_reasons"])


def test_paper_status_without_a_run_says_how_to_start(capsys):
    with pytest.raises(CommandError, match="run_paper_scalper"):
        call_command("paper_status")


def test_max_simultaneous_counts_overlap():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    spans = [(t, t + timedelta(minutes=10)), (t + timedelta(minutes=5), t + timedelta(minutes=15)),
             (t + timedelta(minutes=10), t + timedelta(minutes=20))]
    assert paper_status.max_simultaneous(spans, t + timedelta(hours=1)) == 2
