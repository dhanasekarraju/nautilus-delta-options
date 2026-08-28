from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from nautilus_delta_options.delta.snapshot import DeltaOptionMarketRecord
from nautilus_delta_options.payoff.fees import OptionFeeSchedule
from nautilus_delta_options.payoff.long_option import (
    evaluate_long_option_payoff_plan,
)


class SizingLimit(StrEnum):
    PAYOFF = "payoff"
    RISK = "risk"
    PREMIUM = "premium"
    ASK_DEPTH = "ask_depth"
    PRODUCT_LIMIT = "product_limit"
    ABSOLUTE_LIMIT = "absolute_limit"
    NO_CAPACITY = "no_capacity"


@dataclass(frozen=True, slots=True)
class PositionSizingConfig:
    risk_fraction: Decimal = Decimal("0.02")
    max_premium_fraction: Decimal = Decimal("0.20")
    max_contracts_per_trade: Decimal = Decimal("1000")

    def __post_init__(self) -> None:
        if not Decimal("0") < self.risk_fraction <= Decimal("1"):
            raise ValueError("risk_fraction must be within (0, 1]")
        if not Decimal("0") < self.max_premium_fraction <= Decimal("1"):
            raise ValueError(
                "max_premium_fraction must be within (0, 1]",
            )
        if (
            self.max_contracts_per_trade <= 0
            or self.max_contracts_per_trade != self.max_contracts_per_trade.to_integral_value()
        ):
            raise ValueError(
                "max_contracts_per_trade must be a positive whole number",
            )


@dataclass(frozen=True, slots=True)
class PositionSizingDecision:
    approved: bool
    contracts: Decimal
    limiting_factor: SizingLimit
    risk_budget: Decimal
    premium_budget: Decimal
    planned_loss_per_contract: Decimal
    planned_reward_per_contract: Decimal
    entry_debit_per_contract: Decimal
    total_planned_loss: Decimal
    total_planned_reward: Decimal
    total_entry_debit: Decimal
    reward_risk_ratio: Decimal


def size_long_option_position(
    record: DeltaOptionMarketRecord,
    *,
    account_equity: Decimal,
    available_cash: Decimal,
    stop_exit_bid: Decimal,
    target_exit_bid: Decimal,
    stop_spot: Decimal,
    target_spot: Decimal,
    minimum_reward_risk: Decimal,
    gst_rate: Decimal,
    config: PositionSizingConfig,
) -> PositionSizingDecision:
    if account_equity <= 0:
        raise ValueError("account_equity must be positive")
    if available_cash < 0:
        raise ValueError("available_cash cannot be negative")
    if minimum_reward_risk <= 0:
        raise ValueError("minimum_reward_risk must be positive")
    if gst_rate < 0:
        raise ValueError("gst_rate cannot be negative")
    if not record.eligibility.eligible:
        raise ValueError("Market record is not eligible")
    if record.quote is None:
        raise ValueError("Market record has no executable quote")

    ticker = record.ticker

    if ticker.best_ask is None or ticker.ask_size is None:
        raise ValueError("Ticker is missing executable ask data")

    fee_schedule = OptionFeeSchedule(
        notional_rate=record.product.taker_fee,
        premium_cap_rate=record.product.premium_cap_rate,
        gst_rate=gst_rate,
    )
    payoff = evaluate_long_option_payoff_plan(
        entry_ask=ticker.best_ask,
        stop_exit_bid=stop_exit_bid,
        target_exit_bid=target_exit_bid,
        entry_spot=ticker.spot_price,
        stop_spot=stop_spot,
        target_spot=target_spot,
        contracts=Decimal("1"),
        contract_value=ticker.contract_value,
        minimum_reward_risk=minimum_reward_risk,
        fee_schedule=fee_schedule,
    )

    risk_budget = account_equity * config.risk_fraction
    premium_budget = min(
        account_equity * config.max_premium_fraction,
        available_cash,
    )
    entry_debit_per_contract = (
        ticker.best_ask * ticker.contract_value + payoff.stop_outcome.entry_fee.total_fee
    )

    if not payoff.approved:
        return _rejected_decision(
            limiting_factor=SizingLimit.PAYOFF,
            risk_budget=risk_budget,
            premium_budget=premium_budget,
            payoff_loss=payoff.planned_loss,
            payoff_reward=payoff.planned_reward,
            entry_debit=entry_debit_per_contract,
            reward_risk_ratio=payoff.reward_risk_ratio,
        )

    capacity = {
        SizingLimit.RISK: _whole_contracts(
            risk_budget,
            payoff.planned_loss,
        ),
        SizingLimit.PREMIUM: _whole_contracts(
            premium_budget,
            entry_debit_per_contract,
        ),
        SizingLimit.ASK_DEPTH: _whole_value(ticker.ask_size),
        SizingLimit.PRODUCT_LIMIT: _whole_value(
            record.product.position_size_limit,
        ),
        SizingLimit.ABSOLUTE_LIMIT: _whole_value(
            config.max_contracts_per_trade,
        ),
    }

    limiting_factor, contract_count = min(
        capacity.items(),
        key=lambda item: item[1],
    )

    if contract_count < 1:
        return _rejected_decision(
            limiting_factor=SizingLimit.NO_CAPACITY,
            risk_budget=risk_budget,
            premium_budget=premium_budget,
            payoff_loss=payoff.planned_loss,
            payoff_reward=payoff.planned_reward,
            entry_debit=entry_debit_per_contract,
            reward_risk_ratio=payoff.reward_risk_ratio,
        )

    contracts = Decimal(contract_count)

    return PositionSizingDecision(
        approved=True,
        contracts=contracts,
        limiting_factor=limiting_factor,
        risk_budget=risk_budget,
        premium_budget=premium_budget,
        planned_loss_per_contract=payoff.planned_loss,
        planned_reward_per_contract=payoff.planned_reward,
        entry_debit_per_contract=entry_debit_per_contract,
        total_planned_loss=payoff.planned_loss * contracts,
        total_planned_reward=payoff.planned_reward * contracts,
        total_entry_debit=entry_debit_per_contract * contracts,
        reward_risk_ratio=payoff.reward_risk_ratio,
    )


def _whole_contracts(budget: Decimal, per_contract: Decimal) -> int:
    if per_contract <= 0:
        raise ValueError("per_contract amount must be positive")
    return int(
        (budget / per_contract).to_integral_value(
            rounding=ROUND_FLOOR,
        ),
    )


def _whole_value(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _rejected_decision(
    *,
    limiting_factor: SizingLimit,
    risk_budget: Decimal,
    premium_budget: Decimal,
    payoff_loss: Decimal,
    payoff_reward: Decimal,
    entry_debit: Decimal,
    reward_risk_ratio: Decimal,
) -> PositionSizingDecision:
    return PositionSizingDecision(
        approved=False,
        contracts=Decimal("0"),
        limiting_factor=limiting_factor,
        risk_budget=risk_budget,
        premium_budget=premium_budget,
        planned_loss_per_contract=payoff_loss,
        planned_reward_per_contract=payoff_reward,
        entry_debit_per_contract=entry_debit,
        total_planned_loss=Decimal("0"),
        total_planned_reward=Decimal("0"),
        total_entry_debit=Decimal("0"),
        reward_risk_ratio=reward_risk_ratio,
    )
