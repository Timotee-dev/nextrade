"""Trading records. Every row belongs to a TradingRun whose mode (paper or live) never
changes, which is how paper and live results stay separate everywhere (spec 17)."""

from django.db import models

P = {"max_digits": 28, "decimal_places": 10}


class TradingRun(models.Model):
    class Mode(models.TextChoices):
        PAPER = "paper"
        LIVE = "live"

    mode = models.CharField(max_length=8, choices=Mode.choices)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    config = models.JSONField(default=dict)
    initial_equity = models.DecimalField(**P)
    account_currency = models.CharField(max_length=8, default="USDT")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.mode} run {self.pk} ({self.started_at:%Y-%m-%d})"


class SignalRecord(models.Model):
    run = models.ForeignKey(TradingRun, on_delete=models.CASCADE, related_name="signals")
    key = models.CharField(max_length=160)
    strategy_version = models.CharField(max_length=64)
    symbol = models.CharField(max_length=32)
    timeframe = models.CharField(max_length=4)
    bar_time = models.DateTimeField()
    entry = models.DecimalField(**P)
    stop = models.DecimalField(**P)
    target = models.DecimalField(**P)
    max_hold_bars = models.IntegerField()
    regime = models.CharField(max_length=24, blank=True)
    features = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "key"], name="uniq_signal_per_run")]
        ordering = ["-bar_time"]


class OpportunityRecord(models.Model):
    signal = models.OneToOneField(SignalRecord, on_delete=models.CASCADE, related_name="opportunity")
    accepted = models.BooleanField()
    reasons = models.JSONField(default=list)
    p = models.FloatField(null=True)
    p_source = models.CharField(max_length=64)
    uncertainty = models.FloatField(default=0)
    cost_r = models.FloatField()
    expected_net_r = models.FloatField(null=True)
    score = models.FloatField(null=True)
    explanation = models.JSONField(default=dict)


class RiskAssessmentRecord(models.Model):
    opportunity = models.OneToOneField(OpportunityRecord, on_delete=models.CASCADE, related_name="risk")
    approved = models.BooleanField()
    qty = models.DecimalField(**P)
    risk_amount = models.DecimalField(**P)
    notional = models.DecimalField(**P)
    rules = models.JSONField(default=list)
    limits_version = models.CharField(max_length=32)


class Position(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        CLOSED = "closed"

    run = models.ForeignKey(TradingRun, on_delete=models.CASCADE, related_name="positions")
    signal = models.ForeignKey(SignalRecord, on_delete=models.PROTECT, null=True, blank=True)
    symbol = models.CharField(max_length=32)
    strategy = models.CharField(max_length=24)
    strategy_version = models.CharField(max_length=64)
    qty = models.DecimalField(**P)
    entry_price = models.DecimalField(**P)
    entry_fee = models.DecimalField(**P, default=0)
    stop = models.DecimalField(**P)
    target = models.DecimalField(**P)
    initial_risk = models.DecimalField(**P)
    thesis = models.JSONField(default=dict)
    health = models.CharField(max_length=24, default="developing")
    bars_held = models.IntegerField(default=0)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    exit_price = models.DecimalField(**P, null=True, blank=True)
    exit_reason = models.CharField(max_length=24, blank=True)
    fees = models.DecimalField(**P, default=0)
    realized_pnl = models.DecimalField(**P, null=True, blank=True)
    exit_list_id = models.CharField(max_length=40, blank=True)

    class Meta:
        indexes = [models.Index(fields=["run", "status"], name="pos_run_status")]
        ordering = ["-opened_at"]

    @property
    def r_multiple(self):
        if self.realized_pnl is None or not self.initial_risk:
            return None
        return float(self.realized_pnl / self.initial_risk)


class OrderRecord(models.Model):
    run = models.ForeignKey(TradingRun, on_delete=models.CASCADE, related_name="orders")
    position = models.ForeignKey(Position, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    client_order_id = models.CharField(max_length=40, unique=True)
    purpose = models.CharField(max_length=16)  # entry, exit, take_profit, stop
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=4)
    type = models.CharField(max_length=20)
    qty = models.DecimalField(**P)
    price = models.DecimalField(**P, null=True, blank=True)
    stop_price = models.DecimalField(**P, null=True, blank=True)
    status = models.CharField(max_length=20)
    filled_qty = models.DecimalField(**P, default=0)
    avg_price = models.DecimalField(**P, null=True, blank=True)
    fee = models.DecimalField(**P, default=0)
    venue_order_id = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["run", "status"], name="order_run_status")]


class OrderEventRecord(models.Model):
    order = models.ForeignKey(OrderRecord, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField()
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    filled_qty = models.DecimalField(**P)
    note = models.TextField(blank=True)


class LedgerEntry(models.Model):
    """Balances are sums of entries. Kinds: trade, fee, transfer, adjustment."""
    run = models.ForeignKey(TradingRun, on_delete=models.CASCADE, related_name="ledger")
    at = models.DateTimeField()
    asset = models.CharField(max_length=16)
    amount = models.DecimalField(**P)
    kind = models.CharField(max_length=16)
    ref = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [models.Index(fields=["run", "asset"], name="ledger_run_asset")]


class EquitySnapshot(models.Model):
    run = models.ForeignKey(TradingRun, on_delete=models.CASCADE, related_name="equity")
    at = models.DateTimeField()
    equity = models.DecimalField(**P)
    cash = models.DecimalField(**P)
    open_risk = models.DecimalField(**P)
    peak_equity = models.DecimalField(**P)
    drawdown = models.FloatField()

    class Meta:
        indexes = [models.Index(fields=["run", "at"], name="equity_run_at")]
        ordering = ["at"]


class Heartbeat(models.Model):
    process = models.CharField(max_length=32, unique=True)
    at = models.DateTimeField()
    detail = models.JSONField(default=dict)


class Alert(models.Model):
    """Notifications waiting to be delivered (Telegram outbox) and kept as history."""
    class Level(models.TextChoices):
        INFO = "info"
        WARNING = "warning"
        CRITICAL = "critical"

    created_at = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=8, choices=Level.choices, default=Level.INFO)
    kind = models.CharField(max_length=32)
    message = models.TextField()
    delivered_at = models.DateTimeField(null=True, blank=True)
    attempts = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]


class ControlRequest(models.Model):
    """Operator requests from other processes (Telegram, dashboard) for the trader to apply.
    The trader owns the state machine; it applies these on its next cycle."""
    created_at = models.DateTimeField(auto_now_add=True)
    target_state = models.CharField(max_length=16)
    source = models.CharField(max_length=16)          # telegram, dashboard, cli
    reason = models.TextField()
    processed_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at"]


class TelegramConfirmation(models.Model):
    token = models.CharField(max_length=32, unique=True)
    chat_id = models.BigIntegerField()
    action = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
