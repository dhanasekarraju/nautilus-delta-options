from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nautilus_delta_options.research.experiment1.journal import (
    JournalStatement,
)
from nautilus_delta_options.research.experiment1.schema_identity import (
    build_schema_expectation,
    validate_database_identity,
)
from nautilus_delta_options.signals.v34 import V34Decision

BindingKind = Literal["canonical", "gap"]

PopulationStatus = Literal[
    "canonical",
    "same_decision_retry",
    "conflicting_retry",
]

_NS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class PopulationBinding:
    slot_id: str
    experiment_id: str
    asset: str
    candle_close_boundary_ns: int
    binding_kind: BindingKind
    canonical_event_id: str | None
    canonical_attempt_id: str | None
    canonical_decision: V34Decision | None
    canonical_captured_ns: int | None
    reason: str | None
    recorded_at_utc_ns: int

    @property
    def signal_candle_close_ms(self) -> int:
        return (
            self.candle_close_boundary_ns
            // _NS_PER_MS
            - 1
        )


def build_canonical_population_statement(
    *,
    experiment_id: str,
    run_id: str,
    slot_id: str,
    event_id: str,
    attempt_id: str,
    decision: V34Decision,
    captured_ns: int,
    recorded_at_utc_ns: int,
) -> JournalStatement:
    """Build the canonical binding for the first durable evaluator return.

    The referenced evaluation_completed event must be inserted earlier
    in the SAME SQLite transaction.
    """

    _require_text(experiment_id, "experiment_id")
    _require_text(run_id, "run_id")
    _require_text(slot_id, "slot_id")
    _require_text(event_id, "event_id")
    _require_text(attempt_id, "attempt_id")

    if captured_ns <= 0:
        raise ValueError("captured_ns must be positive")
    if recorded_at_utc_ns <= 0:
        raise ValueError(
            "recorded_at_utc_ns must be positive"
        )

    return (
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
        VALUES (?, ?, ?, 'canonical', ?, ?, ?, ?, NULL, ?)
        """,
        (
            slot_id,
            experiment_id,
            run_id,
            event_id,
            attempt_id,
            decision.value,
            captured_ns,
            recorded_at_utc_ns,
        ),
    )


def seal_population_gaps_through(
    database_path: str | Path,
    *,
    experiment_id: str,
    run_id: str,
    cutoff_boundary_ns: int,
    recorded_at_utc_ns: int,
    busy_timeout_ms: int = 250,
) -> tuple[str, ...]:
    """Seal unbound slots through an explicit protocol cutoff.

    This function deliberately does NOT derive missingness from
    collector startup time.

    The caller must supply a cutoff that the Experiment 1 protocol
    has already declared ineligible for a first evaluator return.
    This prevents a fresh start or near-boundary restart from
    prematurely converting an otherwise valid scheduled slot into
    an immutable gap.

    If an earlier evaluator return was lost before durable SQLite
    commit, a later protocol-approved cutoff can conservatively seal
    that still-unbound slot as a gap. Once sealed, later retries can
    never become canonical population members.
    """

    path = Path(database_path)

    _require_text(experiment_id, "experiment_id")
    _require_text(run_id, "run_id")

    if cutoff_boundary_ns < 0:
        raise ValueError(
            "cutoff_boundary_ns cannot be negative"
        )
    if recorded_at_utc_ns <= 0:
        raise ValueError(
            "recorded_at_utc_ns must be positive"
        )
    if busy_timeout_ms < 0:
        raise ValueError(
            "busy_timeout_ms cannot be negative"
        )
    if not path.exists():
        raise FileNotFoundError(
            "Experiment 1 journal database does not exist"
        )

    schema_path = Path(__file__).with_name("schema.sql")
    expectation = build_schema_expectation(schema_path)

    uri = path.resolve().as_uri() + "?mode=rw"

    con = sqlite3.connect(
        uri,
        uri=True,
        timeout=busy_timeout_ms / 1000,
    )

    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            f"PRAGMA busy_timeout = {busy_timeout_ms}"
        )
        con.execute("PRAGMA synchronous = FULL")

        validate_database_identity(
            con,
            expectation,
        )

        con.execute("BEGIN IMMEDIATE")

        # Revalidate while holding the write reservation so startup
        # recovery never mutates a database whose schema changed
        # between preflight and mutation.
        validate_database_identity(
            con,
            expectation,
        )

        rows = con.execute(
            """
            SELECT slot.slot_id
            FROM evaluation_slots AS slot
            LEFT JOIN population_bindings AS binding
              ON binding.slot_id = slot.slot_id
            WHERE slot.experiment_id = ?
              AND slot.scheduled_evaluation_boundary_ns <= ?
              AND binding.slot_id IS NULL
            ORDER BY
                slot.scheduled_evaluation_boundary_ns,
                slot.asset,
                slot.slot_id
            """,
            (
                experiment_id,
                cutoff_boundary_ns,
            ),
        ).fetchall()

        slot_ids = tuple(str(row[0]) for row in rows)

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
                    'startup_recovery_missing_canonical_binding',
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

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


def load_population_bindings(
    database_path: str | Path,
    *,
    experiment_id: str,
    busy_timeout_ms: int = 250,
) -> tuple[PopulationBinding, ...]:
    """Load immutable canonical/gap ownership for collector recovery."""

    path = Path(database_path)

    _require_text(experiment_id, "experiment_id")

    if busy_timeout_ms < 0:
        raise ValueError(
            "busy_timeout_ms cannot be negative"
        )

    schema_path = Path(__file__).with_name("schema.sql")
    expectation = build_schema_expectation(schema_path)

    uri = path.resolve().as_uri() + "?mode=ro"

    with sqlite3.connect(
        uri,
        uri=True,
        timeout=busy_timeout_ms / 1000,
    ) as con:
        con.execute(
            f"PRAGMA busy_timeout = {busy_timeout_ms}"
        )

        validate_database_identity(
            con,
            expectation,
        )

        rows = con.execute(
            """
            SELECT
                binding.slot_id,
                binding.experiment_id,
                slot.asset,
                slot.candle_close_boundary_ns,
                binding.binding_kind,
                binding.canonical_event_id,
                binding.canonical_attempt_id,
                binding.canonical_decision,
                binding.canonical_captured_ns,
                binding.reason,
                binding.recorded_at_utc_ns
            FROM population_bindings AS binding
            JOIN evaluation_slots AS slot
              ON slot.slot_id = binding.slot_id
            WHERE binding.experiment_id = ?
            ORDER BY
                slot.candle_close_boundary_ns,
                slot.asset,
                slot.slot_id
            """,
            (experiment_id,),
        ).fetchall()

    bindings: list[PopulationBinding] = []

    for row in rows:
        decision_raw = row[7]

        bindings.append(
            PopulationBinding(
                slot_id=str(row[0]),
                experiment_id=str(row[1]),
                asset=str(row[2]),
                candle_close_boundary_ns=int(row[3]),
                binding_kind=str(row[4]),  # type: ignore[arg-type]
                canonical_event_id=(
                    None
                    if row[5] is None
                    else str(row[5])
                ),
                canonical_attempt_id=(
                    None
                    if row[6] is None
                    else str(row[6])
                ),
                canonical_decision=(
                    None
                    if decision_raw is None
                    else V34Decision(str(decision_raw))
                ),
                canonical_captured_ns=(
                    None
                    if row[8] is None
                    else int(row[8])
                ),
                reason=(
                    None
                    if row[9] is None
                    else str(row[9])
                ),
                recorded_at_utc_ns=int(row[10]),
            )
        )

    return tuple(bindings)


def _require_text(value: str, field: str) -> None:
    if not value:
        raise ValueError(f"{field} must be non-empty")
