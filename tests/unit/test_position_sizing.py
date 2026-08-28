from decimal import Decimal
from types import SimpleNamespace

from nautilus_delta_options.paper.sizing import (
    PositionSizingConfig,
    SizingLimit,
    size_long_option_position,
)


def _record():
    ticker = SimpleNamespace(
        best_ask=Decimal("1000"),
        ask_size=Decimal("1000"),
        spot_price=Decimal("80000"),
        contract_value=Decimal("0.001"),
    )
    product = SimpleNamespace(
        taker_fee=Decimal("0.0001"),
        premium_cap_rate=Decimal("0.035"),
        position_size_limit=Decimal("50000"),
    )
    eligibility = SimpleNamespace(eligible=True)

    return SimpleNamespace(
        ticker=ticker,
        product=product,
        eligibility=eligibility,
        quote=object(),
    )


def test_risk_budget_limits_position_to_42_contracts() -> None:
    decision = size_long_option_position(
        _record(),
        account_equity=Decimal("250"),
        available_cash=Decimal("250"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
        minimum_reward_risk=Decimal("1.5"),
        gst_rate=Decimal("0.18"),
        config=PositionSizingConfig(),
    )

    assert decision.approved is True
    assert decision.contracts == Decimal("42")
    assert decision.limiting_factor == SizingLimit.RISK
    assert decision.risk_budget == Decimal("5.00")
    assert decision.total_planned_loss <= Decimal("5.00")
    assert decision.reward_risk_ratio > Decimal("1.52")


def test_payoff_gate_rejects_position_before_sizing() -> None:
    decision = size_long_option_position(
        _record(),
        account_equity=Decimal("250"),
        available_cash=Decimal("250"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
        minimum_reward_risk=Decimal("1.6"),
        gst_rate=Decimal("0.18"),
        config=PositionSizingConfig(),
    )

    assert decision.approved is False
    assert decision.contracts == Decimal("0")
    assert decision.limiting_factor == SizingLimit.PAYOFF


def test_premium_budget_can_limit_position() -> None:
    decision = size_long_option_position(
        _record(),
        account_equity=Decimal("250"),
        available_cash=Decimal("250"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
        minimum_reward_risk=Decimal("1.5"),
        gst_rate=Decimal("0.18"),
        config=PositionSizingConfig(
            risk_fraction=Decimal("0.50"),
            max_premium_fraction=Decimal("0.01"),
        ),
    )

    assert decision.approved is True
    assert decision.contracts == Decimal("2")
    assert decision.limiting_factor == SizingLimit.PREMIUM
