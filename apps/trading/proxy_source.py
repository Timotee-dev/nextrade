"""Paper trading on Quidax with signals from Gate.io daily bars (ADR 0005, 0006).

Quidax keeps only ~300 bars, so a 200-day strategy cannot get its signals from Quidax.
Signals therefore come from Gate.io USDT daily bars (the research data), while every
fill, spread and stop is checked against LIVE Quidax NGN quotes converted to USDT at the
live USDT/NGN rate. The difference between the two prices (the basis) is real and shows
up in the paper results, which is the point of paper trading.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from adapters.base import VenueError
from adapters.binance.stream import Quote
from adapters.quidax.markets import best_bid_ask, market_of
from apps.market.models import Instrument, QuoteSample
from apps.market.store import CandleStore
from nextrade_core.data.candles import Timeframe


class GateQuidaxSource:
    """MarketSource: Gate.io daily frames + Quidax quotes in USDT."""

    def __init__(self, gate: dict[str, Instrument], quidax_by_symbol: dict[str, Instrument], usdtngn: Instrument,
                 max_quote_age: timedelta = timedelta(minutes=5), max_bar_age: timedelta = timedelta(days=2)):
        self.gate, self.quidax, self.usdtngn = gate, quidax_by_symbol, usdtngn
        self.candles = CandleStore(gate)
        self.max_quote_age, self.max_bar_age = max_quote_age, max_bar_age

    def frame(self, symbol, timeframe, bars, now):
        start = timeframe.floor(now) - timeframe.delta * (bars + 1)
        f = self.candles.load_frame(symbol, timeframe, start, timeframe.floor(now))
        return f.iloc[-bars:]

    def last_closed_bar(self, symbol, timeframe, now):
        last = self.candles.last_open_time(symbol, timeframe)
        if last is None:
            return None
        return last if last + timeframe.delta <= now else last - timeframe.delta

    def _latest(self, inst: Instrument):
        return QuoteSample.objects.filter(instrument=inst).order_by("-ts").values_list("bid", "ask", "ts").first()

    def latest_quote(self, symbol):
        q, u = self._latest(self.quidax[symbol]), self._latest(self.usdtngn)
        if q is None or u is None:
            return None
        ts = min(q[2], u[2])
        if timezone.now() - ts > self.max_quote_age:
            return None
        rate = (u[0] + u[1]) / 2
        return (q[0] / rate).quantize(Decimal("1e-10")), (q[1] / rate).quantize(Decimal("1e-10")), ts

    def data_ok(self, symbol, now):
        last = self.candles.last_open_time(symbol, Timeframe.D1)
        if last is None or now - (last + Timeframe.D1.delta) > self.max_bar_age:
            return False
        return self.latest_quote(symbol) is not None


class QuidaxQuotePoller:
    """Stores the best Quidax bid/ask for each traded market and USDT/NGN (one depth call each)."""

    def __init__(self, client, quidax_by_symbol: dict[str, Instrument], usdtngn: Instrument):
        self.client, self.markets, self.usdtngn = client, quidax_by_symbol, usdtngn
        self.store = CandleStore({i.symbol: i for i in [*quidax_by_symbol.values(), usdtngn]})

    def poll(self, now: datetime) -> int:
        saved = 0
        for inst in [self.usdtngn, *self.markets.values()]:
            try:
                top = best_bid_ask(self.client.depth(market_of(inst.symbol), 5))
            except VenueError as exc:
                self.store.record_issue("quote_poll_error", inst.symbol, {"error": str(exc)[:300]})
                continue
            if top:
                self.store.save_quotes([Quote(inst.symbol, now, *top)])
                saved += 1
        return saved


class GateDailyRefresher:
    """Keeps Gate.io daily bars current (one request per symbol, at most hourly)."""

    def __init__(self, client, gate: dict[str, Instrument]):
        self.client, self.store = client, CandleStore(gate)
        self.symbols = sorted(gate)

    def refresh(self, now: datetime) -> int:
        added = 0
        tf = Timeframe.D1
        for s in self.symbols:
            last = self.store.last_open_time(s, tf)
            start = last + tf.delta if last else tf.floor(now) - 800 * tf.delta
            if start >= tf.floor(now):
                continue
            try:
                bars = self.client.candles(s, tf, start, tf.floor(now))
            except VenueError as exc:
                self.store.record_issue("gate_refresh_error", s, {"error": str(exc)[:300]})
                continue
            if bars:
                self.store.save_candles(bars, "rest")
                added += len(bars)
        return added
