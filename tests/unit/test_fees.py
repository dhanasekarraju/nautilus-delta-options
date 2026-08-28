from decimal import Decimal

from nautilus_delta_options.payoff.fees import calculate_option_trade_fee


def test_fee_uses_notional_rate_when_below_premium_cap() -> None:
    result = calculate_option_trade_fee(
        spot_price=Decimal("80521.7"),
        option_price=Decimal("14419"),
        contracts=Decimal("1"),
        contract_value=Decimal("0.001"),
    )

    assert result.notional == Decimal("80.5217")
    assert result.premium == Decimal("14.419")
    assert result.uncapped_fee == Decimal("0.00805217")
    assert result.fee_before_gst == Decimal("0.00805217")
    assert result.total_fee == Decimal("0.0095015606")


def test_fee_uses_premium_cap_for_deep_otm_option() -> None:
    result = calculate_option_trade_fee(
        spot_price=Decimal("80000"),
        option_price=Decimal("10"),
        contracts=Decimal("1"),
        contract_value=Decimal("0.001"),
    )

    assert result.uncapped_fee == Decimal("0.0080000")
    assert result.premium_cap == Decimal("0.00035")
    assert result.fee_before_gst == Decimal("0.00035")
    assert result.total_fee == Decimal("0.0004130")


def test_worthless_option_has_zero_fee() -> None:
    result = calculate_option_trade_fee(
        spot_price=Decimal("80000"),
        option_price=Decimal("0"),
        contracts=Decimal("1"),
        contract_value=Decimal("0.001"),
    )

    assert result.total_fee == Decimal("0.0000000")
