import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from adapters.base import RejectedError
from adapters.gate.client import (GateClient, earliest_allowed, instrument_from_pair, pair_of, row_to_candle)
from nextrade_core.data.candles import EPOCH, Timeframe
from nextrade_core.data.resample import resample_from
from tests.helpers import minutes

FX = Path(__file__).parent.parent / "fixtures" / "gate"


def test_real_rows_close_high_low_open_order_and_forming_bar_dropped():
    rows = json.loads((FX / "recent_1h.json").read_text())
    bars = [row_to_candle(r, "ETHUSDT", Timeframe.H1) for r in rows]
    assert bars[-1] is None                                    # "false" = still forming
    first = bars[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("2058.26"), Decimal("2060.77"), Decimal("2044.49"), Decimal("2054.69"))
    assert first.open_time == datetime(2026, 11, 11, 14, tzinfo=UTC)
    assert first.volume == Decimal("12747.06510000") and first.quote_volume == Decimal("26180273.80059600")
    for b in bars[:-1]:
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
    for a, b in zip(bars[:3], bars[1:4]):
        assert abs(a.close - b.open) / a.close < Decimal("0.001")   # consecutive bars connect


def test_pair_rules_and_names():
    inst = instrument_from_pair(json.loads((FX / "pair.json").read_text()))
    assert (inst.symbol, inst.venue, inst.tick_size, inst.step_size) == ("ETHUSDT", "gate", Decimal("0.01"),
                                                                        Decimal("0.0001"))
    assert inst.min_qty == Decimal("0.001") and inst.min_notional == Decimal(3)
    assert pair_of("ETHUSDT") == "ETH_USDT" and pair_of("SOLBTC") == "SOL_BTC"
    with pytest.raises(ValueError):
        pair_of("USDT")


def test_earliest_allowed_is_within_10000_bars():
    now = datetime(2026, 11, 11, 19, 30, tzinfo=UTC)
    e = earliest_allowed(now, Timeframe.H4)
    assert (Timeframe.H4.floor(now) - e) / Timeframe.H4.delta == 9990


def gate_server(series, calls):
    def handler(request):
        if request.url.path.endswith("/spot/candlesticks"):
            calls.append(dict(request.url.params))
            f, t = int(request.url.params["from"]), int(request.url.params["to"])
            if t - f > 999 * 3600:
                return httpx.Response(400, json={"label": "INVALID_PARAM_VALUE", "message": "too many points"})
            rows = [[str(int((c.open_time - EPOCH).total_seconds())), str(c.quote_volume), str(c.close), str(c.high),
                     str(c.low), str(c.open), str(c.volume), "true"]
                    for c in series if f <= (c.open_time - EPOCH).total_seconds() <= t]
            return httpx.Response(200, json=rows)
        return httpx.Response(400, content=(FX / "too_old.json").read_bytes())
    return handler


def hourly(n):
    from dataclasses import replace
    return [replace(c, symbol="ETHUSDT", timeframe=Timeframe.H1, trade_count=0,
                    open_time=c.open_time.replace(minute=0) + timedelta(hours=i))
            for i, c in enumerate(minutes(n))]


def test_candles_page_1000_at_a_time_and_respect_window():
    series, calls = hourly(2500), []
    gc = GateClient("https://g.test", transport=httpx.MockTransport(gate_server(series, calls)), sleep=lambda s: None)
    start, end = series[0].open_time, series[-1].open_time + Timeframe.H1.delta
    got = gc.candles("ETHUSDT", Timeframe.H1, start, end)
    assert got == series and len(calls) == 3
    assert calls[0]["currency_pair"] == "ETH_USDT" and calls[0]["interval"] == "1h"


def test_too_old_is_rejected_with_gate_label():
    gc = GateClient("https://g.test", transport=httpx.MockTransport(gate_server([], [])), sleep=lambda s: None)
    with pytest.raises(RejectedError, match="Candlestick too long ago"):
        gc.currency_pair("ETH_USDT")


def test_resample_from_hourly():
    h = hourly(48)
    four = resample_from(h, Timeframe.H4)
    assert len(four) == 12 and four[0].open == h[0].open and four[0].close == h[3].close
    assert four[0].high == max(c.high for c in h[:4]) and four[0].volume == sum(c.volume for c in h[:4])
    assert len(resample_from(h[1:], Timeframe.H4)) == 11          # partial first bucket dropped
    with pytest.raises(ValueError):
        resample_from(h, Timeframe.M30)
