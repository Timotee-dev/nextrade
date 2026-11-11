"""Telegram control and notifications (spec 16).

Security model:
* Only chat ids listed in TELEGRAM_ALLOWED_CHAT_IDS get any answer. Others are ignored
  (no reply at all) and logged in the audit log.
* Read-only commands answer at once.
* /pause and /shutdown (the safe direction) need a confirmation button press.
* /resume needs the PIN (TELEGRAM_PIN) AND a confirmation, and only works from PAUSED.
  A risk shutdown or emergency can only be cleared with `acknowledge_halt` on the PC.
* Telegram never enforces a risk control by itself: NARA and the watchdog do.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from django.utils import timezone

from apps.ops.models import AuditLog
from apps.trading import reporting as rep
from apps.trading.models import Alert, ControlRequest, Position, TelegramConfirmation
from nextrade_core.data.sessions import ForexCalendar

API = "https://api.telegram.org"
CONFIRM_TTL = timedelta(minutes=2)
SAFE_ACTIONS = {"pause": "paused", "shutdown": "emergency"}
HELP = ("/status /portfolio /crypto /forex /opportunities /openpositions /history /report "
        "/daily /weekly /monthly /risk /pause /resume <PIN> /shutdown")


@dataclass
class Reply:
    text: str
    confirm_token: str | None = None
    extras: dict = field(default_factory=dict)


class TelegramClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=f"{API}/bot{token}", timeout=35, transport=transport)

    def send(self, chat_id: int, text: str, confirm_token: str | None = None) -> bool:
        body = {"chat_id": chat_id, "text": text[:4000]}
        if confirm_token:
            body["reply_markup"] = {"inline_keyboard": [[
                {"text": "Confirm", "callback_data": f"confirm:{confirm_token}"},
                {"text": "Cancel", "callback_data": f"cancel:{confirm_token}"}]]}
        try:
            return self._http.post("/sendMessage", json=body).json().get("ok", False)
        except (httpx.HTTPError, ValueError):
            return False

    def updates(self, offset: int | None, timeout_s: int = 25) -> list[dict]:
        try:
            r = self._http.get("/getUpdates", params={"offset": offset, "timeout": timeout_s})
            return r.json().get("result", [])
        except (httpx.HTTPError, ValueError):
            return []

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._http.post("/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text})
        except httpx.HTTPError:
            pass


def _money(x, places=2):
    return "-" if x is None else f"{float(x):,.{places}f}"


def _pct(x):
    return "-" if x is None else f"{float(x) * 100:.2f}%"


class Bot:
    def __init__(self, allowed_chat_ids: set[int], pin: str, mode: str = "paper") -> None:
        self.allowed, self.pin, self.mode = allowed_chat_ids, pin, mode

    # ------------------------------------------------------------------ entry points
    def handle_message(self, chat_id: int, text: str) -> Reply | None:
        if chat_id not in self.allowed:
            AuditLog.objects.create(actor=f"telegram:{chat_id}", action="unauthorised_message", target="",
                                    details={"text": text[:200]})
            return None
        parts = text.strip().split()
        if not parts:
            return Reply(HELP)
        cmd, args = parts[0].split("@")[0].lower(), parts[1:]
        handler = getattr(self, f"cmd_{cmd.lstrip('/')}", None)
        if handler is None or not cmd.startswith("/"):
            return Reply(f"Unknown command. {HELP}")
        AuditLog.objects.create(actor=f"telegram:{chat_id}", action="command", target=cmd,
                                details={"args": ["***"] if cmd == "/resume" else args})
        return handler(chat_id, args)

    def handle_callback(self, chat_id: int, data: str) -> str:
        if chat_id not in self.allowed:
            return "Not authorised."
        kind, _, token = data.partition(":")
        conf = TelegramConfirmation.objects.filter(token=token, chat_id=chat_id, used_at=None).first()
        if conf is None or conf.expires_at < timezone.now():
            return "This confirmation has expired. Send the command again."
        conf.used_at = timezone.now()
        conf.save(update_fields=["used_at"])
        if kind != "confirm":
            return "Cancelled."
        target = {"pause": "paused", "shutdown": "emergency", "resume": self.mode}[conf.action]
        ControlRequest.objects.create(target_state=target, source="telegram",
                                      reason=f"/{conf.action} confirmed in Telegram by chat {chat_id}")
        AuditLog.objects.create(actor=f"telegram:{chat_id}", action=f"confirmed_{conf.action}", target=target)
        return f"Requested: {conf.action}. The trader applies it within one cycle."

    def _confirm(self, chat_id: int, action: str, text: str) -> Reply:
        token = secrets.token_hex(8)
        TelegramConfirmation.objects.create(token=token, chat_id=chat_id, action=action,
                                            expires_at=timezone.now() + CONFIRM_TTL)
        return Reply(text, confirm_token=token)

    def _run(self):
        return rep.current_run(self.mode)

    # ------------------------------------------------------------------ read-only commands
    def cmd_start(self, chat_id, args):
        return Reply(f"NEXTRADE bot. Commands: {HELP}")

    cmd_help = cmd_start

    def cmd_status(self, chat_id, args):
        st = rep.system_status()
        beats = ", ".join(f"{p} {int(a)}s ago" for p, a in st["heartbeat_age_s"].items()) or "no heartbeats"
        return Reply(f"State: {st['state']} ({st['reason']})\nProcesses: {beats}\n"
                     f"Critical alerts (24h): {st['open_critical_alerts']}")

    def cmd_portfolio(self, chat_id, args):
        run = self._run()
        if run is None:
            return Reply(f"No {self.mode} run yet.")
        s = rep.summary(run)
        return Reply(f"{run.mode.upper()} run {run.pk}\nCapital: {_money(s['total_capital'])} {s['currency']}\n"
                     f"Available: {_money(s['available_capital'])}  Invested: {_money(s['invested_capital'])}\n"
                     f"Realised P&L: {_money(s['realized_pnl'], 4)}  Net return: {_pct(s['net_return'])}\n"
                     f"Open positions: {s['active_positions']}  Closed trades: {s['total_trades']}\n"
                     f"Win rate: {_pct(s['win_rate'])}  Max drawdown: {_pct(s['max_drawdown'])}")

    def cmd_crypto(self, chat_id, args):
        rows = rep.data_health()
        lines = [f"{h['symbol']}: last bar {h['age_min']:.0f} min ago" if h["age_min"] is not None
                 else f"{h['symbol']}: no data" for h in rows]
        return Reply("Crypto data:\n" + ("\n".join(lines) or "no instruments"))

    def cmd_forex(self, chat_id, args):
        cal = ForexCalendar()
        now = timezone.now()
        return Reply(f"Forex market: {'open' if cal.is_open(now) else 'closed'}; sessions: "
                     f"{', '.join(cal.active_sessions(now)) or 'none'}. Forex trading is not enabled yet.")

    def cmd_opportunities(self, chat_id, args):
        run = self._run()
        if run is None:
            return Reply("No run yet.")
        lines = [f"{o.signal.symbol} {o.signal.strategy_version.split('-')[0]}: "
                 f"{'ACCEPTED' if o.accepted else ', '.join(o.reasons)} (E={o.expected_net_r if o.expected_net_r is None else round(o.expected_net_r, 3)})"
                 for o in rep.recent_opportunities(run, 10)]
        return Reply("Latest opportunities:\n" + ("\n".join(lines) or "none"))

    def cmd_openpositions(self, chat_id, args):
        run = self._run()
        qs = Position.objects.filter(run=run, status="open") if run else []
        lines = [f"{p.symbol} {p.strategy}: {p.qty} @ {_money(p.entry_price, 6)}, stop {_money(p.stop, 6)}, "
                 f"target {_money(p.target, 6)}, {p.health}" for p in qs]
        return Reply("Open positions:\n" + ("\n".join(lines) or "none"))

    def cmd_history(self, chat_id, args):
        run = self._run()
        qs = Position.objects.filter(run=run, status="closed")[:10] if run else []
        lines = [f"{p.closed_at:%m-%d %H:%M} {p.symbol} {p.exit_reason}: {_money(p.realized_pnl, 4)}" for p in qs]
        return Reply("Last closed trades:\n" + ("\n".join(lines) or "none"))

    def _period(self, days: int, label: str) -> Reply:
        run = self._run()
        if run is None:
            return Reply("No run yet.")
        r = rep.period_report(run, timezone.now() - timedelta(days=days))
        return Reply(f"{label} ({run.mode}): {r['trades']} closed trades, {r['wins']} winners, "
                     f"P&L {_money(r['pnl'], 4)} {run.account_currency}, {r['opened']} opened")

    def cmd_daily(self, chat_id, args):
        return self._period(1, "Last 24 hours")

    def cmd_weekly(self, chat_id, args):
        return self._period(7, "Last 7 days")

    def cmd_monthly(self, chat_id, args):
        return self._period(30, "Last 30 days")

    def cmd_report(self, chat_id, args):
        return Reply("\n\n".join([self.cmd_portfolio(chat_id, args).text, self.cmd_daily(chat_id, args).text]))

    def cmd_risk(self, chat_id, args):
        run = self._run()
        st = rep.system_status()
        if run is None:
            return Reply(f"State: {st['state']}. No run yet.")
        s = rep.summary(run)
        last = run.equity.order_by("-at").first()
        return Reply(f"State: {st['state']}\nCurrent drawdown: {_pct(s['current_drawdown'])}  "
                     f"Max: {_pct(s['max_drawdown'])}\nOpen risk: {_money(last.open_risk if last else 0, 4)}")

    # ------------------------------------------------------------------ state-changing commands
    def cmd_pause(self, chat_id, args):
        return self._confirm(chat_id, "pause", "Pause new entries? Open positions keep their stops.")

    def cmd_shutdown(self, chat_id, args):
        return self._confirm(chat_id, "shutdown", "EMERGENCY shutdown: block entries and halt until you "
                                                  "acknowledge on the PC. Confirm?")

    def cmd_resume(self, chat_id, args):
        if not args or not self.pin or not hmac.compare_digest(args[0], self.pin):
            AuditLog.objects.create(actor=f"telegram:{chat_id}", action="resume_bad_pin", target="")
            return Reply("Resume refused: PIN missing or wrong.")
        state = rep.system_status()["state"]
        if state != "paused":
            return Reply(f"Resume only works from PAUSED (now: {state}). A shutdown must be acknowledged "
                         "on the PC with acknowledge_halt.")
        return self._confirm(chat_id, "resume", "Resume trading (new entries allowed again)?")


def deliver_outbox(client: TelegramClient, chat_ids: set[int], min_level: str = "info", max_attempts: int = 5) -> int:
    """Send undelivered alerts. Returns how many were delivered."""
    order = {"info": 0, "warning": 1, "critical": 2}
    sent = 0
    for alert in Alert.objects.filter(delivered_at=None, attempts__lt=max_attempts).order_by("created_at")[:50]:
        if order[alert.level] < order[min_level]:
            alert.delivered_at = timezone.now()   # below threshold: mark handled, do not send
            alert.save(update_fields=["delivered_at"])
            continue
        icon = {"info": "", "warning": "WARNING: ", "critical": "CRITICAL: "}[alert.level]
        ok = all(client.send(cid, f"{icon}{alert.message}") for cid in chat_ids) if chat_ids else False
        alert.attempts += 1
        if ok:
            alert.delivered_at = timezone.now()
            sent += 1
        alert.save(update_fields=["attempts", "delivered_at"])
    return sent
