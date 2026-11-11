"""Run the Telegram bot: answers commands and delivers alerts.

Needs in .env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS (comma-separated), TELEGRAM_PIN.
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from apps.notify.telegram import Bot, TelegramClient, deliver_outbox


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
        client, bot = TelegramClient(token), Bot(chats, pin, o["mode"])
        offset = None
        self.stdout.write("Telegram bot running. Ctrl+C to stop.")
        while True:
            for upd in client.updates(offset, timeout_s=0 if o["once"] else 25):
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
