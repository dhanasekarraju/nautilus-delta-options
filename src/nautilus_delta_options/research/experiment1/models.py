from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Asset = Literal["BTC", "ETH"]

_RESOLUTION_NS = 300_000_000_000
_HORIZON_NS = 1_800_000_000_000

_SOURCE_SYMBOL_BY_ASSET = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
}


class JournalEventType(StrEnum):
    RUN_STARTED = "run_started"
    EVALUATION_STARTED = "evaluation_started"
    EVALUATION_COMPLETED = "evaluation_completed"
    EVALUATION_FAILED = "evaluation_failed"
    EVALUATION_SKIPPED = "evaluation_skipped"
    COLLECTION_FAULT = "collection_fault"
    RUN_STOPPED = "run_stopped"
    GAP_ASSESSMENT = "gap_assessment"


@dataclass(frozen=True, slots=True)
class EvaluationSlot:
    slot_id: str
    experiment_id: str
    asset: Asset
    source_symbol: str
    candle_open_ns: int
    candle_close_boundary_ns: int
    scheduled_evaluation_boundary_ns: int
    endpoint_close_boundary_ns: int

    def __post_init__(self) -> None:
        if self.asset not in ("BTC", "ETH"):
            raise ValueError("unsupported asset")

        expected_symbol = _SOURCE_SYMBOL_BY_ASSET[self.asset]
        if self.source_symbol != expected_symbol:
            raise ValueError(
                "source_symbol does not match asset"
            )

        if (
            self.candle_open_ns % _RESOLUTION_NS != 0
            or self.candle_close_boundary_ns % _RESOLUTION_NS != 0
        ):
            raise ValueError(
                "signal candle boundaries must align to five minutes"
            )

        if (
            self.candle_close_boundary_ns
            - self.candle_open_ns
            != _RESOLUTION_NS
        ):
            raise ValueError(
                "signal candle must span exactly five minutes"
            )

        if (
            self.scheduled_evaluation_boundary_ns
            != self.candle_close_boundary_ns
        ):
            raise ValueError(
                "evaluation boundary must equal signal candle close"
            )

        if (
            self.endpoint_close_boundary_ns
            != self.candle_close_boundary_ns + _HORIZON_NS
        ):
            raise ValueError(
                "endpoint must be exactly 30 minutes after "
                "signal candle"
            )


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: str
    protocol_version: str
    protocol_sha256: str

    window_start_utc_ns: int
    window_end_utc_ns: int
    endpoint_collection_end_utc_ns: int
    endpoint_acquisition_deadline_utc_ns: int

    signal_engine_source_sha256: str
    collector_build_id: str
    collector_source_sha256: str

    effective_config_json: str
    config_sha256: str
    runtime_identity_json: str

    population_rules_json: str
    missing_data_rules_json: str
    endpoint_rules_json: str
    clustering_rules_json: str
    failure_rules_json: str

    created_at_utc_ns: int

    venue: str = "Delta Exchange India"
    endpoint: str = "/v2/history/candles"
    resolution_seconds: int = 300
    primary_horizon_seconds: int = 1800
    timestamp_convention: str = (
        "source native inclusive close timestamp retained; "
        "normalized close boundary is exclusive"
    )

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ValueError("experiment_id must be non-empty")
        if self.window_end_utc_ns <= self.window_start_utc_ns:
            raise ValueError("window end must follow start")
        if (
            self.window_start_utc_ns % _RESOLUTION_NS != 0
            or self.window_end_utc_ns % _RESOLUTION_NS != 0
        ):
            raise ValueError(
                "experiment window must align to five minutes"
            )
        if (
            self.endpoint_collection_end_utc_ns
            != self.window_end_utc_ns + 1_800_000_000_000
        ):
            raise ValueError(
                "endpoint collection end must be exactly 30 minutes after signal window"
            )
        if (
            self.endpoint_acquisition_deadline_utc_ns
            < self.endpoint_collection_end_utc_ns
        ):
            raise ValueError(
                "endpoint acquisition deadline cannot precede endpoint boundary"
            )
        if self.resolution_seconds != 300:
            raise ValueError("Experiment 1 resolution must remain five minutes")
        if self.primary_horizon_seconds != 1800:
            raise ValueError("Experiment 1 horizon must remain 30 minutes")
