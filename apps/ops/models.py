"""Append-only operational records. Rows can be created, never edited or deleted."""

from django.db import models

from nextrade_core.controller.state_machine import Actor, TradingState, Transition


class AppendOnlyError(Exception):
    pass


class AppendOnlyModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise AppendOnlyError(f"{type(self).__name__} rows cannot be modified")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{type(self).__name__} rows cannot be deleted")


class SystemStateTransition(AppendOnlyModel):
    from_state = models.CharField(max_length=16, choices=[(s.value, s.value) for s in TradingState])
    to_state = models.CharField(max_length=16, choices=[(s.value, s.value) for s in TradingState])
    actor = models.CharField(max_length=16, choices=[(a.value, a.value) for a in Actor])
    reason = models.TextField()
    context = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-at", "-id"]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M:%S} {self.from_state} -> {self.to_state} ({self.actor})"


class AuditLog(AppendOnlyModel):
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=64)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at", "-id"]


def record_transition(t: Transition) -> SystemStateTransition:
    """Recorder callback for TradingStateMachine."""
    return SystemStateTransition.objects.create(
        from_state=t.from_state.value, to_state=t.to_state.value, actor=t.actor.value,
        reason=t.reason, context=t.context, at=t.at,
    )
