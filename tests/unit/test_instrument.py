import json
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.binance.filters import instrument_from_symbol, supports_oco

INFO = json.loads((Path(__file__).parent.parent / "fixtures" / "binance_exchange_info.json").read_text())
SYMBOLS = {s["symbol"]: s for s in INFO["symbols"]}


@pytest.fixture
def btc():
    return instrument_from_symbol(SYMBOLS["BTCUSDT"])


class TestFilterParsing:
    def test_btc_rules(self, btc):
        assert btc.tick_size == Decimal("0.01")
        assert btc.step_size == Decimal("0.00001")
        assert btc.min_notional == Decimal("5")
        assert btc.tradable and supports_oco(SYMBOLS["BTCUSDT"])
        assert "STOP_LOSS_LIMIT" in btc.order_types

    def test_legacy_min_notional_filter(self):
        doge = instrument_from_symbol(SYMBOLS["DOGEUSDT"])
        assert doge.min_notional == Decimal("1")
        assert doge.step_size == Decimal("1")
        assert not supports_oco(SYMBOLS["DOGEUSDT"])

    def test_integer_step_is_not_scientific(self):
        old = instrument_from_symbol(SYMBOLS["OLDUSDT"])
        assert str(old.step_size) == "10"
        assert not old.tradable  # status BREAK
        assert old.min_notional == Decimal(0)

    def test_missing_filters_rejected(self):
        with pytest.raises(ValueError):
            instrument_from_symbol({"symbol": "X", "filters": []})


class TestOrderValidation:
    def test_valid_order(self, btc):
        assert btc.order_violations("0.00010", "60000.00") == []

    def test_below_min_notional(self, btc):
        problems = btc.order_violations("0.00005", "60000.00")  # 3 USDT
        assert any("notional" in p for p in problems)

    def test_bad_step_and_tick(self, btc):
        problems = btc.order_violations("0.000105", "60000.005")
        assert any("step" in p for p in problems)
        assert any("tick" in p for p in problems)

    def test_untradable(self):
        old = instrument_from_symbol(SYMBOLS["OLDUSDT"])
        assert "instrument not tradable" in old.order_violations("10", "1.0")

    def test_min_qty_for_notional_is_valid_and_minimal(self, btc):
        price = Decimal("63817.42")
        qty = btc.min_qty_for_notional(price)
        assert btc.order_violations(qty, price) == []
        assert btc.order_violations(qty - btc.step_size, price) != []

    def test_normalize_qty_rounds_down(self, btc):
        assert btc.normalize_qty("0.000129") == Decimal("0.00012")


class TestInstrumentEdges:
    def test_key_and_price_normalisation(self, btc):
        assert btc.key == "binance:BTCUSDT"
        assert btc.normalize_price("60000.006") == Decimal("60000.01")

    def test_non_positive_inputs(self, btc):
        problems = btc.order_violations("0", "0")
        assert "quantity must be positive" in problems and "price must be positive" in problems
        with pytest.raises(ValueError):
            btc.min_qty_for_notional("0")

    def test_quantity_bounds(self):
        doge = instrument_from_symbol(SYMBOLS["DOGEUSDT"])
        assert any("above maximum" in p for p in doge.order_violations("9000001", "0.10000"))
        old = instrument_from_symbol({**SYMBOLS["OLDUSDT"], "status": "TRADING"})
        assert old.order_violations("10", "1.0") == []
        from dataclasses import replace
        coarse_min = replace(old, step_size=Decimal(1))
        assert any("below minimum" in p for p in coarse_min.order_violations("9", "1.0"))

    def test_zero_tick_rejected(self):
        from nextrade_core.domain.instrument import AssetClass, Instrument
        with pytest.raises(ValueError):
            Instrument("x", "X", "A", "B", AssetClass.CRYPTO, Decimal(0), Decimal(1), Decimal(0), Decimal(0))
