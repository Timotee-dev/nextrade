from django.contrib import admin

from .models import Instrument, VenueAccount


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "venue", "tradable", "tick_size", "step_size", "min_notional", "oco_allowed", "updated_at")
    list_filter = ("venue", "tradable", "asset_class")
    search_fields = ("symbol", "base")


@admin.register(VenueAccount)
class VenueAccountAdmin(admin.ModelAdmin):
    list_display = ("label", "venue", "mode", "key_env_var", "created_at")
