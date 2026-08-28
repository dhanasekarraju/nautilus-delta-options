from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nautilus_trader.model import OptionKind

from nautilus_delta_options.delta.nautilus_mapper import (
    map_delta_product_to_crypto_option,
)
from nautilus_delta_options.delta.product import DeltaOptionProduct


def _product() -> DeltaOptionProduct:
    return DeltaOptionProduct(
        product_id=149236,
        symbol="P-BTC-94000-301026",
        contract_type="put_options",
        underlying="BTC",
        underlying_precision=8,
        quote_currency="USD",
        quote_precision=8,
        settlement_currency="USD",
        settlement_precision=8,
        contract_unit_currency="BTC",
        strike_price=Decimal("94000"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        launch_time=datetime(2026, 8, 25, 2, 20, 3, tzinfo=UTC),
        settlement_time=datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
        maker_fee=Decimal("0.0001"),
        taker_fee=Decimal("0.0001"),
        premium_cap_rate=Decimal("0.035"),
        position_size_limit=Decimal("50000"),
        is_quanto=False,
        notional_type="vanilla",
        trading_status="operational",
        state="live",
    )


def test_maps_delta_product_to_nautilus_crypto_option() -> None:
    instrument = map_delta_product_to_crypto_option(
        _product(),
        ts_event_ns=1_000,
        ts_init_ns=2_000,
    )

    assert str(instrument.id) == "P-BTC-94000-301026.DELTA"
    assert str(instrument.raw_symbol) == "P-BTC-94000-301026"
    assert str(instrument.underlying) == "BTC"
    assert str(instrument.quote_currency) == "USD"
    assert str(instrument.settlement_currency) == "USD"
    assert instrument.is_inverse is False
    assert instrument.option_kind == OptionKind.PUT
    assert str(instrument.strike_price) == "94000"
    assert instrument.price_precision == 1
    assert instrument.size_precision == 0
    assert str(instrument.price_increment) == "0.1"
    assert str(instrument.size_increment) == "1"
    assert str(instrument.multiplier) == "0.001"
    assert instrument.maker_fee == Decimal("0.0001")
    assert instrument.taker_fee == Decimal("0.0001")
    assert instrument.expiration_ns > instrument.activation_ns


def test_maps_call_option_kind() -> None:
    product = replace(
        _product(),
        symbol="C-BTC-94000-301026",
        contract_type="call_options",
    )

    instrument = map_delta_product_to_crypto_option(
        product,
        ts_event_ns=1_000,
        ts_init_ns=2_000,
    )

    assert instrument.option_kind == OptionKind.CALL


def test_rejects_quanto_option() -> None:
    product = replace(_product(), is_quanto=True)

    with pytest.raises(ValueError, match="Quanto"):
        map_delta_product_to_crypto_option(
            product,
            ts_event_ns=1_000,
            ts_init_ns=2_000,
        )
