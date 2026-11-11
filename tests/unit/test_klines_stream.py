import json
from datetime import timedelta
from decimal import Decimal

import httpx

from adapters.binance.klines import fetch_closed_klines, row_to_candle
from adapters.binance.rest import BinanceRest
from adapters.binance.stream import KlineEvent, Quote, QuoteSampler, build_stream_url, parse_message
from nextrade_core.data.candles import Timeframe, utc_to_ms
from tests.helpers import T0, minutes, to_row


def test_row_to_candle_round_trip():
    bar = minutes(1)[0]
    assert row_to_candle(to_row(bar), "BTCUSDT", Timeframe.M1) == bar


def rest_serving(bars):
    def handler(request):
        start = int(request.url.params["startTime"])
        limit = int(request.url.params["limit"])
        rows = [to_row(c) for c in bars if utc_to_ms(c.open_time) >= start][:limit]
        return httpx.Response(200, json=rows)
    return BinanceRest("https://api.test", transport=httpx.MockTransport(handler))


def test_fetch_pages_and_drops_forming_bar():
    bars = minutes(2500)
    now = bars[-1].open_time + timedelta(seconds=30)  # last bar still forming
    got = fetch_closed_klines(rest_serving(bars), "BTCUSDT", Timeframe.M1, T0, now)
    assert got == bars[:-1]


def test_fetch_from_middle():
    bars = minutes(50)
    got = fetch_closed_klines(rest_serving(bars), "BTCUSDT", Timeframe.M1, bars[20].open_time, T0 + timedelta(hours=1))
    assert got == bars[20:]


def test_stream_url():
    url = build_stream_url(["BTCUSDT", "ETHUSDT"])
    assert url.endswith("streams=btcusdt@kline_1m/btcusdt@bookTicker/ethusdt@kline_1m/ethusdt@bookTicker")


def kline_msg(bar, closed=True):
    return json.dumps({"stream": "btcusdt@kline_1m", "data": {"e": "kline", "s": bar.symbol, "k": {
        "t": utc_to_ms(bar.open_time), "s": bar.symbol, "i": "1m", "o": str(bar.open), "h": str(bar.high),
        "l": str(bar.low), "c": str(bar.close), "v": str(bar.volume), "q": str(bar.quote_volume),
        "n": bar.trade_count, "x": closed}}})


def book_msg(symbol="BTCUSDT", bid="60000.00", ask="60000.01"):
    return json.dumps({"stream": "btcusdt@bookTicker",
                       "data": {"u": 1, "s": symbol, "b": bid, "B": "1.2", "a": ask, "A": "0.8"}})


def test_parse_kline_and_quote():
    bar = minutes(1)[0]
    event = parse_message(kline_msg(bar), T0)
    assert isinstance(event, KlineEvent) and event.closed and event.candle == bar
    quote = parse_message(book_msg(), T0)
    assert isinstance(quote, Quote) and quote.spread == Decimal("0.01")
    assert parse_message(json.dumps({"result": None, "id": 1}), T0) is None


def test_quote_sampler():
    sampler = QuoteSampler(10)
    q = parse_message(book_msg(), T0)
    assert sampler.offer(q) is q
    assert sampler.offer(Quote(q.symbol, T0 + timedelta(seconds=5), q.bid, q.ask, q.bid_qty, q.ask_qty)) is None
    later = Quote(q.symbol, T0 + timedelta(seconds=10), q.bid, q.ask, q.bid_qty, q.ask_qty)
    assert sampler.offer(later) is later
    other = Quote("ETHUSDT", T0 + timedelta(seconds=1), q.bid, q.ask, q.bid_qty, q.ask_qty)
    assert sampler.offer(other) is other
