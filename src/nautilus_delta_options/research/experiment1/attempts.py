from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from nautilus_delta_options.research.experiment1.manifest import (
    canonical_json,
    sha256_text,
)
from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
    JournalEventType,
)
from nautilus_delta_options.research.experiment1.ownership import (
    CollectorLease,
)
from nautilus_delta_options.research.experiment1.schema_identity import (
    build_schema_expectation,
    validate_database_identity,
)


@dataclass(frozen=True, slots=True)
class UnresolvedEvaluationAttempt:
    event_id: str
    experiment_id: str
    run_id: str
    slot_id: str
    attempt_id: str
    asset: str
    candle_close_boundary_ns: int
    started_at_utc_ns: int


def record_evaluation_attempt_started(
    lease: CollectorLease,
    *,
    experiment_id: str,
    run_id: str,
    slot: EvaluationSlot,
    attempt_id: str,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None = None,
    busy_timeout_ms: int = 250,
) -> str:
    """Durably record evaluator intent before evaluator invocation."""

    _require_text(experiment_id, "experiment_id")
    _require_text(run_id, "run_id")
    _require_text(attempt_id, "attempt_id")

    if slot.experiment_id != experiment_id:
        raise ValueError(
            "slot experiment does not match experiment_id"
        )

    _validate_time(
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    database_path = _held_database_path(lease)

    payload = canonical_json(
        {
            "asset": slot.asset,
            "candle_close_boundary_ns": (
                slot.candle_close_boundary_ns
            ),
            "phase": "before_evaluator_invocation",
        }
    )

    event_id = _event_id(
        experiment_id,
        run_id,
        attempt_id,
        JournalEventType.EVALUATION_STARTED,
    )

    with _write_connection(
        database_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as con:
        con.execute("BEGIN IMMEDIATE")

        _validate_identity(con)

        prior = con.execute(
            """
            SELECT
                event_type,
                run_id,
                slot_id
            FROM journal_events
            WHERE experiment_id = ?
              AND attempt_id = ?
            ORDER BY sequence
            """,
            (
                experiment_id,
                attempt_id,
            ),
        ).fetchall()

        if prior:
            raise RuntimeError(
                "evaluation attempt_id already exists"
            )

        con.execute(
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                experiment_id,
                run_id,
                slot.slot_id,
                attempt_id,
                JournalEventType.EVALUATION_STARTED.value,
                event_time_utc_ns,
                monotonic_elapsed_ns,
                payload,
                sha256_text(payload),
            ),
        )

        con.commit()

    return event_id


def record_evaluation_attempt_failed(
    lease: CollectorLease,
    *,
    experiment_id: str,
    run_id: str,
    slot: EvaluationSlot,
    attempt_id: str,
    reason: str,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None = None,
    busy_timeout_ms: int = 250,
) -> str:
    """Durably prove that an evaluator attempt failed before return."""

    _require_text(experiment_id, "experiment_id")
    _require_text(run_id, "run_id")
    _require_text(attempt_id, "attempt_id")
    _require_text(reason, "reason")

    if slot.experiment_id != experiment_id:
        raise ValueError(
            "slot experiment does not match experiment_id"
        )

    _validate_time(
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    database_path = _held_database_path(lease)

    payload = canonical_json(
        {
            "phase": "evaluator_failed_before_return",
            "reason": reason,
        }
    )

    event_id = _event_id(
        experiment_id,
        run_id,
        attempt_id,
        JournalEventType.EVALUATION_FAILED,
    )

    with _write_connection(
        database_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as con:
        con.execute("BEGIN IMMEDIATE")

        _validate_identity(con)

        rows = con.execute(
            """
            SELECT
                event_type,
                run_id,
                slot_id
            FROM journal_events
            WHERE experiment_id = ?
              AND attempt_id = ?
            ORDER BY sequence
            """,
            (
                experiment_id,
                attempt_id,
            ),
        ).fetchall()

        starts = [
            row
            for row in rows
            if str(row[0])
            == JournalEventType.EVALUATION_STARTED.value
        ]

        if len(starts) != 1:
            raise RuntimeError(
                "evaluation failure requires exactly one "
                "durable evaluation_started event"
            )

        start = starts[0]

        if str(start[1]) != run_id:
            raise RuntimeError(
                "evaluation attempt run_id does not match start"
            )

        if str(start[2]) != slot.slot_id:
            raise RuntimeError(
                "evaluation attempt slot does not match start"
            )

        terminal_types = {
            JournalEventType.EVALUATION_COMPLETED.value,
            JournalEventType.EVALUATION_FAILED.value,
        }

        if any(
            str(row[0]) in terminal_types
            for row in rows
        ):
            raise RuntimeError(
                "evaluation attempt already has a terminal event"
            )

        con.execute(
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                experiment_id,
                run_id,
                slot.slot_id,
                attempt_id,
                JournalEventType.EVALUATION_FAILED.value,
                event_time_utc_ns,
                monotonic_elapsed_ns,
                payload,
                sha256_text(payload),
            ),
        )

        con.commit()

    return event_id


def load_unresolved_evaluation_attempts(
    database_path: str | Path,
    *,
    experiment_id: str,
    busy_timeout_ms: int = 250,
) -> tuple[UnresolvedEvaluationAttempt, ...]:
    """Load starts that have no completed/failed terminal evidence."""

    _require_text(experiment_id, "experiment_id")

    path = Path(database_path)

    if not path.exists():
        raise FileNotFoundError(
            "Experiment 1 journal database does not exist"
        )

    if busy_timeout_ms < 0:
        raise ValueError(
            "busy_timeout_ms cannot be negative"
        )

    uri = path.resolve().as_uri() + "?mode=ro"

    with sqlite3.connect(
        uri,
        uri=True,
        timeout=busy_timeout_ms / 1000,
    ) as con:
        con.execute(
            f"PRAGMA busy_timeout = {busy_timeout_ms}"
        )

        _validate_identity(con)

        rows = con.execute(
            """
            SELECT
                started.event_id,
                started.experiment_id,
                started.run_id,
                started.slot_id,
                started.attempt_id,
                slot.asset,
                slot.candle_close_boundary_ns,
                started.event_time_utc_ns
            FROM journal_events AS started
            JOIN evaluation_slots AS slot
              ON slot.slot_id = started.slot_id
            LEFT JOIN population_bindings AS binding
              ON binding.slot_id = started.slot_id
            WHERE started.experiment_id = ?
              AND started.event_type = 'evaluation_started'
              AND binding.slot_id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM journal_events AS terminal
                  WHERE terminal.experiment_id
                        = started.experiment_id
                    AND terminal.attempt_id
                        = started.attempt_id
                    AND terminal.event_type IN (
                        'evaluation_completed',
                        'evaluation_failed'
                    )
              )
            ORDER BY
                slot.candle_close_boundary_ns,
                slot.asset,
                started.sequence
            """,
            (experiment_id,),
        ).fetchall()

    return tuple(
        UnresolvedEvaluationAttempt(
            event_id=str(row[0]),
            experiment_id=str(row[1]),
            run_id=str(row[2]),
            slot_id=str(row[3]),
            attempt_id=str(row[4]),
            asset=str(row[5]),
            candle_close_boundary_ns=int(row[6]),
            started_at_utc_ns=int(row[7]),
        )
        for row in rows
    )


def seal_unresolved_attempts_as_gaps(
    lease: CollectorLease,
    *,
    experiment_id: str,
    run_id: str,
    recorded_at_utc_ns: int,
    busy_timeout_ms: int = 250,
) -> tuple[str, ...]:
    """Seal crash-ambiguous evaluator starts as immutable gaps.

    This is intentionally independent of the normal protocol cutoff.
    A durable start without a durable completed/failed terminal means
    the previous process may have produced an evaluator result that
    was lost before evidence commit.
    """

    _require_text(experiment_id, "experiment_id")
    _require_text(run_id, "run_id")

    if recorded_at_utc_ns <= 0:
        raise ValueError(
            "recorded_at_utc_ns must be positive"
        )

    database_path = _held_database_path(lease)

    with _write_connection(
        database_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as con:
        con.execute("BEGIN IMMEDIATE")

        _validate_identity(con)

        rows = con.execute(
            """
            SELECT DISTINCT
                slot.slot_id,
                slot.candle_close_boundary_ns,
                slot.asset
            FROM journal_events AS started
            JOIN evaluation_slots AS slot
              ON slot.slot_id = started.slot_id
            LEFT JOIN population_bindings AS binding
              ON binding.slot_id = started.slot_id
            WHERE started.experiment_id = ?
              AND started.event_type = 'evaluation_started'
              AND binding.slot_id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM journal_events AS terminal
                  WHERE terminal.experiment_id
                        = started.experiment_id
                    AND terminal.attempt_id
                        = started.attempt_id
                    AND terminal.event_type IN (
                        'evaluation_completed',
                        'evaluation_failed'
                    )
              )
            ORDER BY
                slot.candle_close_boundary_ns,
                slot.asset,
                slot.slot_id
            """,
            (experiment_id,),
        ).fetchall()

        slot_ids = tuple(
            str(row[0])
            for row in rows
        )

        for slot_id in slot_ids:
            con.execute(
                """
                INSERT INTO population_bindings (
                    slot_id,
                    experiment_id,
                    recorded_run_id,
                    binding_kind,
                    canonical_event_id,
                    canonical_attempt_id,
                    canonical_decision,
                    canonical_captured_ns,
                    reason,
                    recorded_at_utc_ns
                )
                VALUES (
                    ?, ?, ?, 'gap',
                    NULL, NULL, NULL, NULL,
                    'startup_recovery_unresolved_evaluation_attempt',
                    ?
                )
                """,
                (
                    slot_id,
                    experiment_id,
                    run_id,
                    recorded_at_utc_ns,
                ),
            )

        con.commit()

    return slot_ids


def _held_database_path(
    lease: CollectorLease,
) -> Path:
    if not lease.is_held:
        raise RuntimeError(
            "collector lease must be held"
        )

    return lease.database_path


def _write_connection(
    database_path: Path,
    *,
    busy_timeout_ms: int,
) -> sqlite3.Connection:
    if busy_timeout_ms < 0:
        raise ValueError(
            "busy_timeout_ms cannot be negative"
        )

    uri = database_path.resolve().as_uri() + "?mode=rw"

    con = sqlite3.connect(
        uri,
        uri=True,
        timeout=busy_timeout_ms / 1000,
    )

    con.execute("PRAGMA foreign_keys = ON")
    con.execute(
        f"PRAGMA busy_timeout = {busy_timeout_ms}"
    )
    con.execute("PRAGMA synchronous = FULL")

    _validate_identity(con)

    return con


def _validate_identity(
    con: sqlite3.Connection,
) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    expectation = build_schema_expectation(schema_path)

    validate_database_identity(
        con,
        expectation,
    )


def _event_id(
    experiment_id: str,
    run_id: str,
    attempt_id: str,
    event_type: JournalEventType,
) -> str:
    return sha256_text(
        ":".join(
            (
                "event",
                experiment_id,
                run_id,
                attempt_id,
                event_type.value,
            )
        )
    )


def _validate_time(
    *,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None,
) -> None:
    if event_time_utc_ns <= 0:
        raise ValueError(
            "event_time_utc_ns must be positive"
        )

    if (
        monotonic_elapsed_ns is not None
        and monotonic_elapsed_ns < 0
    ):
        raise ValueError(
            "monotonic_elapsed_ns cannot be negative"
        )


def _require_text(
    value: str,
    field: str,
) -> None:
    if not value:
        raise ValueError(
            f"{field} must be non-empty"
        )
