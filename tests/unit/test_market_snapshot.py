from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.product import DeltaOptionProduct
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
)
from nautilus_delta_options.delta.snapshot import build_market_snapshot
from nautilus_delta_options.selection.eligibility import EligibilityConfig

AS_OF = date(2026, 8, 28)
TIMESTAMP_US = 1_787_878_544_804_095
TIMESTAMP_NS = TIMESTAMP_US * 1_000 + 1


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


def _catalog(product: DeltaOptionProduct) -> DeltaOptionProductsSnapshot:
    return DeltaOptionProductsSnapshot(
        underlying="BTC",
        products=(product,),
        rejected_records=(),
    )


def _chain(ticker: DeltaOptionTicker) -> DeltaOptionChainSnapshot:
    return DeltaOptionChainSnapshot(
        underlying="BTC",
        tickers=(ticker,),
        rejected_records=(),
    )


def test_builds_joined_market_snapshot() -> None:
    snapshot = build_market_snapshot(
        catalog=_catalog(_product()),
        chain=_chain(_ticker()),
        as_of=AS_OF,
        captured_ns=TIMESTAMP_NS,
        eligibility_config=EligibilityConfig(
            min_dte=1,
            max_dte=90,
        ),
    )

    assert snapshot.product_count == 1
    assert snapshot.ticker_count == 1
    assert len(snapshot.records) == 1
    assert len(snapshot.eligible_records) == 1
    assert snapshot.errors == ()
    assert snapshot.unmatched_product_ids == ()

    record = snapshot.records[0]
    assert str(record.instrument.id) == "P-BTC-94000-301026.DELTA"
    assert str(record.quote.bid_price) == "14209.0"
    assert record.greeks.delta == pytest.approx(-0.8479)


def test_rejects_product_ticker_symbol_mismatch() -> None:
    ticker = replace(_ticker(), symbol="P-BTC-92000-301026")

    snapshot = build_market_snapshot(
        catalog=_catalog(_product()),
        chain=_chain(ticker),
        as_of=AS_OF,
        captured_ns=TIMESTAMP_NS,
        eligibility_config=EligibilityConfig(),
    )

    assert snapshot.records == ()
    assert snapshot.errors == ("P-BTC-92000-301026: symbol mismatch",)


def test_rejects_underlying_snapshot_mismatch() -> None:
    chain = replace(_chain(_ticker()), underlying="ETH")

    with pytest.raises(ValueError, match="underlyings must match"):
        build_market_snapshot(
            catalog=_catalog(_product()),
            chain=chain,
            as_of=AS_OF,
            captured_ns=TIMESTAMP_NS,
            eligibility_config=EligibilityConfig(),
        )
