import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from adapters.base import RateLimitedError, RejectedError, RetryableError, UnknownStateError
from adapters.quidax.markets import best_bid_ask, instrument_from_market, parse_klines
from adapters.quidax.rest import QuidaxRest
from nextrade_core.costs.model import cost_model_for
from nextrade_core.data.candles import Timeframe

FX = Path(__file__).parent.parent / "fixtures" / "quidax"


def load(name):
    return json.loads((FX / name).read_text(), parse_float=Decimal)


class TestRealResponseFormats:
    """Fixtures are real Quidax responses captured from the operator's PC on 2026-11-11."""

    def test_kline_field_order_is_open_close_high_low(self):
        bars = parse_klines(load("k_btcusdt.json")["data"], "BTCUSDT", Timeframe.M15)
        assert [b.open_time for b in bars] == sorted(b.open_time for b in bars)   # newest-first input
        for b in bars:
            assert b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close)
        first = bars[0]
        assert (first.open, first.close, first.high, first.low) == (
            Decimal("74133.59"), Decimal("74156.16"), Decimal("74208.71"), Decimal("74109.25"))
        assert all(bars[i].close == bars[i + 1].open for i in (0, 3))              # continuity where visible
        assert bars[1].open_time - bars[0].open_time == Timeframe.M15.delta

    def test_flat_illiquid_bars_and_zero_volume(self):
        bars = parse_klines(load("k_btcngn.json")["data"], "BTCNGN", Timeframe.M1)
        assert len(bars) == 5 and any(b.volume == 0 for b in bars)
        assert all(b.open == b.high == b.low == b.close for b in bars)

    def test_depth_best_prices(self):
        bid, ask, bq, aq = best_bid_ask(load("depth_btcngn.json")["data"])
        assert (bid, ask) == (Decimal("104037527"), Decimal("104547199"))
        assert bq == Decimal("0.00311806") and aq == Decimal("0.00031061")
        assert best_bid_ask({"bids": [], "asks": [[1, 1]]}) is None

    def test_market_rules(self):
        markets = {m["id"]: m for m in load("markets.json")["data"]}
        eth = instrument_from_market(markets["ethngn"])
        assert (eth.symbol, eth.base, eth.quote, eth.venue) == ("ETHNGN", "ETH", "NGN", "quidax")
        assert eth.tick_size == Decimal(1) and eth.step_size == Decimal("0.0000001")
        assert eth.min_notional == Decimal(1250)
        assert eth.order_violations(Decimal("0.0005"), Decimal("2927573")) == []      # ~N1,464
        assert any("notional" in p for p in eth.order_violations(Decimal("0.0003"), Decimal("2927573")))
        usdt_pair = dict(markets["ethngn"], id="ethusdt", quote_unit="USDT")
        assert instrument_from_market(usdt_pair, Decimal("1400")).min_notional == Decimal("0.89")


def client(handler, secret="sk", **kw):
    return QuidaxRest("https://q.test/api/v1", secret, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None, **kw)


class TestClient:
    def test_unwraps_envelope_and_parses_decimals(self):
        c = client(lambda r: httpx.Response(200, content=(FX / "tickers.json").read_bytes()))
        t = c.tickers()
        assert t["ethngn"]["ticker"]["buy"] == Decimal("2927573")

    def test_kline_query(self):
        seen = {}

        def handler(r):
            seen.update(path=r.url.path, **dict(r.url.params))
            return httpx.Response(200, content=(FX / "k_btcngn.json").read_bytes())

        rows = client(handler).klines("btcngn", 60, since_s=1700000000, limit=500)
        assert len(rows) == 5
        assert seen == {"path": "/api/v1/markets/btcngn/k", "period": "60", "timestamp": "1700000000", "limit": "500"}

    def test_auth_header_and_order_body(self):
        seen = {}

        def handler(r):
            seen["auth"] = r.headers.get("authorization")
            seen["body"] = json.loads(r.content)
            seen["path"] = r.url.path
            return httpx.Response(200, json={"status": "success", "data": {"id": "abc", "status": "wait"}})

        out = client(handler).create_order("ethngn", "buy", "limit", Decimal("0.0005000"), Decimal("2900000"))
        assert out["id"] == "abc" and seen["auth"] == "Bearer sk" and seen["path"] == "/api/v1/users/me/orders"
        assert seen["body"] == {"market": "ethngn", "side": "buy", "ord_type": "limit",
                                "volume": "0.0005000", "price": "2900000"}

    def test_market_order_has_no_price(self):
        seen = {}

        def handler(r):
            seen["body"] = json.loads(r.content)
            return httpx.Response(200, json={"status": "success", "data": {"id": "x"}})

        client(handler).create_order("ethngn", "sell", "market", Decimal("0.001"))
        assert "price" not in seen["body"]

    def test_private_call_without_secret_refused_locally(self):
        with pytest.raises(RejectedError):
            client(lambda r: httpx.Response(200, json={}), secret=None).wallets()

    def test_rate_limit_444(self):
        with pytest.raises(RateLimitedError):
            client(lambda r: httpx.Response(444)).markets()

    def test_404_is_rejected_and_get_order_returns_none(self):
        nf = lambda r: httpx.Response(404, content=(FX / "not_found.json").read_bytes())  # noqa: E731
        with pytest.raises(RejectedError) as info:
            client(nf).depth("btcngn")
        assert info.value.code == 404
        assert client(nf).get_order("missing") is None

    def test_error_status_in_200_envelope(self):
        c = client(lambda r: httpx.Response(200, json={"status": "error", "message": "Insufficient balance"}))
        with pytest.raises(RejectedError, match="Insufficient"):
            c.create_order("ethngn", "buy", "market", Decimal("1"))

    def test_unknown_outcome_on_order_timeout_but_retryable_on_reads(self):
        def slow(r):
            raise httpx.ReadTimeout("slow", request=r)

        with pytest.raises(UnknownStateError):
            client(slow).cancel_order("abc")
        with pytest.raises(RetryableError):
            client(slow).markets()
        with pytest.raises(UnknownStateError):
            client(lambda r: httpx.Response(502)).create_order("ethngn", "buy", "market", Decimal("1"))

    def test_throttle_spaces_requests(self):
        waits, clock = [], [0.0]

        def mono():
            return clock[0]

        def sleep(s):
            waits.append(s)
            clock[0] += s

        c = QuidaxRest("https://q.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
                       sleep=sleep, monotonic=mono)
        c.markets()
        c.markets()
        assert waits and waits[-1] == pytest.approx(0.21)


def test_quidax_cost_model():
    m = cost_model_for("quidax", {"ETHNGN": 18.0})
    assert m.fee_rate == 0.0015 and m.spread("ETHNGN") == 0.0018 and m.spread("BTCNGN") == 0.004
    # a 4% stop on ETH/NGN: round trip ~0.58% of price -> ~0.15 R
    assert m.cost_in_r("ETHNGN", 100.0, 96.0) == pytest.approx(0.145, abs=0.01)
