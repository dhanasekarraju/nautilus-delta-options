import sqlite3
from pathlib import Path

import pytest

SCHEMA = Path(
    "src/nautilus_delta_options/research/experiment1/schema.sql"
)


def _database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text())
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
            1000000000,
            601000000000,
            2401000000000,
            2461000000000,
            '["BTC","ETH"]',
            '{"BTC":"BTCUSD","ETH":"ETHUSD"}',
            'Delta Exchange India',
            '/v2/history/candles',
            300,
            'normalized exclusive close boundary',
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


def _insert_slot(con: sqlite3.Connection) -> None:
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
        VALUES (
            'slot-btc-1',
            'exp-1',
            'BTC',
            'BTCUSD',
            1000000000,
            301000000000,
            301000000000,
            2101000000000
        )
        """
    )


def test_schema_loads_and_foreign_keys_are_enabled() -> None:
    con = _database()

    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    tables = {
        row[0]
        for row in con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        )
    }

    assert {
        "experiment_manifest",
        "evaluation_slots",
        "journal_events",
        "source_responses",
        "candle_observations",
        "evaluation_candle_inputs",
        "signal_endpoints",
    } <= tables


def test_manifest_is_append_only() -> None:
    con = _database()
    _insert_manifest(con)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_manifest is append-only",
    ):
        con.execute(
            """
            UPDATE experiment_manifest
            SET protocol_version = '2'
            WHERE experiment_id = 'exp-1'
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_manifest is append-only",
    ):
        con.execute(
            """
            DELETE FROM experiment_manifest
            WHERE experiment_id = 'exp-1'
            """
        )


def test_schedule_is_append_only_and_unique_per_asset_candle() -> None:
    con = _database()
    _insert_manifest(con)
    _insert_slot(con)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_slot(con)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="evaluation_slots is append-only",
    ):
        con.execute(
            """
            UPDATE evaluation_slots
            SET source_symbol = 'OTHER'
            WHERE slot_id = 'slot-btc-1'
            """
        )


def test_slot_endpoint_is_exactly_thirty_minutes() -> None:
    con = _database()
    _insert_manifest(con)

    with pytest.raises(sqlite3.IntegrityError):
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
            VALUES (
                'bad-slot',
                'exp-1',
                'BTC',
                'BTCUSD',
                1000000000,
                301000000000,
                301000000000,
                2100000000000
            )
            """
        )


def test_journal_events_are_append_only() -> None:
    con = _database()
    _insert_manifest(con)
    _insert_slot(con)

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
            'event-1',
            'exp-1',
            'run-1',
            'slot-btc-1',
            'attempt-1',
            'evaluation_started',
            400000000000,
            1,
            '{}',
            'payload-sha'
        )
        """
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="journal_events is append-only",
    ):
        con.execute(
            """
            UPDATE journal_events
            SET event_type = 'evaluation_completed'
            WHERE event_id = 'event-1'
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="journal_events is append-only",
    ):
        con.execute(
            """
            DELETE FROM journal_events
            WHERE event_id = 'event-1'
            """
        )


def test_source_response_is_append_only_and_candle_links_to_it() -> None:
    con = _database()
    _insert_manifest(con)

    con.execute(
        """
        INSERT INTO source_responses (
            response_id,
            experiment_id,
            run_id,
            request_id,
            asset,
            source_symbol,
            request_started_utc_ns,
            response_received_utc_ns,
            request_elapsed_ns,
            source_payload_sha256,
            raw_response_body,
            parse_status
        )
        VALUES (
            'response-1',
            'exp-1',
            'run-1',
            'request-1',
            'BTC',
            'BTCUSD',
            1000000000,
            2000000000,
            1000000000,
            'response-sha',
            X'7B7D',
            'parsed'
        )
        """
    )

    con.execute(
        """
        INSERT INTO candle_observations (
            observation_id,
            experiment_id,
            run_id,
            request_id,
            response_id,
            asset,
            source_symbol,
            source_timestamp_raw,
            normalized_open_ns,
            normalized_close_boundary_ns,
            open_decimal,
            high_decimal,
            low_decimal,
            close_decimal,
            volume_decimal,
            request_started_utc_ns,
            response_received_utc_ns,
            request_elapsed_ns,
            source_payload_sha256,
            raw_candle_json,
            parse_status
        )
        VALUES (
            'observation-1',
            'exp-1',
            'run-1',
            'request-1',
            'response-1',
            'BTC',
            'BTCUSD',
            '0',
            0,
            300000000000,
            '100',
            '110',
            '90',
            '105',
            '12.5',
            1000000000,
            2000000000,
            1000000000,
            'response-sha',
            '{}',
            'parsed'
        )
        """
    )

    assert con.execute(
        """
        SELECT response_id
        FROM candle_observations
        WHERE observation_id = 'observation-1'
        """
    ).fetchone()[0] == "response-1"

    with pytest.raises(
        sqlite3.IntegrityError,
        match="source_responses is append-only",
    ):
        con.execute(
            """
            UPDATE source_responses
            SET parse_status = 'changed'
            WHERE response_id = 'response-1'
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="source_responses is append-only",
    ):
        con.execute(
            """
            DELETE FROM source_responses
            WHERE response_id = 'response-1'
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """
            INSERT INTO candle_observations (
                observation_id,
                experiment_id,
                run_id,
                request_id,
                response_id,
                asset,
                source_symbol,
                source_timestamp_raw,
                normalized_open_ns,
                normalized_close_boundary_ns,
                open_decimal,
                high_decimal,
                low_decimal,
                close_decimal,
                volume_decimal,
                request_started_utc_ns,
                response_received_utc_ns,
                request_elapsed_ns,
                source_payload_sha256,
                raw_candle_json,
                parse_status
            )
            VALUES (
                'observation-bad',
                'exp-1',
                'run-1',
                'request-missing',
                'missing-response',
                'BTC',
                'BTCUSD',
                '0',
                0,
                300000000000,
                '100',
                '110',
                '90',
                '105',
                '12.5',
                1000000000,
                2000000000,
                1000000000,
                'missing-sha',
                '{}',
                'parsed'
            )
            """
        )
