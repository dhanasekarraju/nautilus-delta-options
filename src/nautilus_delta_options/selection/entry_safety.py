from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from nautilus_delta_options.delta.snapshot import DeltaOptionMarketRecord

_NANOSECONDS_PER_SECOND = 1_000_000_000
_NANOSECONDS_PER_HOUR = 3_600 * _NANOSECONDS_PER_SECOND


class EntrySafetyReason(StrEnum):
    MISSING_DELTA = "missing_delta"
    DELTA_OUT_OF_RANGE = "delta_out_of_range"
    MONEYNESS_TOO_WIDE = "moneyness_too_wide"
    TOO_CLOSE_TO_SETTLEMENT = "too_close_to_settlement"
    STALE_QUOTE = "stale_quote"
    FUTURE_QUOTE = "future_quote"


@dataclass(frozen=True, slots=True)
class EntrySafetyConfig:
    min_abs_delta: Decimal = Decimal("0.35")
    max_abs_delta: Decimal = Decimal("0.60")
    max_moneyness_fraction: Decimal = Decimal("0.05")
    min_hours_to_settlement: Decimal = Decimal("36")
    max_quote_age_seconds: Decimal = Decimal("15")
    max_signal_age_seconds: Decimal = Decimal("360")
    max_future_skew_seconds: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        decimal_fields = (
            ("min_abs_delta", self.min_abs_delta),
            ("max_abs_delta", self.max_abs_delta),
            ("max_moneyness_fraction", self.max_moneyness_fraction),
            ("min_hours_to_settlement", self.min_hours_to_settlement),
            ("max_quote_age_seconds", self.max_quote_age_seconds),
            ("max_signal_age_seconds", self.max_signal_age_seconds),
            ("max_future_skew_seconds", self.max_future_skew_seconds),
        )

        for name, value in decimal_fields:
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")

        if not Decimal("0") < self.min_abs_delta <= Decimal("1"):
            raise ValueError("min_abs_delta must be within (0, 1]")
        if not self.min_abs_delta <= self.max_abs_delta <= Decimal("1"):
            raise ValueError("max_abs_delta must be between min_abs_delta and 1")
        if not Decimal("0") < self.max_moneyness_fraction < Decimal("1"):
            raise ValueError("max_moneyness_fraction must be within (0, 1)")
        if self.min_hours_to_settlement <= 0:
            raise ValueError("min_hours_to_settlement must be positive")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.max_signal_age_seconds <= 0:
            raise ValueError("max_signal_age_seconds must be positive")
        if self.max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class EntrySafetyResult:
    approved: bool
    reasons: tuple[EntrySafetyReason, ...]
    abs_delta: Decimal | None
    moneyness_fraction: Decimal
    hours_to_settlement: Decimal
    quote_age_seconds: Decimal


def evaluate_option_entry_safety(
    record: DeltaOptionMarketRecord,
    *,
    observed_ns: int,
    config: EntrySafetyConfig | None = None,
) -> EntrySafetyResult:
    if observed_ns <= 0:
        raise ValueError("observed_ns must be positive")

    resolved = config or EntrySafetyConfig()
    ticker = record.ticker

    if ticker.spot_price <= 0:
        raise ValueError("spot_price must be positive")

    reasons: list[EntrySafetyReason] = []
    abs_delta: Decimal | None = None

    if ticker.delta is None or not ticker.delta.is_finite():
        reasons.append(EntrySafetyReason.MISSING_DELTA)
    else:
        abs_delta = abs(ticker.delta)
        if not resolved.min_abs_delta <= abs_delta <= resolved.max_abs_delta:
            reasons.append(EntrySafetyReason.DELTA_OUT_OF_RANGE)

    moneyness_fraction = abs(ticker.strike_price - ticker.spot_price) / ticker.spot_price
    if moneyness_fraction > resolved.max_moneyness_fraction:
        reasons.append(EntrySafetyReason.MONEYNESS_TOO_WIDE)

    settlement_ns = _datetime_to_unix_ns(record.product.settlement_time)
    hours_to_settlement = Decimal(settlement_ns - observed_ns) / Decimal(_NANOSECONDS_PER_HOUR)
    if hours_to_settlement < resolved.min_hours_to_settlement:
        reasons.append(EntrySafetyReason.TOO_CLOSE_TO_SETTLEMENT)

    quote_event_ns = ticker.exchange_timestamp * 1_000
    quote_age_seconds = Decimal(observed_ns - quote_event_ns) / Decimal(_NANOSECONDS_PER_SECOND)
    if quote_age_seconds < -resolved.max_future_skew_seconds:
        reasons.append(EntrySafetyReason.FUTURE_QUOTE)
    elif quote_age_seconds > resolved.max_quote_age_seconds:
        reasons.append(EntrySafetyReason.STALE_QUOTE)

    return EntrySafetyResult(
        approved=not reasons,
        reasons=tuple(reasons),
        abs_delta=abs_delta,
        moneyness_fraction=moneyness_fraction,
        hours_to_settlement=hours_to_settlement,
        quote_age_seconds=quote_age_seconds,
    )


def signal_is_fresh(
    *,
    candle_close_ms: int,
    observed_ns: int,
    config: EntrySafetyConfig | None = None,
) -> bool:
    if candle_close_ms <= 0:
        raise ValueError("candle_close_ms must be positive")
    if observed_ns <= 0:
        raise ValueError("observed_ns must be positive")

    resolved = config or EntrySafetyConfig()
    candle_close_ns = candle_close_ms * 1_000_000
    age_seconds = Decimal(observed_ns - candle_close_ns) / Decimal(_NANOSECONDS_PER_SECOND)
    return -resolved.max_future_skew_seconds <= age_seconds <= resolved.max_signal_age_seconds


def _datetime_to_unix_ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("settlement_time must include timezone information")

    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = utc_value - epoch
    return (
        elapsed.days * 86_400 + elapsed.seconds
    ) * _NANOSECONDS_PER_SECOND + elapsed.microseconds * 1_000
