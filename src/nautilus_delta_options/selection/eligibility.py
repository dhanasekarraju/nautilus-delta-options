from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from nautilus_delta_options.delta.models import DeltaOptionTicker


class EligibilityReason(StrEnum):
    NOT_OPERATIONAL = "not_operational"
    DTE_BELOW_MINIMUM = "dte_below_minimum"
    DTE_ABOVE_MAXIMUM = "dte_above_maximum"
    MISSING_TWO_SIDED_QUOTE = "missing_two_sided_quote"
    INVALID_QUOTE = "invalid_quote"
    INSUFFICIENT_QUOTE_DEPTH = "insufficient_quote_depth"
    SPREAD_TOO_WIDE = "spread_too_wide"
    MISSING_VOLUME = "missing_volume"
    INSUFFICIENT_VOLUME = "insufficient_volume"
    INSUFFICIENT_OPEN_INTEREST = "insufficient_open_interest"


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    min_dte: int = 1
    max_dte: int = 7
    max_spread_fraction: Decimal = Decimal("0.025")
    min_volume: Decimal = Decimal("0")
    min_open_interest_contracts: Decimal = Decimal("1")
    min_quote_size: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.min_dte < 0:
            raise ValueError("min_dte cannot be negative")
        if self.max_dte < self.min_dte:
            raise ValueError("max_dte cannot be below min_dte")
        if self.max_spread_fraction <= 0:
            raise ValueError("max_spread_fraction must be positive")
        if self.min_volume < 0:
            raise ValueError("min_volume cannot be negative")
        if self.min_open_interest_contracts < 0:
            raise ValueError("min_open_interest_contracts cannot be negative")
        if self.min_quote_size <= 0:
            raise ValueError("min_quote_size must be positive")


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    dte: int
    reasons: tuple[EligibilityReason, ...]


def evaluate_market_eligibility(
    ticker: DeltaOptionTicker,
    *,
    as_of: date,
    config: EligibilityConfig,
) -> EligibilityResult:
    reasons: list[EligibilityReason] = []
    dte = (ticker.expiry - as_of).days

    if ticker.trading_status != "operational":
        reasons.append(EligibilityReason.NOT_OPERATIONAL)

    if dte < config.min_dte:
        reasons.append(EligibilityReason.DTE_BELOW_MINIMUM)
    elif dte > config.max_dte:
        reasons.append(EligibilityReason.DTE_ABOVE_MAXIMUM)

    quotes_present = ticker.best_bid is not None and ticker.best_ask is not None
    if not quotes_present:
        reasons.append(EligibilityReason.MISSING_TWO_SIDED_QUOTE)
    elif (
        ticker.best_bid is not None
        and ticker.best_ask is not None
        and (ticker.best_bid <= 0 or ticker.best_ask < ticker.best_bid)
    ):
        reasons.append(EligibilityReason.INVALID_QUOTE)
    elif ticker.spread_fraction is not None and ticker.spread_fraction > config.max_spread_fraction:
        reasons.append(EligibilityReason.SPREAD_TOO_WIDE)

    if (
        ticker.bid_size is None
        or ticker.ask_size is None
        or ticker.bid_size < config.min_quote_size
        or ticker.ask_size < config.min_quote_size
    ):
        reasons.append(EligibilityReason.INSUFFICIENT_QUOTE_DEPTH)

    if ticker.volume is None:
        reasons.append(EligibilityReason.MISSING_VOLUME)
    elif ticker.volume <= config.min_volume:
        reasons.append(EligibilityReason.INSUFFICIENT_VOLUME)

    if ticker.open_interest_contracts < config.min_open_interest_contracts:
        reasons.append(EligibilityReason.INSUFFICIENT_OPEN_INTEREST)

    return EligibilityResult(
        eligible=not reasons,
        dte=dte,
        reasons=tuple(reasons),
    )
