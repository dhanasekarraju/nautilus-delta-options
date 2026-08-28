from nautilus_delta_options.delta.public_client import (
    parse_option_products_payload,
)


def _valid_product() -> dict[str, object]:
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


def test_parses_valid_product_payload() -> None:
    snapshot = parse_option_products_payload(
        {
            "success": True,
            "result": [_valid_product()],
        },
        underlying="BTC",
    )

    assert len(snapshot.products) == 1
    assert snapshot.products[0].product_id == 149236
    assert snapshot.rejected_records == ()


def test_retains_valid_products_and_reports_invalid_records() -> None:
    snapshot = parse_option_products_payload(
        {
            "success": True,
            "result": [_valid_product(), "invalid record"],
        },
        underlying="BTC",
    )

    assert len(snapshot.products) == 1
    assert snapshot.rejected_records == ("record 1: must be an object",)
