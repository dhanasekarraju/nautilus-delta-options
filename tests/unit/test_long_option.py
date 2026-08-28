from decimal import Decimal

from nautilus_delta_options.payoff.long_option import (
    calculate_long_option_round_trip,
    evaluate_long_option_payoff_plan,
)


def test_profitable_round_trip_deducts_both_fees() -> None:
    result = calculate_long_option_round_trip(
        entry_price=Decimal("1000"),
        exit_price=Decimal("1150"),
        entry_spot=Decimal("80000"),
        exit_spot=Decimal("80800"),
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
    )

    assert result.underlying_quantity == Decimal("0.010")
    assert result.entry_premium == Decimal("10.000")
    assert result.gross_pnl == Decimal("1.500")
    assert result.entry_fee.total_fee == Decimal("0.094400000")
    assert result.exit_fee.total_fee == Decimal("0.095344000")
    assert result.total_fees == Decimal("0.189744000")
    assert result.net_pnl == Decimal("1.310256000")
    assert result.return_on_entry_cost > Decimal("0.12")


def test_crossing_spread_produces_net_loss() -> None:
    result = calculate_long_option_round_trip(
        entry_price=Decimal("1000"),
        exit_price=Decimal("980"),
        entry_spot=Decimal("80000"),
        exit_spot=Decimal("80000"),
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
    )

    assert result.gross_pnl == Decimal("-0.200")
    assert result.total_fees == Decimal("0.188800000")
    assert result.net_pnl == Decimal("-0.388800000")


def test_payoff_gate_uses_net_reward_and_net_loss() -> None:
    plan = evaluate_long_option_payoff_plan(
        entry_ask=Decimal("1000"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        entry_spot=Decimal("80000"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
        minimum_reward_risk=Decimal("1.5"),
    )

    assert plan.planned_loss == Decimal("1.188210000")
    assert plan.planned_reward == Decimal("1.810020000")
    assert plan.reward_risk_ratio > Decimal("1.52")
    assert plan.approved is True


def test_payoff_gate_rejects_insufficient_ratio() -> None:
    plan = evaluate_long_option_payoff_plan(
        entry_ask=Decimal("1000"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        entry_spot=Decimal("80000"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
        minimum_reward_risk=Decimal("1.6"),
    )

    assert plan.reward_risk_ratio < Decimal("1.6")
    assert plan.approved is False
