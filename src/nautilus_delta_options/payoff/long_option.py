from dataclasses import dataclass
from decimal import Decimal

from nautilus_delta_options.payoff.fees import (
    OptionFeeBreakdown,
    OptionFeeSchedule,
    calculate_option_trade_fee,
)


@dataclass(frozen=True, slots=True)
class LongOptionRoundTrip:
    underlying_quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_premium: Decimal
    gross_pnl: Decimal
    entry_fee: OptionFeeBreakdown
    exit_fee: OptionFeeBreakdown
    total_fees: Decimal
    net_pnl: Decimal
    return_on_entry_cost: Decimal


@dataclass(frozen=True, slots=True)
class LongOptionPayoffPlan:
    stop_outcome: LongOptionRoundTrip
    target_outcome: LongOptionRoundTrip
    planned_loss: Decimal
    planned_reward: Decimal
    reward_risk_ratio: Decimal
    minimum_reward_risk: Decimal
    approved: bool


def calculate_long_option_round_trip(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    entry_spot: Decimal,
    exit_spot: Decimal,
    contracts: Decimal,
    contract_value: Decimal,
    fee_schedule: OptionFeeSchedule | None = None,
) -> LongOptionRoundTrip:
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if exit_price < 0:
        raise ValueError("exit_price cannot be negative")

    underlying_quantity = contracts * contract_value
    entry_premium = entry_price * underlying_quantity

    entry_fee = calculate_option_trade_fee(
        spot_price=entry_spot,
        option_price=entry_price,
        contracts=contracts,
        contract_value=contract_value,
        schedule=fee_schedule,
    )
    exit_fee = calculate_option_trade_fee(
        spot_price=exit_spot,
        option_price=exit_price,
        contracts=contracts,
        contract_value=contract_value,
        schedule=fee_schedule,
    )

    gross_pnl = (exit_price - entry_price) * underlying_quantity
    total_fees = entry_fee.total_fee + exit_fee.total_fee
    net_pnl = gross_pnl - total_fees
    entry_cost = entry_premium + entry_fee.total_fee

    return LongOptionRoundTrip(
        underlying_quantity=underlying_quantity,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_premium=entry_premium,
        gross_pnl=gross_pnl,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        total_fees=total_fees,
        net_pnl=net_pnl,
        return_on_entry_cost=net_pnl / entry_cost,
    )


def evaluate_long_option_payoff_plan(
    *,
    entry_ask: Decimal,
    stop_exit_bid: Decimal,
    target_exit_bid: Decimal,
    entry_spot: Decimal,
    stop_spot: Decimal,
    target_spot: Decimal,
    contracts: Decimal,
    contract_value: Decimal,
    minimum_reward_risk: Decimal,
    fee_schedule: OptionFeeSchedule | None = None,
) -> LongOptionPayoffPlan:
    if minimum_reward_risk <= 0:
        raise ValueError("minimum_reward_risk must be positive")

    stop_outcome = calculate_long_option_round_trip(
        entry_price=entry_ask,
        exit_price=stop_exit_bid,
        entry_spot=entry_spot,
        exit_spot=stop_spot,
        contracts=contracts,
        contract_value=contract_value,
        fee_schedule=fee_schedule,
    )
    target_outcome = calculate_long_option_round_trip(
        entry_price=entry_ask,
        exit_price=target_exit_bid,
        entry_spot=entry_spot,
        exit_spot=target_spot,
        contracts=contracts,
        contract_value=contract_value,
        fee_schedule=fee_schedule,
    )

    planned_loss = -stop_outcome.net_pnl
    planned_reward = target_outcome.net_pnl

    if planned_loss <= 0:
        raise ValueError("stop outcome must produce a net loss")
    if planned_reward <= 0:
        raise ValueError("target outcome must produce a net profit")

    reward_risk_ratio = planned_reward / planned_loss

    return LongOptionPayoffPlan(
        stop_outcome=stop_outcome,
        target_outcome=target_outcome,
        planned_loss=planned_loss,
        planned_reward=planned_reward,
        reward_risk_ratio=reward_risk_ratio,
        minimum_reward_risk=minimum_reward_risk,
        approved=reward_risk_ratio >= minimum_reward_risk,
    )
