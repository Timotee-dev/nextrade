from django.contrib import admin

from .models import AuditLog, SystemStateTransition


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SystemStateTransition)
class SystemStateTransitionAdmin(ReadOnlyAdmin):
    list_display = ("at", "from_state", "to_state", "actor", "reason")
    list_filter = ("to_state", "actor")


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("at", "actor", "action", "target")
    list_filter = ("action",)
