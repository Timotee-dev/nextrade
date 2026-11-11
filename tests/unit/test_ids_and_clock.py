from datetime import UTC, datetime, timedelta, timezone

import pytest

from nextrade_core.clock import SimClock, WallClock
from nextrade_core.domain.ids import CLIENT_ORDER_ID_RE, make_client_order_id


class TestClientOrderId:
    def test_deterministic(self):
        assert make_client_order_id("run1", "opp42", "entry") == make_client_order_id("run1", "opp42", "entry")

    def test_differs_by_leg_and_opportunity(self):
        ids = {make_client_order_id("run1", "opp42", "entry"),
               make_client_order_id("run1", "opp42", "stop"),
               make_client_order_id("run1", "opp43", "entry"),
               make_client_order_id("run2", "opp42", "entry")}
        assert len(ids) == 4

    def test_binance_format(self):
        cid = make_client_order_id("r", "o", "l")
        assert CLIENT_ORDER_ID_RE.match(cid) and len(cid) <= 36

    def test_requires_all_parts(self):
        with pytest.raises(ValueError):
            make_client_order_id("", "o", "l")


class TestClock:
    def test_sim_clock_moves_forward_only(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = SimClock(start)
        clock.advance_to(start + timedelta(minutes=1))
        assert clock.now() == start + timedelta(minutes=1)
        with pytest.raises(ValueError):
            clock.advance_to(start)

    def test_requires_utc(self):
        with pytest.raises(ValueError):
            SimClock(datetime(2026, 1, 1))
        with pytest.raises(ValueError):
            SimClock(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))))

    def test_wall_clock_is_utc(self):
        assert WallClock().now().tzinfo is UTC
