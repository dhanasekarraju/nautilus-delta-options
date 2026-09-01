from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nautilus_delta_options.delta.snapshot import DeltaOptionMarketRecord
from nautilus_delta_options.paper.ledger import PaperPosition

_NANOSECONDS_PER_MINUTE = 60 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class PaperExitPolicyConfig:
    max_hold_minutes: int = 240
    close_before_settlement_minutes: int = 120

    def __post_init__(self) -> None:
        if self.max_hold_minutes <= 0:
            raise ValueError("max_hold_minutes must be positive")
        if self.close_before_settlement_minutes <= 0:
            raise ValueError("close_before_settlement_minutes must be positive")


def paper_time_exit_due(
    position: PaperPosition,
    record: DeltaOptionMarketRecord,
    *,
    observed_ns: int,
    config: PaperExitPolicyConfig | None = None,
) -> bool:
    if observed_ns <= 0:
        raise ValueError("observed_ns must be positive")
    if record.product.product_id != position.product_id:
        raise ValueError("Exit record does not match the position")

    resolved = config or PaperExitPolicyConfig()
    max_hold_ns = resolved.max_hold_minutes * _NANOSECONDS_PER_MINUTE
    settlement_buffer_ns = resolved.close_before_settlement_minutes * _NANOSECONDS_PER_MINUTE
    settlement_ns = _datetime_to_unix_ns(record.product.settlement_time)

    return (
        observed_ns - position.opened_ns >= max_hold_ns
        or settlement_ns - observed_ns <= settlement_buffer_ns
    )


def _datetime_to_unix_ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("settlement_time must include timezone information")

    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = utc_value - epoch
    return (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000_000 + elapsed.microseconds * 1_000
