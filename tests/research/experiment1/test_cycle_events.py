from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nautilus_delta_options.research.experiment1.cycle_events import (
    build_cycle_started_statement,
    build_cycle_terminal_statement,
)

_SCHEMA = Path(
    "src/nautilus_delta_options/research/experiment1/schema.sql"
)


def _database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(_SCHEMA.read_text())
    _insert_manifest(con)
    return con


def _insert_manifest(con: sqlite3.Connection) -> None:
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
            0,
            300000000000,
            2100000000000,
            2160000000000,
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
        """
    )


def test_cycle_started_event_is_accepted_by_schema() -> None:
    con = _database()

    statement, ref = build_cycle_started_statement(
        experiment_id="exp-1",
        run_id="run-1",
        cycle_id="cycle-1",
        event_time_utc_ns=10,
    )

    con.execute(*statement)

    row = con.execute(
        """
        SELECT event_type, payload_json
        FROM journal_events
        WHERE event_id = ?
        """,
        (ref.event_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == "observer_cycle_started"
    assert json.loads(row[1])["cycle_id"] == "cycle-1"


def test_completed_cycle_marks_state_committed() -> None:
    con = _database()

    statement, ref = build_cycle_terminal_statement(
        experiment_id="exp-1",
        run_id="run-1",
        cycle_id="cycle-1",
        status="completed",
        candle_close_boundary_ns=0,
        evaluation_event_ids=("btc-event", "eth-event"),
        event_time_utc_ns=20,
    )

    con.execute(*statement)

    payload_json = con.execute(
        """
        SELECT payload_json
        FROM journal_events
        WHERE event_id = ?
        """,
        (ref.event_id,),
    ).fetchone()[0]

    payload = json.loads(payload_json)

    assert payload["status"] == "completed"
    assert payload["state_committed"] is True
    assert payload["evaluation_event_ids"] == [
        "btc-event",
        "eth-event",
    ]


def test_aborted_cycle_preserves_returned_evaluation_ids() -> None:
    con = _database()

    statement, ref = build_cycle_terminal_statement(
        experiment_id="exp-1",
        run_id="run-1",
        cycle_id="cycle-1",
        status="aborted",
        candle_close_boundary_ns=0,
        evaluation_event_ids=("btc-event",),
        event_time_utc_ns=20,
        failure_stage="btc_call_ranking",
        detail="injected failure",
    )

    con.execute(*statement)

    row = con.execute(
        """
        SELECT event_type, payload_json
        FROM journal_events
        WHERE event_id = ?
        """,
        (ref.event_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == "observer_cycle_aborted"

    payload = json.loads(row[1])

    assert payload["state_committed"] is False
    assert payload["evaluation_event_ids"] == ["btc-event"]
    assert payload["failure_stage"] == "btc_call_ranking"


def test_completed_cycle_rejects_failure_stage() -> None:
    with pytest.raises(
        ValueError,
        match="cannot have failure_stage",
    ):
        build_cycle_terminal_statement(
            experiment_id="exp-1",
            run_id="run-1",
            cycle_id="cycle-1",
            status="completed",
            candle_close_boundary_ns=0,
            evaluation_event_ids=(),
            event_time_utc_ns=20,
            failure_stage="unexpected",
        )


def test_aborted_cycle_requires_failure_stage() -> None:
    with pytest.raises(
        ValueError,
        match="requires failure_stage",
    ):
        build_cycle_terminal_statement(
            experiment_id="exp-1",
            run_id="run-1",
            cycle_id="cycle-1",
            status="aborted",
            candle_close_boundary_ns=None,
            evaluation_event_ids=(),
            event_time_utc_ns=20,
        )
