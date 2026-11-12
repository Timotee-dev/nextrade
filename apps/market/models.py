from django.db import models

from nextrade_core.domain.instrument import AssetClass, Instrument as DomainInstrument

PRICE = {"max_digits": 28, "decimal_places": 10}


class Instrument(models.Model):
    """Venue order rules, refreshed from the exchange (spec 3.2, 14)."""

    venue = models.CharField(max_length=32)
    symbol = models.CharField(max_length=32)
    base = models.CharField(max_length=16)
    quote = models.CharField(max_length=16)
    asset_class = models.CharField(max_length=16, choices=[(a.value, a.value) for a in AssetClass])
    tick_size = models.DecimalField(**PRICE)
    step_size = models.DecimalField(**PRICE)
    min_qty = models.DecimalField(**PRICE)
    min_notional = models.DecimalField(**PRICE)
    max_qty = models.DecimalField(**PRICE, null=True, blank=True)
    contract_size = models.DecimalField(**PRICE, default=1)
    tradable = models.BooleanField(default=True)
    oco_allowed = models.BooleanField(default=False)
    order_types = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["venue", "symbol"], name="uniq_instrument_venue_symbol"),
            models.CheckConstraint(condition=models.Q(tick_size__gt=0), name="instrument_tick_positive"),
            models.CheckConstraint(condition=models.Q(step_size__gt=0), name="instrument_step_positive"),
        ]
        ordering = ["venue", "symbol"]

    def __str__(self) -> str:
        return f"{self.venue}:{self.symbol}"

    def to_domain(self) -> DomainInstrument:
        return DomainInstrument(
            venue=self.venue, symbol=self.symbol, base=self.base, quote=self.quote,
            asset_class=AssetClass(self.asset_class), tick_size=self.tick_size,
            step_size=self.step_size, min_qty=self.min_qty, min_notional=self.min_notional,
            max_qty=self.max_qty, contract_size=self.contract_size, tradable=self.tradable,
            order_types=frozenset(self.order_types),
        )


class VenueAccount(models.Model):
    """A connection to a venue. Stores the NAME of the env var holding the key, never the key."""

    class Mode(models.TextChoices):
        TESTNET = "testnet"
        PAPER = "paper"
        LIVE = "live"

    venue = models.CharField(max_length=32)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    label = models.CharField(max_length=64, unique=True)
    key_env_var = models.CharField(max_length=64, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.label} ({self.venue}, {self.mode})"


class Candle(models.Model):
    """OHLCV bars. 1m bars are the stored source of truth; higher timeframes are resampled."""

    class Source(models.TextChoices):
        ARCHIVE = "archive"
        REST = "rest"
        WS = "ws"

    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="candles")
    timeframe = models.CharField(max_length=4)
    open_time = models.DateTimeField()
    open = models.DecimalField(**PRICE)
    high = models.DecimalField(**PRICE)
    low = models.DecimalField(**PRICE)
    close = models.DecimalField(**PRICE)
    volume = models.DecimalField(max_digits=32, decimal_places=10)
    quote_volume = models.DecimalField(max_digits=32, decimal_places=10)
    trade_count = models.IntegerField()
    source = models.CharField(max_length=8, choices=Source.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["instrument", "timeframe", "open_time"],
                                    name="uniq_candle_instrument_tf_time"),
        ]
        ordering = ["instrument", "timeframe", "open_time"]


class QuoteSample(models.Model):
    """Best bid/ask sampled live (about every 10 s), to build a spread history."""

    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="quotes")
    ts = models.DateTimeField()
    bid = models.DecimalField(**PRICE)
    ask = models.DecimalField(**PRICE)
    bid_qty = models.DecimalField(max_digits=32, decimal_places=10)
    ask_qty = models.DecimalField(max_digits=32, decimal_places=10)

    class Meta:
        indexes = [models.Index(fields=["instrument", "ts"], name="quote_instrument_ts")]


class DataQualityEvent(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="quality_events")
    timeframe = models.CharField(max_length=4, blank=True)
    kind = models.CharField(max_length=32)
    at = models.DateTimeField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    run_label = models.CharField(max_length=64, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["kind", "detected_at"], name="dq_kind_detected")]
        ordering = ["-detected_at"]


class BookSnapshot(models.Model):
    """Top order-book levels at one moment (microstructure research: market making and
    price gaps). Quidax NGN books are stored with venue "quidax"; world prices from Gate.io
    are stored as one-level books with venue "gate". Prices and sizes kept as strings."""

    venue = models.CharField(max_length=16)
    symbol = models.CharField(max_length=32)
    ts = models.DateTimeField()
    bids = models.JSONField()            # [[price, qty], ...] best first
    asks = models.JSONField()

    class Meta:
        indexes = [models.Index(fields=["venue", "symbol", "ts"], name="book_venue_symbol_ts")]
