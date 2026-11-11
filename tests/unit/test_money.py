from decimal import Decimal

import pytest

from nextrade_core.domain.money import Money, ceil_to_step, floor_to_step, round_to_tick, to_decimal


class TestToDecimal:
    def test_accepts_str_int_decimal(self):
        assert to_decimal("0.1") == Decimal("0.1")
        assert to_decimal(3) == Decimal(3)
        assert to_decimal(Decimal("2.5")) == Decimal("2.5")

    @pytest.mark.parametrize("bad", [0.1, True, None, [1]])
    def test_refuses_float_bool_and_others(self, bad):
        with pytest.raises(TypeError):
            to_decimal(bad)

    @pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity"])
    def test_refuses_non_numbers(self, bad):
        with pytest.raises(ValueError):
            to_decimal(bad)

    def test_exactness_where_float_fails(self):
        assert to_decimal("0.1") + to_decimal("0.2") == Decimal("0.3")


class TestRounding:
    @pytest.mark.parametrize("value,step,expected", [
        ("0.000123456", "0.00001", "0.00012"),
        ("12.999", "1", "12"),
        ("0.00001", "0.00001", "0.00001"),
        ("0.000009", "0.00001", "0.00000"),
        ("155", "10", "150"),
    ])
    def test_floor_to_step(self, value, step, expected):
        assert floor_to_step(value, step) == Decimal(expected)

    def test_floor_never_exceeds_input(self):
        for raw in ["1.23456789", "0.00099999", "987.654321"]:
            assert floor_to_step(raw, "0.001") <= Decimal(raw)

    @pytest.mark.parametrize("value,step,expected", [
        ("0.000121", "0.00001", "0.00013"),
        ("0.00012", "0.00001", "0.00012"),
        ("151", "10", "160"),
    ])
    def test_ceil_to_step(self, value, step, expected):
        assert ceil_to_step(value, step) == Decimal(expected)

    @pytest.mark.parametrize("price,tick,expected", [
        ("100.004", "0.01", "100.00"),
        ("100.006", "0.01", "100.01"),
        ("100.005", "0.01", "100.00"),  # half-even
        ("100.015", "0.01", "100.02"),
    ])
    def test_round_to_tick(self, price, tick, expected):
        assert round_to_tick(price, tick) == Decimal(expected)

    @pytest.mark.parametrize("fn", [floor_to_step, ceil_to_step, round_to_tick])
    def test_step_must_be_positive(self, fn):
        with pytest.raises(ValueError):
            fn("1", "0")


class TestMoney:
    def test_add_same_currency(self):
        assert Money.of("100.10", "NGN") + Money.of("0.90", "NGN") == Money.of("101.00", "NGN")

    def test_currency_mismatch_raises(self):
        with pytest.raises(ValueError):
            Money.of(1, "NGN") + Money.of(1, "USDT")
        with pytest.raises(ValueError):
            Money.of(1, "NGN") < Money.of(1, "USDT")

    def test_multiply_and_convert(self):
        fee = Money.of("7.5", "USDT") * "0.001"
        assert fee == Money.of("0.0075", "USDT")
        assert Money.of("5", "USDT").convert("1500", "NGN") == Money.of("7500", "NGN")

    def test_rejects_float_amount(self):
        with pytest.raises(TypeError):
            Money(0.1, "NGN")

    def test_currency_code_validated(self):
        with pytest.raises(ValueError):
            Money.of(1, "ngn")

    def test_quantize(self):
        assert Money.of("1.005", "NGN").quantize(2).amount == Decimal("1.00")


class TestMoneyFullSurface:
    def test_sub_neg_rmul(self):
        a, b = Money.of("10", "USDT"), Money.of("4", "USDT")
        assert a - b == Money.of("6", "USDT")
        assert -a == Money.of("-10", "USDT")
        assert 2 * b == Money.of("8", "USDT")

    def test_comparisons(self):
        a, b = Money.of("1", "NGN"), Money.of("2", "NGN")
        assert a < b and a <= b and b > a and b >= a and a <= a and a >= a

    def test_zero_and_str(self):
        assert Money.zero("NGN").amount == 0
        assert str(Money.of("1.5", "USDT")) == "1.5 USDT"

    def test_combining_with_non_money_fails(self):
        with pytest.raises(TypeError):
            Money.of(1, "NGN") + 1
