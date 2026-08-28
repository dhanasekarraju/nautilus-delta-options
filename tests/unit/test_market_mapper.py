from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.market_mapper import (
    map_delta_ticker_to_option_greeks,
    map_delta_ticker_to_quote_tick,
)
from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.nautilus_mapper import (
    map_delta_product_to_crypto_option,
)
from nautilus_delta_options.delta.product import DeltaOptionProduct

TIMESTAMP_US = 1_787_878_544_804_095
TIMESTAMP_NS = TIMESTAMP_US * 1_000


def _instrument():
    product = DeltaOptionProduct(
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
    return map_delta_product_to_crypto_option(
        product,
        ts_event_ns=TIMESTAMP_NS,
        ts_init_ns=TIMESTAMP_NS,
    )


def _ticker() -> DeltaOptionTicker:
    return DeltaOptionTicker(
        product_id=149236,
        symbol="P-BTC-94000-301026",
        underlying="BTC",
        contract_type="put_options",
        strike_price=Decimal("94000"),
        expiry=date(2026, 10, 30),
        mark_price=Decimal("14315.3"),
        spot_price=Decimal("80521.7"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal("14209"),
        best_ask=Decimal("14419"),
        bid_size=Decimal("6085"),
        ask_size=Decimal("4080"),
        mark_iv=Decimal("0.3371"),
        bid_iv=Decimal("0.3241"),
        ask_iv=Decimal("0.3508"),
        delta=Decimal("-0.8479"),
        gamma=Decimal("0.00002074"),
        theta=Decimal("-21.0263"),
        rho=Decimal("-143.6114"),
        vega=Decimal("78.9998"),
        open_interest_contracts=Decimal("261"),
        volume=Decimal("0.031"),
        exchange_timestamp=TIMESTAMP_US,
        trading_status="operational",
    )


def test_maps_executable_bid_and_ask_to_quote_tick() -> None:
    quote = map_delta_ticker_to_quote_tick(
        _ticker(),
        _instrument(),
        ts_init_ns=TIMESTAMP_NS + 1,
    )

    assert str(quote.instrument_id) == "P-BTC-94000-301026.DELTA"
    assert str(quote.bid_price) == "14209.0"
    assert str(quote.ask_price) == "14419.0"
    assert str(quote.bid_size) == "6085"
    assert str(quote.ask_size) == "4080"
    assert quote.ts_event == TIMESTAMP_NS
    assert quote.ts_init == TIMESTAMP_NS + 1


def test_maps_exchange_supplied_greeks() -> None:
    greeks = map_delta_ticker_to_option_greeks(
        _ticker(),
        _instrument(),
        ts_init_ns=TIMESTAMP_NS + 1,
    )

    assert greeks.delta == pytest.approx(-0.8479)
    assert greeks.gamma == pytest.approx(0.00002074)
    assert greeks.vega == pytest.approx(78.9998)
    assert greeks.theta == pytest.approx(-21.0263)
    assert greeks.rho == pytest.approx(-143.6114)
    assert greeks.mark_iv == pytest.approx(0.3371)
    assert greeks.underlying_price == pytest.approx(80521.7)
    assert greeks.open_interest == pytest.approx(261)


def test_rejects_quote_with_missing_bid() -> None:
    ticker = replace(_ticker(), best_bid=None)

    with pytest.raises(ValueError, match="tradeable quote"):
        map_delta_ticker_to_quote_tick(
            ticker,
            _instrument(),
            ts_init_ns=TIMESTAMP_NS + 1,
        )


def test_rejects_symbol_mismatch() -> None:
    ticker = replace(_ticker(), symbol="P-BTC-92000-301026")

    with pytest.raises(ValueError, match="does not match"):
        map_delta_ticker_to_option_greeks(
            ticker,
            _instrument(),
            ts_init_ns=TIMESTAMP_NS + 1,
        )
