from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nautilus_delta_options.research.experiment1.cycle_events import (
    build_cycle_terminal_statement,
)
from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
)
from nautilus_delta_options.research.experiment1.manifest import (
    canonical_json,
    sha256_text,
)
from nautilus_delta_options.research.experiment1.rolling_state import (
    load_last_committed_observer_state,
)


def _database(
    tmp_path: Path,
) -> Path:
    path = tmp_path / "rolling.sqlite"

    journal = ExperimentJournal(
        path,
        create_new=True,
        start_immediately=False,
    )

    assert journal.close(
        timeout_seconds=2.0
    )

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

        for asset, symbol in (
            ("BTC", "BTCUSD"),
            ("ETH", "ETHUSD"),
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
                VALUES (
                    ?,
                    'exp-1',
                    ?,
                    ?,
                    -300000000000,
                    0,
                    0,
                    1800000000000
                )
                """,
                (
                    f"{asset.lower()}-slot",
                    asset,
                    symbol,
                ),
            )

    return path


def _insert_evaluation(
    con: sqlite3.Connection,
    *,
    asset: str,
    event_id: str,
    run_id: str = "run-1",
    bad_hash: bool = False,
) -> None:
    contract_type = (
        "call_options"
        if asset == "BTC"
        else "put_options"
    )

    payload = {
        "asset": asset,
        "candle_close_boundary_ns": 0,
        "current_chain_state": {
            "underlying": asset,
            "captured_ns": (
                100
                if asset == "BTC"
                else 200
            ),
            "contracts": [
                {
                    "symbol": f"{asset}-TEST",
                    "contract_type": contract_type,
                    "iv": 0.4,
                    "mark_price": 10.0,
                    "open_interest": 100.0,
                    "volume": 20.0,
                    "bid_size": 5.0,
                    "ask_size": 4.0,
                }
            ],
        },
    }

    payload_json = canonical_json(payload)

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
            ?,
            'exp-1',
            ?,
            ?,
            ?,
            'evaluation_completed',
            10,
            NULL,
            ?,
            ?
        )
        """,
        (
            event_id,
            run_id,
            f"{asset.lower()}-slot",
            f"attempt-{asset.lower()}",
            payload_json,
            (
                "bad-hash"
                if bad_hash
                else sha256_text(payload_json)
            ),
        ),
    )


def test_returns_none_without_completed_cycle(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    assert (
        load_last_committed_observer_state(
            path,
            experiment_id="exp-1",
        )
        is None
    )


def test_recovers_latest_completed_cycle_and_ignores_later_abort(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        _insert_evaluation(
            con,
            asset="BTC",
            event_id="btc-eval",
        )
        _insert_evaluation(
            con,
            asset="ETH",
            event_id="eth-eval",
        )

        completed, completed_ref = (
            build_cycle_terminal_statement(
                experiment_id="exp-1",
                run_id="run-1",
                cycle_id="cycle-1",
                status="completed",
                candle_close_boundary_ns=0,
                evaluation_event_ids=(
                    "btc-eval",
                    "eth-eval",
                ),
                event_time_utc_ns=20,
            )
        )

        con.execute(*completed)

        aborted, _ = build_cycle_terminal_statement(
            experiment_id="exp-1",
            run_id="run-1",
            cycle_id="cycle-2",
            status="aborted",
            candle_close_boundary_ns=0,
            evaluation_event_ids=(
                "btc-eval",
            ),
            event_time_utc_ns=30,
            failure_stage="eth_evaluation",
        )

        con.execute(*aborted)

    recovered = load_last_committed_observer_state(
        path,
        experiment_id="exp-1",
    )

    assert recovered is not None
    assert (
        recovered.cycle_event_id
        == completed_ref.event_id
    )
    assert recovered.candle_close_boundary_ns == 0

    assert [
        chain.underlying
        for chain in recovered.chains
    ] == [
        "BTC",
        "ETH",
    ]

    assert [
        chain.captured_ns
        for chain in recovered.chains
    ] == [
        100,
        200,
    ]

    assert (
        recovered.chains[0].contracts[0].symbol
        == "BTC-TEST"
    )

    assert (
        recovered.chains[1].contracts[0].symbol
        == "ETH-TEST"
    )


def test_rejects_corrupted_referenced_evaluation_payload(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)

    with sqlite3.connect(path) as con:
        con.execute("PRAGMA foreign_keys = ON")

        _insert_evaluation(
            con,
            asset="BTC",
            event_id="btc-eval",
            bad_hash=True,
        )
        _insert_evaluation(
            con,
            asset="ETH",
            event_id="eth-eval",
        )

        completed, _ = (
            build_cycle_terminal_statement(
                experiment_id="exp-1",
                run_id="run-1",
                cycle_id="cycle-1",
                status="completed",
                candle_close_boundary_ns=0,
                evaluation_event_ids=(
                    "btc-eval",
                    "eth-eval",
                ),
                event_time_utc_ns=20,
            )
        )

        con.execute(*completed)

    with pytest.raises(
        RuntimeError,
        match="evaluation payload hash mismatch",
    ):
        load_last_committed_observer_state(
            path,
            experiment_id="exp-1",
        )
