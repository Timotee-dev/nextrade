"""Refresh Binance order rules (tick, step, minimum notional) into the database."""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.market.services import upsert_instruments
from apps.ops.models import AuditLog

from ._common import make_rest, parse_symbols

make_client = make_rest  # patch point for tests


class Command(BaseCommand):
    help = "Download Binance exchange rules for the configured symbols and upsert Instruments."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", default=None)

    def handle(self, *args, symbols: str | None, **options):
        wanted = parse_symbols(symbols)
        client = make_client()
        try:
            with transaction.atomic():
                created, updated = upsert_instruments(client, wanted)
                AuditLog.objects.create(actor="system", action="sync_instruments", target="binance",
                                        details={"symbols": wanted, "created": created, "updated": updated})
        finally:
            client.close()
        self.stdout.write(self.style.SUCCESS(f"Instruments: {created} created, {updated} updated"))
