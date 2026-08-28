from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OptionFeeSchedule:
    notional_rate: Decimal = Decimal("0.0001")
    premium_cap_rate: Decimal = Decimal("0.035")
    gst_rate: Decimal = Decimal("0.18")


@dataclass(frozen=True, slots=True)
class OptionFeeBreakdown:
    notional: Decimal
    premium: Decimal
    uncapped_fee: Decimal
    premium_cap: Decimal
    fee_before_gst: Decimal
    gst: Decimal
    total_fee: Decimal


def calculate_option_trade_fee(
    *,
    spot_price: Decimal,
    option_price: Decimal,
    contracts: Decimal,
    contract_value: Decimal,
    schedule: OptionFeeSchedule | None = None,
) -> OptionFeeBreakdown:
    if schedule is None:
        schedule = OptionFeeSchedule()
    if spot_price <= 0:
        raise ValueError("spot_price must be positive")
    if option_price < 0:
        raise ValueError("option_price cannot be negative")
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    if contract_value <= 0:
        raise ValueError("contract_value must be positive")

    underlying_quantity = contracts * contract_value
    notional = spot_price * underlying_quantity
    premium = option_price * underlying_quantity

    uncapped_fee = notional * schedule.notional_rate
    premium_cap = premium * schedule.premium_cap_rate
    fee_before_gst = min(uncapped_fee, premium_cap)
    gst = fee_before_gst * schedule.gst_rate

    return OptionFeeBreakdown(
        notional=notional,
        premium=premium,
        uncapped_fee=uncapped_fee,
        premium_cap=premium_cap,
        fee_before_gst=fee_before_gst,
        gst=gst,
        total_fee=fee_before_gst + gst,
    )
