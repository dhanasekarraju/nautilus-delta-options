from datetime import date
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.models import DeltaOptionTicker


def _sample_ticker() -> dict[str, object]:
    return {
        "product_id": 149236,
        "symbol": "P-BTC-94000-301026",
        "underlying_asset_symbol": "BTC",
        "contract_type": "put_options",
        "strike_price": "94000",
        "mark_price": "14315.31128269",
        "spot_price": "80521.7",
        "contract_value": "0.001",
        "tick_size": "0.1",
        "quotes": {
            "best_ask": "14419",
            "best_bid": "14209",
            "mark_iv": "0.33716195",
            "bid_iv": "0.32418372",
            "ask_iv": "0.35081119",
            "ask_size": "4080",
            "bid_size": "6085",
        },
        "greeks": {
            "delta": "-0.84795376",
            "theta": "-21.02636032",
            "gamma": "0.00002074",
            "rho": "-143.61147823",
            "vega": "78.99989858",
        },
        "oi_contracts": "261",
        "volume": 0.031,
        "timestamp": 1787878544804095,
        "product_trading_status": "operational",
    }


def test_parses_delta_option_ticker_without_float_loss() -> None:
    ticker = DeltaOptionTicker.from_api(_sample_ticker())

    assert ticker.product_id == 149236
    assert ticker.symbol == "P-BTC-94000-301026"
    assert ticker.expiry == date(2026, 10, 30)
    assert ticker.strike_price == Decimal("94000")
    assert ticker.best_bid == Decimal("14209")
    assert ticker.best_ask == Decimal("14419")
    assert ticker.spread == Decimal("210")
    assert ticker.spread_fraction == Decimal("210") / Decimal("14419")
    assert ticker.delta == Decimal("-0.84795376")
    assert ticker.has_tradeable_quote is True


def test_missing_bid_is_not_tradeable() -> None:
    raw = _sample_ticker()
    quotes = raw["quotes"]
    assert isinstance(quotes, dict)
    quotes["best_bid"] = None

    ticker = DeltaOptionTicker.from_api(raw)

    assert ticker.spread is None
    assert ticker.has_tradeable_quote is False


def test_rejects_unknown_contract_type() -> None:
    raw = _sample_ticker()
    raw["contract_type"] = "move_options"

    with pytest.raises(ValueError, match="Unsupported contract_type"):
        DeltaOptionTicker.from_api(raw)


def test_null_volume_is_preserved_without_rejecting_quote() -> None:
    raw = _sample_ticker()
    raw["volume"] = None

    ticker = DeltaOptionTicker.from_api(raw)

    assert ticker.volume is None
    assert ticker.has_tradeable_quote is True
