from django.contrib import admin

from .models import ExperimentRun


@admin.register(ExperimentRun)
class ExperimentRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "strategy_version", "model_kind", "timeframe", "gate_pass", "dataset_hash")
    list_filter = ("gate_pass", "model_kind", "timeframe")
    readonly_fields = [f.name for f in ExperimentRun._meta.fields]
