"""Run the Telegram bot: answers commands and delivers alerts.

Needs in .env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS (comma-separated), TELEGRAM_PIN.
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

import time

from apps.notify.telegram import TOKEN_FORMAT, Bot, TelegramAuthError, TelegramClient, deliver_outbox


def settings_from_env():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chats = {int(c) for c in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
    pin = os.getenv("TELEGRAM_PIN", "")
    return token, chats, pin


class Command(BaseCommand):
    help = "Telegram bot: commands + alert delivery."

    def add_arguments(self, parser):
        parser.add_argument("--mode", default="paper", choices=["paper", "live"])
        parser.add_argument("--min-level", default="info", choices=["info", "warning", "critical"])
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **o):
        token, chats, pin = settings_from_env()
        if not token or not chats:
            raise CommandError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS in .env")
        if len(pin) < 6:
            raise CommandError("Set TELEGRAM_PIN (at least 6 characters) in .env")
        if not TOKEN_FORMAT.match(token):
            raise CommandError(
                "TELEGRAM_BOT_TOKEN in .env is not a full bot token. It must look like 1234567890:AAH... "
                "(digits, a colon, then a long secret string), exactly as @BotFather gave it, with no quotes or spaces. "
                f"Yours has {len(token)} characters and {'a colon' if ':' in token else 'no colon'}.")
        client, bot = TelegramClient(token), Bot(chats, pin, o["mode"])
        offset = None
        self.stdout.write("Telegram bot running. Ctrl+C to stop.")
        quiet = 0
        while True:
            started = time.monotonic()
            try:
                updates = client.updates(offset, timeout_s=0 if o["once"] else 25)
            except TelegramAuthError as exc:
                raise CommandError(f"{exc} Check TELEGRAM_BOT_TOKEN in .env (copy it again from @BotFather).") from exc
            if not o["once"] and not updates and time.monotonic() - started < 1:
                quiet = min(quiet + 1, 6)          # answered instantly with nothing: network trouble, back off
                time.sleep(min(60, 2 ** quiet))
            else:
                quiet = 0
            for upd in updates:
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    text = bot.handle_callback(cq["message"]["chat"]["id"], cq.get("data", ""))
                    client.answer_callback(cq["id"], text)
                    client.send(cq["message"]["chat"]["id"], text)
                elif "message" in upd and "text" in upd["message"]:
                    m = upd["message"]
                    reply = bot.handle_message(m["chat"]["id"], m["text"])
                    if reply:
                        client.send(m["chat"]["id"], reply.text, reply.confirm_token)
            deliver_outbox(client, chats, o["min_level"])
            if o["once"]:
                return
