import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from adapters.base import RateLimitedError, RejectedError, RetryableError, UnknownStateError
from adapters.binance.rest import BinanceRest

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
NOW_MS = 1_794_400_000_000


def client(handler, key="k", secret="s"):
    return BinanceRest("https://api.test", key, secret, transport=httpx.MockTransport(handler),
                       time_ms=lambda: NOW_MS)


def test_public_call_parses_decimals_and_tracks_weight():
    def handler(request):
        assert request.url.path == "/api/v3/exchangeInfo"
        assert json.loads(request.url.params["symbols"]) == ["BTCUSDT"]
        return httpx.Response(200, json=INFO, headers={"x-mbx-used-weight-1m": "21"})

    c = client(handler)
    info = c.exchange_info(["BTCUSDT"])
    assert info["symbols"][0]["symbol"] == "BTCUSDT"
    assert c.used_weight_1m == 21


def test_signed_request_signature_and_key_header():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-MBX-APIKEY")
        seen["query"] = urlsplit(str(request.url)).query
        return httpx.Response(200, json={"canTrade": True})

    client(handler).account()
    params = dict(parse_qsl(seen["query"]))
    unsigned = seen["query"].rsplit("&signature=", 1)[0]
    expected = hmac.new(b"s", unsigned.encode(), hashlib.sha256).hexdigest()
    assert seen["key"] == "k"
    assert params["signature"] == expected
    assert params["timestamp"] == str(NOW_MS)


def test_signed_call_without_keys_is_rejected_locally():
    with pytest.raises(RejectedError):
        client(lambda r: httpx.Response(200, json={}), key=None, secret=None).account()


def test_sync_time_offset():
    c = client(lambda r: httpx.Response(200, json={"serverTime": NOW_MS + 750}))
    assert c.sync_time() == 750


def test_new_order_sends_exact_decimal_strings():
    seen = {}

    def handler(request):
        seen.update(dict(parse_qsl(urlsplit(str(request.url)).query)))
        return httpx.Response(200, json={"status": "NEW"})

    client(handler).new_order(symbol="BTCUSDT", side="BUY", order_type="LIMIT",
                              quantity=Decimal("0.00010"), price=Decimal("30000.00"),
                              time_in_force="GTC", client_order_id="nt-abc")
    assert seen["quantity"] == "0.00010" and seen["price"] == "30000.00"
    assert seen["newClientOrderId"] == "nt-abc"


@pytest.mark.parametrize("status,exc", [(429, RateLimitedError), (418, RateLimitedError)])
def test_rate_limits(status, exc):
    c = client(lambda r: httpx.Response(status, headers={"retry-after": "7"}))
    with pytest.raises(exc) as info:
        c.ping()
    assert info.value.retry_after_s == 7


def test_5xx_on_read_is_retryable_but_on_order_is_unknown():
    c = client(lambda r: httpx.Response(503))
    with pytest.raises(RetryableError):
        c.ping()
    with pytest.raises(UnknownStateError):
        c.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET",
                    quantity=Decimal("0.001"), client_order_id="nt-x")


def test_timeout_on_order_is_unknown_never_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(UnknownStateError):
        client(handler).cancel_order("BTCUSDT", "nt-x")


def test_connect_error_is_retryable_even_for_orders():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(RetryableError) as info:
        client(handler).new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET",
                                  quantity=Decimal("0.001"), client_order_id="nt-x")
    assert not isinstance(info.value, UnknownStateError)


def test_get_order_returns_none_when_binance_says_it_does_not_exist():
    c = client(lambda r: httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."}))
    assert c.get_order("BTCUSDT", "nt-x") is None


def test_other_4xx_is_rejected_with_code():
    c = client(lambda r: httpx.Response(400, json={"code": -1013, "msg": "Filter failure: NOTIONAL"}))
    with pytest.raises(RejectedError) as info:
        c.new_order(symbol="BTCUSDT", side="BUY", order_type="MARKET",
                    quantity=Decimal("0.00001"), client_order_id="nt-x")
    assert info.value.code == -1013


def test_clock_drift_is_retryable():
    c = client(lambda r: httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"}))
    with pytest.raises(RetryableError):
        c.account()
