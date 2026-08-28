from dataclasses import replace
from datetime import date
from decimal import Decimal

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.selection.eligibility import (
    EligibilityConfig,
    EligibilityReason,
    evaluate_market_eligibility,
)

AS_OF = date(2026, 8, 28)


def _ticker() -> DeltaOptionTicker:
    return DeltaOptionTicker(
        product_id=1,
        symbol="C-BTC-80000-300826",
        underlying="BTC",
        contract_type="call_options",
        strike_price=Decimal("80000"),
        expiry=date(2026, 8, 30),
        mark_price=Decimal("99"),
        spot_price=Decimal("80000"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal("98"),
        best_ask=Decimal("100"),
        bid_size=Decimal("100"),
        ask_size=Decimal("100"),
        mark_iv=Decimal("0.5"),
        bid_iv=Decimal("0.49"),
        ask_iv=Decimal("0.51"),
        delta=Decimal("0.5"),
        gamma=Decimal("0.0001"),
        theta=Decimal("-10"),
        rho=Decimal("1"),
        vega=Decimal("20"),
        open_interest_contracts=Decimal("100"),
        volume=Decimal("10"),
        exchange_timestamp=1,
        trading_status="operational",
    )


def test_eligible_market_passes_all_quality_checks() -> None:
    result = evaluate_market_eligibility(
        _ticker(),
        as_of=AS_OF,
        config=EligibilityConfig(),
    )

    assert result.eligible is True
    assert result.dte == 2
    assert result.reasons == ()


def test_wide_spread_is_rejected() -> None:
    ticker = replace(
        _ticker(),
        best_bid=Decimal("95"),
        best_ask=Decimal("100"),
    )

    result = evaluate_market_eligibility(
        ticker,
        as_of=AS_OF,
        config=EligibilityConfig(),
    )

    assert result.eligible is False
    assert EligibilityReason.SPREAD_TOO_WIDE in result.reasons


def test_missing_activity_and_same_day_expiry_are_rejected() -> None:
    ticker = replace(
        _ticker(),
        expiry=AS_OF,
        volume=None,
        open_interest_contracts=Decimal("0"),
    )

    result = evaluate_market_eligibility(
        ticker,
        as_of=AS_OF,
        config=EligibilityConfig(),
    )

    assert result.eligible is False
    assert EligibilityReason.DTE_BELOW_MINIMUM in result.reasons
    assert EligibilityReason.MISSING_VOLUME in result.reasons
    assert EligibilityReason.INSUFFICIENT_OPEN_INTEREST in result.reasons
