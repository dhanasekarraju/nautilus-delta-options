from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
    JournalStatement,
)
from nautilus_delta_options.research.experiment1.manifest import (
    canonical_json,
    sha256_text,
)
from nautilus_delta_options.research.experiment1.models import (
    JournalEventType,
)

CycleTerminalStatus = Literal["completed", "aborted"]


@dataclass(frozen=True, slots=True)
class CycleEventRef:
    event_id: str
    cycle_id: str


def enqueue_cycle_started(
    journal: ExperimentJournal,
    *,
    experiment_id: str,
    run_id: str,
    cycle_id: str,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None = None,
) -> CycleEventRef | None:
    statement, ref = build_cycle_started_statement(
        experiment_id=experiment_id,
        run_id=run_id,
        cycle_id=cycle_id,
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    if not journal.enqueue_batch((statement,)):
        return None

    return ref


def enqueue_cycle_terminal(
    journal: ExperimentJournal,
    *,
    experiment_id: str,
    run_id: str,
    cycle_id: str,
    status: CycleTerminalStatus,
    candle_close_boundary_ns: int | None,
    evaluation_event_ids: tuple[str, ...],
    event_time_utc_ns: int,
    failure_stage: str | None = None,
    detail: str | None = None,
    monotonic_elapsed_ns: int | None = None,
) -> CycleEventRef | None:
    statement, ref = build_cycle_terminal_statement(
        experiment_id=experiment_id,
        run_id=run_id,
        cycle_id=cycle_id,
        status=status,
        candle_close_boundary_ns=candle_close_boundary_ns,
        evaluation_event_ids=evaluation_event_ids,
        event_time_utc_ns=event_time_utc_ns,
        failure_stage=failure_stage,
        detail=detail,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    if not journal.enqueue_batch((statement,)):
        return None

    return ref


def build_cycle_started_statement(
    *,
    experiment_id: str,
    run_id: str,
    cycle_id: str,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None = None,
) -> tuple[JournalStatement, CycleEventRef]:
    _validate_common(
        experiment_id=experiment_id,
        run_id=run_id,
        cycle_id=cycle_id,
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    event_type = JournalEventType.OBSERVER_CYCLE_STARTED

    payload = {
        "payload_version": 1,
        "cycle_id": cycle_id,
    }

    return _event_statement(
        experiment_id=experiment_id,
        run_id=run_id,
        cycle_id=cycle_id,
        event_type=event_type,
        payload=payload,
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )


def build_cycle_terminal_statement(
    *,
    experiment_id: str,
    run_id: str,
    cycle_id: str,
    status: CycleTerminalStatus,
    candle_close_boundary_ns: int | None,
    evaluation_event_ids: tuple[str, ...],
    event_time_utc_ns: int,
    failure_stage: str | None = None,
    detail: str | None = None,
    monotonic_elapsed_ns: int | None = None,
) -> tuple[JournalStatement, CycleEventRef]:
    _validate_common(
        experiment_id=experiment_id,
        run_id=run_id,
        cycle_id=cycle_id,
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    if len(evaluation_event_ids) != len(set(evaluation_event_ids)):
        raise ValueError(
            "evaluation_event_ids must be unique"
        )

    if any(not value for value in evaluation_event_ids):
        raise ValueError(
            "evaluation_event_ids must be non-empty"
        )

    if status == "completed":
        if candle_close_boundary_ns is None:
            raise ValueError(
                "completed cycle requires candle boundary"
            )
        if failure_stage is not None:
            raise ValueError(
                "completed cycle cannot have failure_stage"
            )

        event_type = JournalEventType.OBSERVER_CYCLE_COMPLETED
        state_committed = True

    elif status == "aborted":
        if not failure_stage:
            raise ValueError(
                "aborted cycle requires failure_stage"
            )

        event_type = JournalEventType.OBSERVER_CYCLE_ABORTED
        state_committed = False

    else:
        raise ValueError("unsupported cycle terminal status")

    payload = {
        "payload_version": 1,
        "cycle_id": cycle_id,
        "status": status,
        "candle_close_boundary_ns": candle_close_boundary_ns,
        "state_committed": state_committed,
        "failure_stage": failure_stage,
        "detail": detail,
        "evaluation_event_ids": list(evaluation_event_ids),
        "evaluation_event_count": len(evaluation_event_ids),
    }

    return _event_statement(
        experiment_id=experiment_id,
        run_id=run_id,
        cycle_id=cycle_id,
        event_type=event_type,
        payload=payload,
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )


def _event_statement(
    *,
    experiment_id: str,
    run_id: str,
    cycle_id: str,
    event_type: JournalEventType,
    payload: dict[str, object],
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None,
) -> tuple[JournalStatement, CycleEventRef]:
    payload_json = canonical_json(payload)
    payload_sha256 = sha256_text(payload_json)

    event_id = sha256_text(
        "\x1f".join(
            (
                "observer-cycle-event",
                experiment_id,
                run_id,
                cycle_id,
                event_type.value,
            )
        )
    )

    statement: JournalStatement = (
        """
        INSERT INTO journal_events (
            event_id,
            experiment_id,
            run_id,
            slot_id,
            attempt_id,
            event_type,
            event_time_utc_ns,
            monotonic_elapsed_ns,
            payload_json,
            payload_sha256
        )
        VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            experiment_id,
            run_id,
            event_type.value,
            event_time_utc_ns,
            monotonic_elapsed_ns,
            payload_json,
            payload_sha256,
        ),
    )

    return (
        statement,
        CycleEventRef(
            event_id=event_id,
            cycle_id=cycle_id,
        ),
    )


def _validate_common(
    *,
    experiment_id: str,
    run_id: str,
    cycle_id: str,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None,
) -> None:
    if not experiment_id:
        raise ValueError("experiment_id must be non-empty")
    if not run_id:
        raise ValueError("run_id must be non-empty")
    if not cycle_id:
        raise ValueError("cycle_id must be non-empty")
    if event_time_utc_ns < 0:
        raise ValueError("event_time_utc_ns cannot be negative")
    if (
        monotonic_elapsed_ns is not None
        and monotonic_elapsed_ns < 0
    ):
        raise ValueError(
            "monotonic_elapsed_ns cannot be negative"
        )
