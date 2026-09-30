from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
)
from nautilus_delta_options.research.experiment1.manifest import (
    sha256_text,
)
from nautilus_delta_options.research.experiment1.population import (
    build_canonical_population_statement,
    load_population_bindings,
    seal_population_gaps_through,
)
from nautilus_delta_options.signals.v34 import V34Decision

_STEP = 300_000_000_000
_HORIZON = 1_800_000_000_000


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        path,
        create_new=True,
        start_immediately=False,
    )
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        con.execute(
            """
            INSERT INTO experiment_manifest (
                experiment_id,
                protocol_version,
                protocol_sha256,
                manifest_sha256,
                schedule_sha256,
                window_start_utc_ns,
                window_end_utc_ns,
                endpoint_collection_end_utc_ns,
                endpoint_acquisition_deadline_utc_ns,
                assets_json,
                source_symbols_json,
                venue,
                endpoint,
                resolution_seconds,
                timestamp_convention,
                primary_horizon_seconds,
                signal_engine_source_sha256,
                collector_build_id,
                collector_source_sha256,
                effective_config_json,
                config_sha256,
                runtime_identity_json,
                population_rules_json,
                missing_data_rules_json,
                endpoint_rules_json,
                clustering_rules_json,
                failure_rules_json,
                created_at_utc_ns
            )
            VALUES (
                'exp-1',
                '1',
                'protocol-sha',
                'manifest-sha',
                'schedule-sha',
                ?,
                ?,
                ?,
                ?,
                '["BTC","ETH"]',
                '{"BTC":"BTCUSD","ETH":"ETHUSD"}',
                'Delta Exchange India',
                '/v2/history/candles',
                300,
                'exclusive-close-boundary',
                1800,
                'signal-sha',
                'collector-build',
                'collector-sha',
                '{}',
                'config-sha',
                '{}',
                '{}',
                '{}',
                '{}',
                '{}',
                '{}',
                1
            )
            """,
            (
                _STEP,
                4 * _STEP,
                4 * _STEP + _HORIZON,
                4 * _STEP + _HORIZON,
            ),
        )

        for index, close_ns in enumerate(
            (
                _STEP,
                2 * _STEP,
                3 * _STEP,
            ),
            start=1,
        ):
            con.execute(
                """
                INSERT INTO evaluation_slots (
                    slot_id,
                    experiment_id,
                    asset,
                    source_symbol,
                    candle_open_ns,
                    candle_close_boundary_ns,
                    scheduled_evaluation_boundary_ns,
                    endpoint_close_boundary_ns
                )
                VALUES (?, 'exp-1', 'BTC', 'BTCUSD', ?, ?, ?, ?)
                """,
                (
                    f"slot-{index}",
                    close_ns - _STEP,
                    close_ns,
                    close_ns,
                    close_ns + _HORIZON,
                ),
            )

    return path


def _insert_evaluation_event(
    con: sqlite3.Connection,
    *,
    slot_id: str,
    event_id: str,
    attempt_id: str,
) -> None:
    payload = "{}"

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
        VALUES (
            ?, 'exp-1', 'run-1', ?, ?,
            'evaluation_completed',
            1,
            NULL,
            ?,
            ?
        )
        """,
        (
            event_id,
            slot_id,
            attempt_id,
            payload,
            sha256_text(payload),
        ),
    )


def test_startup_recovery_seals_only_due_unbound_slots(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        _insert_evaluation_event(
            con,
            slot_id="slot-1",
            event_id="event-1",
            attempt_id="attempt-1",
        )

        con.execute(
            *build_canonical_population_statement(
                experiment_id="exp-1",
                run_id="run-1",
                slot_id="slot-1",
                event_id="event-1",
                attempt_id="attempt-1",
                decision=V34Decision.WAIT,
                captured_ns=123,
                recorded_at_utc_ns=456,
            )
        )

    sealed = seal_population_gaps_through(
        path,
        experiment_id="exp-1",
        run_id="run-restart",
        cutoff_boundary_ns=2 * _STEP,
        recorded_at_utc_ns=2 * _STEP + 1,
    )

    assert sealed == ("slot-2",)

    bindings = load_population_bindings(
        path,
        experiment_id="exp-1",
    )

    assert [binding.slot_id for binding in bindings] == [
        "slot-1",
        "slot-2",
    ]

    canonical, gap = bindings

    assert canonical.binding_kind == "canonical"
    assert canonical.canonical_decision is V34Decision.WAIT
    assert canonical.canonical_captured_ns == 123
    assert canonical.reason is None

    assert gap.binding_kind == "gap"
    assert gap.canonical_event_id is None
    assert gap.canonical_decision is None
    assert gap.reason == (
        "startup_recovery_missing_canonical_binding"
    )

    # Future slot remains eligible because it was not yet due.
    assert all(
        binding.slot_id != "slot-3"
        for binding in bindings
    )


def test_startup_gap_sealing_is_idempotent(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    first = seal_population_gaps_through(
        path,
        experiment_id="exp-1",
        run_id="run-a",
        cutoff_boundary_ns=2 * _STEP,
        recorded_at_utc_ns=2 * _STEP + 1,
    )

    second = seal_population_gaps_through(
        path,
        experiment_id="exp-1",
        run_id="run-b",
        cutoff_boundary_ns=2 * _STEP,
        recorded_at_utc_ns=2 * _STEP + 1,
    )

    assert first == ("slot-1", "slot-2")
    assert second == ()


def test_gap_slot_cannot_later_be_promoted_to_canonical(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    sealed = seal_population_gaps_through(
        path,
        experiment_id="exp-1",
        run_id="run-restart",
        cutoff_boundary_ns=_STEP,
        recorded_at_utc_ns=_STEP + 1,
    )

    assert sealed == ("slot-1",)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        _insert_evaluation_event(
            con,
            slot_id="slot-1",
            event_id="later-event",
            attempt_id="later-attempt",
        )

        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                *build_canonical_population_statement(
                    experiment_id="exp-1",
                    run_id="run-retry",
                    slot_id="slot-1",
                    event_id="later-event",
                    attempt_id="later-attempt",
                    decision=V34Decision.CALL,
                    captured_ns=999,
                    recorded_at_utc_ns=1000,
                )
            )


def test_canonical_binding_requires_matching_evaluation_event(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        _insert_evaluation_event(
            con,
            slot_id="slot-2",
            event_id="wrong-slot-event",
            attempt_id="attempt-1",
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match=(
                "canonical population binding does not "
                "match evaluation event"
            ),
        ):
            con.execute(
                *build_canonical_population_statement(
                    experiment_id="exp-1",
                    run_id="run-1",
                    slot_id="slot-1",
                    event_id="wrong-slot-event",
                    attempt_id="attempt-1",
                    decision=V34Decision.PUT,
                    captured_ns=123,
                    recorded_at_utc_ns=456,
                )
            )


def test_population_bindings_are_append_only(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    seal_population_gaps_through(
        path,
        experiment_id="exp-1",
        run_id="run-restart",
        cutoff_boundary_ns=_STEP,
        recorded_at_utc_ns=_STEP + 1,
    )

    with sqlite3.connect(path) as con:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="population_bindings is append-only",
        ):
            con.execute(
                """
                UPDATE population_bindings
                SET reason = 'changed'
                WHERE slot_id = 'slot-1'
                """
            )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="population_bindings is append-only",
        ):
            con.execute(
                """
                DELETE FROM population_bindings
                WHERE slot_id = 'slot-1'
                """
            )


def test_schema_version_is_two(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    with sqlite3.connect(path) as con:
        assert con.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == 2


def test_population_loader_reports_durable_evaluation_count(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        _insert_evaluation_event(
            con,
            slot_id="slot-1",
            event_id="event-1",
            attempt_id="attempt-1",
        )

        con.execute(
            *build_canonical_population_statement(
                experiment_id="exp-1",
                run_id="run-1",
                slot_id="slot-1",
                event_id="event-1",
                attempt_id="attempt-1",
                decision=V34Decision.WAIT,
                captured_ns=123,
                recorded_at_utc_ns=456,
            )
        )

        _insert_evaluation_event(
            con,
            slot_id="slot-1",
            event_id="event-2",
            attempt_id="attempt-2",
        )

    bindings = load_population_bindings(
        path,
        experiment_id="exp-1",
    )

    binding = next(
        item
        for item in bindings
        if item.slot_id == "slot-1"
    )

    assert binding.binding_kind == "canonical"
    assert binding.durable_evaluation_count == 2
