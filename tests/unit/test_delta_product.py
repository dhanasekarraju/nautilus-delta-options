from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.product import DeltaOptionProduct


def _product() -> dict[str, object]:
    return {
        "id": 149236,
        "symbol": "P-BTC-94000-301026",
        "contract_type": "put_options",
        "contract_unit_currency": "BTC",
        "contract_value": "0.001",
        "tick_size": "0.1",
        "strike_price": "94000",
        "launch_time": "2026-08-25T02:20:03Z",
        "settlement_time": "2026-10-30T12:00:00Z",
        "maker_commission_rate": "0.0001",
        "taker_commission_rate": "0.0001",
        "position_size_limit": 50000,
        "is_quanto": False,
        "notional_type": "vanilla",
        "trading_status": "operational",
        "state": "live",
        "underlying_asset": {
            "symbol": "BTC",
            "precision": 8,
        },
        "quoting_asset": {
            "symbol": "USD",
            "precision": 8,
        },
        "settling_asset": {
            "symbol": "USD",
            "precision": 8,
        },
        "product_specs": {
            "premium_commission_rate": 0.035,
        },
    }


def test_parses_delta_option_product() -> None:
    product = DeltaOptionProduct.from_api(_product())

    assert product.product_id == 149236
    assert product.underlying == "BTC"
    assert product.quote_currency == "USD"
    assert product.settlement_currency == "USD"
    assert product.strike_price == Decimal("94000")
    assert product.contract_value == Decimal("0.001")
    assert product.maker_fee == Decimal("0.0001")
    assert product.premium_cap_rate == Decimal("0.035")
    assert product.launch_time == datetime(2026, 8, 25, 2, 20, 3, tzinfo=UTC)
    assert product.settlement_time == datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def test_rejects_contract_unit_mismatch() -> None:
    raw = _product()
    raw["contract_unit_currency"] = "ETH"

    with pytest.raises(ValueError, match="must match underlying"):
        DeltaOptionProduct.from_api(raw)


def test_rejects_datetime_without_timezone() -> None:
    raw = _product()
    raw["settlement_time"] = "2026-10-30T12:00:00"

    with pytest.raises(ValueError, match="timezone information"):
        DeltaOptionProduct.from_api(raw)
