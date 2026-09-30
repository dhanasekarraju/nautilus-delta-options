from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nautilus_delta_options.delta.history import (
    DeltaCandle,
    DeltaCandleSnapshot,
)
from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
)
from nautilus_delta_options.research.experiment1.capture import (
    CapturedHistorySnapshot,
    CapturedOptionChainSnapshot,
    RawPublicResponse,
)
from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
)
from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
)
from nautilus_delta_options.research.experiment1.service import (
    enqueue_completed_evaluation,
)
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Config,
    V34ContractState,
    V34Decision,
    V34FlowState,
    V34ShadowSignal,
    V34UnderlyingState,
)

_CLOSE_NS = 1_800_000_000 * 1_000_000_000
_OPEN_NS = _CLOSE_NS - 300 * 1_000_000_000


def _seed_manifest_and_slot(
    database_path: Path,
    slot: EvaluationSlot,
) -> None:
    with sqlite3.connect(database_path) as con:
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
                ?, 'v1', 'protocol-sha', 'manifest-sha', 'schedule-sha',
                ?, ?, ?, ?,
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
                slot.experiment_id,
                _CLOSE_NS,
                _CLOSE_NS + 300_000_000_000,
                _CLOSE_NS + 2_100_000_000_000,
                _CLOSE_NS + 2_160_000_000_000,
            ),
        )

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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot.slot_id,
                slot.experiment_id,
                slot.asset,
                slot.source_symbol,
                slot.candle_open_ns,
                slot.candle_close_boundary_ns,
                slot.scheduled_evaluation_boundary_ns,
                slot.endpoint_close_boundary_ns,
            ),
        )


def _slot() -> EvaluationSlot:
    return EvaluationSlot(
        slot_id="slot-1",
        experiment_id="exp-1",
        asset="BTC",
        source_symbol="BTCUSD",
        candle_open_ns=_OPEN_NS,
        candle_close_boundary_ns=_CLOSE_NS,
        scheduled_evaluation_boundary_ns=_CLOSE_NS,
        endpoint_close_boundary_ns=(
            _CLOSE_NS + 1_800_000_000_000
        ),
    )


def _raw_response(
    url: str,
    payload: object,
    *,
    started_ns: int,
    received_ns: int,
) -> RawPublicResponse:
    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    return RawPublicResponse(
        url=url,
        request_started_utc_ns=started_ns,
        response_received_utc_ns=received_ns,
        request_elapsed_ns=received_ns - started_ns,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
    )


def _history() -> CapturedHistorySnapshot:
    candles = (
        DeltaCandle(
            time_s=1_799_999_400,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("10"),
        ),
        DeltaCandle(
            time_s=1_799_999_700,
            open=Decimal("101"),
            high=Decimal("103"),
            low=Decimal("100"),
            close=Decimal("102"),
            volume=Decimal("11"),
        ),
    )

    payload = {
        "success": True,
        "result": [
            {
                "time": 1_799_999_400,
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "10",
            },
            {
                "time": "1799999700",
                "open": "101",
                "high": "103",
                "low": "100",
                "close": "102",
                "volume": "11",
            },
        ],
    }

    response = _raw_response(
        "https://api.india.delta.exchange/v2/history/candles"
        "?resolution=5m&symbol=BTCUSD",
        payload,
        started_ns=1_900_000_000_000_000_000,
        received_ns=1_900_000_000_100_000_000,
    )

    return CapturedHistorySnapshot(
        response=response,
        snapshot=DeltaCandleSnapshot(
            underlying="BTC",
            symbol="BTCUSD",
            resolution="5m",
            candles=candles,
            captured_ns=response.request_started_utc_ns,
        ),
    )


def _option_ticker() -> DeltaOptionTicker:
    return DeltaOptionTicker(
        product_id=12345,
        symbol="C-BTC-100-011026",
        underlying="BTC",
        contract_type="call_options",
        strike_price=Decimal("100"),
        expiry=date(2026, 10, 1),
        mark_price=Decimal("10"),
        spot_price=Decimal("102"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal("9.8"),
        best_ask=Decimal("10.2"),
        bid_size=Decimal("50"),
        ask_size=Decimal("45"),
        mark_iv=Decimal("0.40"),
        bid_iv=Decimal("0.39"),
        ask_iv=Decimal("0.41"),
        delta=Decimal("0.30"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.02"),
        rho=Decimal("0.01"),
        vega=Decimal("0.05"),
        open_interest_contracts=Decimal("100"),
        volume=Decimal("20"),
        exchange_timestamp=1_900_000_000_250,
        trading_status="operational",
    )


def _option_chain() -> CapturedOptionChainSnapshot:
    ticker = _option_ticker()

    payload = {
        "success": True,
        "result": [
            {
                "symbol": ticker.symbol,
                "product_id": ticker.product_id,
            }
        ],
    }

    response = _raw_response(
        "https://api.india.delta.exchange/v2/tickers"
        "?underlying_asset_symbols=BTC",
        payload,
        started_ns=1_900_000_000_200_000_000,
        received_ns=1_900_000_000_300_000_000,
    )

    return CapturedOptionChainSnapshot(
        response=response,
        snapshot=DeltaOptionChainSnapshot(
            underlying="BTC",
            tickers=(ticker,),
            rejected_records=("record 7: test rejection",),
        ),
    )


def _signal() -> V34ShadowSignal:
    current_chain = V34ChainState(
        underlying="BTC",
        captured_ns=1_900_000_000_400_000_000,
        contracts=(
            V34ContractState(
                symbol="C-BTC-100-011026",
                contract_type="call_options",
                iv=0.4,
                mark_price=10.0,
                open_interest=100.0,
                volume=20.0,
                bid_size=50.0,
                ask_size=45.0,
            ),
        ),
    )

    return V34ShadowSignal(
        underlying="BTC",
        candle_close_ms=1_799_999_999_999,
        decision=V34Decision.CALL,
        call_score=80.0,
        put_score=35.0,
        confidence=80.0,
        underlying_state=V34UnderlyingState(
            close=102.0,
            rsi=55.0,
            ema20=101.0,
            ema50=100.0,
            adx=25.0,
            atr_pct=0.02,
            ema20_slope_atr=0.3,
            return_5m=0.01,
            return_15m=0.02,
            return_30m=0.03,
            extension_atr=0.5,
            call_score=60.0,
            put_score=20.0,
        ),
        flow_state=V34FlowState(
            call_score=20.0,
            put_score=15.0,
            call_confirmations=2,
            put_confirmations=1,
            iv_edge=0.1,
            oi_edge=0.2,
            volume_edge=0.3,
            depth_edge=0.4,
            premium_edge=0.5,
        ),
        chain_state=current_chain,
        reasons=("test_reason",),
    )


def test_completed_evaluation_is_persisted_as_one_atomic_envelope(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )

    slot = _slot()
    _seed_manifest_and_slot(database_path, slot)

    history = _history()
    option_chain = _option_chain()
    signal = _signal()

    refs = enqueue_completed_evaluation(
        journal,
        experiment_id="exp-1",
        run_id="run-1",
        slot=slot,
        attempt_id="attempt-1",
        population_status="canonical",
        retry_ordinal=0,
        history=history,
        option_chain=option_chain,
        evaluated_option_chain=option_chain.snapshot,
        signal=signal,
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 28),
        event_time_utc_ns=1_900_000_000_500_000_000,
        monotonic_elapsed_ns=500_000_000,
    )

    assert refs is not None

    queued = journal.stats()
    assert queued.accepted == 1
    assert queued.rejected == 0
    assert queued.written == 0

    journal.start()
    assert journal.close(timeout_seconds=2.0)

    final = journal.stats()
    assert final.accepted == 1
    assert final.written == 1
    assert final.fatal_error is None

    with sqlite3.connect(database_path) as con:
        responses = con.execute(
            """
            SELECT
                response_kind,
                request_url,
                source_symbol,
                source_payload_sha256,
                raw_response_body
            FROM source_responses
            ORDER BY response_kind
            """
        ).fetchall()

        observations = con.execute(
            """
            SELECT
                observation_id,
                source_timestamp_raw,
                normalized_open_ns,
                normalized_close_boundary_ns,
                raw_candle_json
            FROM candle_observations
            ORDER BY normalized_open_ns
            """
        ).fetchall()

        inputs = con.execute(
            """
            SELECT ordinal, observation_id
            FROM evaluation_candle_inputs
            WHERE attempt_id = 'attempt-1'
            ORDER BY ordinal
            """
        ).fetchall()

        event = con.execute(
            """
            SELECT
                event_type,
                payload_json,
                payload_sha256
            FROM journal_events
            WHERE event_id = ?
            """,
            (refs.event_id,),
        ).fetchone()

        population_binding = con.execute(
            """
            SELECT
                binding_kind,
                canonical_event_id,
                canonical_attempt_id,
                canonical_decision,
                canonical_captured_ns
            FROM population_bindings
            WHERE slot_id = ?
            """,
            (slot.slot_id,),
        ).fetchone()

    assert [row[0] for row in responses] == [
        "history",
        "option_chain",
    ]

    assert responses[0][4] == history.response.body
    assert responses[1][4] == option_chain.response.body

    assert len(observations) == 2
    assert observations[0][1] == "1799999400"
    assert observations[1][1] == '"1799999700"'

    assert inputs == [
        (0, observations[0][0]),
        (1, observations[1][0]),
    ]

    assert refs.p0_observation_id == observations[-1][0]

    assert event is not None
    assert event[0] == "evaluation_completed"

    assert population_binding == (
        "canonical",
        refs.event_id,
        "attempt-1",
        "call",
        signal.chain_state.captured_ns,
    )

    payload = json.loads(event[1])

    assert payload["decision"] == "call"
    assert payload["call_score"] == 80.0
    assert payload["put_score"] == 35.0
    assert payload["flow_state"]["call_confirmations"] == 2
    assert payload["reasons"] == ["test_reason"]
    assert payload["previous_chain_state"] is None

    evaluated = payload["evaluated_option_chain"]
    assert evaluated["underlying"] == "BTC"
    assert evaluated["rejected_records"] == [
        "record 7: test rejection"
    ]
    assert len(evaluated["tickers"]) == 1
    assert evaluated["tickers"][0]["symbol"] == (
        "C-BTC-100-011026"
    )
    assert evaluated["tickers"][0]["best_bid"] == "9.8"
    assert evaluated["tickers"][0]["best_ask"] == "10.2"
    assert evaluated["tickers"][0]["delta"] == "0.30"

    assert payload["candle_input_count"] == 2
    assert payload["p0_observation_id"] == refs.p0_observation_id
    assert payload["population_status"] == "canonical"
    assert payload["retry_ordinal"] == 0
    assert payload["canonical_decision"] == "call"
    assert payload["canonical_captured_ns"] == (
        signal.chain_state.captured_ns
    )

    assert event[2] == hashlib.sha256(
        event[1].encode("utf-8")
    ).hexdigest()


def test_evaluation_validation_fails_before_queue_admission(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )

    slot = _slot()

    bad_slot = EvaluationSlot(
        slot_id=slot.slot_id,
        experiment_id=slot.experiment_id,
        asset="ETH",
        source_symbol="ETHUSD",
        candle_open_ns=slot.candle_open_ns,
        candle_close_boundary_ns=slot.candle_close_boundary_ns,
        scheduled_evaluation_boundary_ns=(
            slot.scheduled_evaluation_boundary_ns
        ),
        endpoint_close_boundary_ns=slot.endpoint_close_boundary_ns,
    )

    option_chain = _option_chain()

    with pytest.raises(
        ValueError,
        match="history underlying does not match slot",
    ):
        enqueue_completed_evaluation(
            journal,
            experiment_id="exp-1",
            run_id="run-1",
            slot=bad_slot,
            attempt_id="attempt-1",
            population_status="canonical",
            retry_ordinal=0,
            history=_history(),
            option_chain=option_chain,
            evaluated_option_chain=option_chain.snapshot,
            signal=_signal(),
            previous_chain=None,
            config=V34Config(),
            as_of=date(2026, 9, 28),
            event_time_utc_ns=1_900_000_000_500_000_000,
        )

    stats = journal.stats()
    assert stats.accepted == 0
    assert stats.rejected == 0
    assert stats.written == 0


def test_evaluation_envelope_rejection_persists_nothing(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        max_queue_bytes=1,
        start_immediately=False,
    )

    slot = _slot()
    _seed_manifest_and_slot(database_path, slot)
    option_chain = _option_chain()

    refs = enqueue_completed_evaluation(
        journal,
        experiment_id="exp-1",
        run_id="run-1",
        slot=slot,
        attempt_id="attempt-1",
        population_status="canonical",
        retry_ordinal=0,
        history=_history(),
        option_chain=option_chain,
        evaluated_option_chain=option_chain.snapshot,
        signal=_signal(),
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 28),
        event_time_utc_ns=1_900_000_000_500_000_000,
    )

    assert refs is None

    stats = journal.stats()
    assert stats.accepted == 0
    assert stats.rejected == 1
    assert stats.written == 0

    with sqlite3.connect(database_path) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM source_responses"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM candle_observations"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM evaluation_candle_inputs"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM journal_events"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM population_bindings"
        ).fetchone()[0] == 0




def test_retry_evidence_does_not_replace_canonical_binding(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )

    slot = _slot()
    _seed_manifest_and_slot(database_path, slot)

    history = _history()
    option_chain = _option_chain()
    signal = _signal()

    canonical_refs = enqueue_completed_evaluation(
        journal,
        experiment_id="exp-1",
        run_id="run-1",
        slot=slot,
        attempt_id="attempt-1",
        population_status="canonical",
        retry_ordinal=0,
        history=history,
        option_chain=option_chain,
        evaluated_option_chain=option_chain.snapshot,
        signal=signal,
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 28),
        event_time_utc_ns=1_900_000_000_500_000_000,
    )

    assert canonical_refs is not None

    retry_refs = enqueue_completed_evaluation(
        journal,
        experiment_id="exp-1",
        run_id="run-1",
        slot=slot,
        attempt_id="attempt-2",
        population_status="same_decision_retry",
        retry_ordinal=1,
        history=history,
        option_chain=option_chain,
        evaluated_option_chain=option_chain.snapshot,
        signal=signal,
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 28),
        event_time_utc_ns=1_900_000_000_600_000_000,
        canonical_decision=signal.decision,
        canonical_captured_ns=signal.chain_state.captured_ns,
    )

    assert retry_refs is not None

    journal.start()
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(database_path) as con:
        binding = con.execute(
            """
            SELECT
                canonical_event_id,
                canonical_attempt_id,
                canonical_decision,
                canonical_captured_ns
            FROM population_bindings
            WHERE slot_id = ?
            """,
            (slot.slot_id,),
        ).fetchone()

        events = con.execute(
            """
            SELECT
                event_id,
                attempt_id,
                payload_json
            FROM journal_events
            WHERE event_type = 'evaluation_completed'
            ORDER BY sequence
            """
        ).fetchall()

    assert binding == (
        canonical_refs.event_id,
        "attempt-1",
        "call",
        signal.chain_state.captured_ns,
    )

    assert [row[0] for row in events] == [
        canonical_refs.event_id,
        retry_refs.event_id,
    ]

    assert [row[1] for row in events] == [
        "attempt-1",
        "attempt-2",
    ]

    retry_payload = json.loads(events[1][2])

    assert (
        retry_payload["population_status"]
        == "same_decision_retry"
    )
    assert retry_payload["retry_ordinal"] == 1
    assert retry_payload["canonical_decision"] == "call"
    assert retry_payload["canonical_captured_ns"] == (
        signal.chain_state.captured_ns
    )


def test_backward_wall_clock_response_is_preserved_without_rewrite(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )

    slot = _slot()
    _seed_manifest_and_slot(database_path, slot)

    normal_history = _history()

    backward_response = RawPublicResponse(
        url=normal_history.response.url,
        request_started_utc_ns=1_900_000_000_500_000_000,
        response_received_utc_ns=1_900_000_000_400_000_000,
        request_elapsed_ns=100_000_000,
        body=normal_history.response.body,
        sha256=normal_history.response.sha256,
    )

    assert backward_response.clock_anomaly

    history = CapturedHistorySnapshot(
        response=backward_response,
        snapshot=DeltaCandleSnapshot(
            underlying=normal_history.snapshot.underlying,
            symbol=normal_history.snapshot.symbol,
            resolution=normal_history.snapshot.resolution,
            candles=normal_history.snapshot.candles,
            captured_ns=backward_response.request_started_utc_ns,
        ),
    )

    option_chain = _option_chain()

    refs = enqueue_completed_evaluation(
        journal,
        experiment_id="exp-1",
        run_id="run-clock-anomaly",
        slot=slot,
        attempt_id="attempt-clock-anomaly",
        population_status="canonical",
        retry_ordinal=0,
        history=history,
        option_chain=option_chain,
        evaluated_option_chain=option_chain.snapshot,
        signal=_signal(),
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 30),
        event_time_utc_ns=1_900_000_000_600_000_000,
    )

    assert refs is not None

    journal.start()
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(database_path) as con:
        source_row = con.execute(
            """
            SELECT
                request_started_utc_ns,
                response_received_utc_ns,
                request_elapsed_ns,
                clock_anomaly
            FROM source_responses
            WHERE response_kind = 'history'
            """
        ).fetchone()

        candle_rows = con.execute(
            """
            SELECT DISTINCT
                request_started_utc_ns,
                response_received_utc_ns,
                request_elapsed_ns
            FROM candle_observations
            """
        ).fetchall()

    assert source_row == (
        1_900_000_000_500_000_000,
        1_900_000_000_400_000_000,
        100_000_000,
        1,
    )

    assert candle_rows == [
        (
            1_900_000_000_500_000_000,
            1_900_000_000_400_000_000,
            100_000_000,
        )
    ]


def test_excluded_gap_evaluation_preserves_gap_binding(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )

    slot = _slot()
    _seed_manifest_and_slot(database_path, slot)

    with sqlite3.connect(database_path) as con:
        con.execute("PRAGMA foreign_keys = ON")

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
                ?, 'exp-1', 'run-restart', 'gap',
                NULL, NULL, NULL, NULL,
                'protocol_cutoff_gap',
                1
            )
            """,
            (slot.slot_id,),
        )

    option_chain = _option_chain()

    refs = enqueue_completed_evaluation(
        journal,
        experiment_id="exp-1",
        run_id="run-restart",
        slot=slot,
        attempt_id="post-gap-attempt",
        population_status="excluded_by_gap",
        retry_ordinal=0,
        history=_history(),
        option_chain=option_chain,
        evaluated_option_chain=option_chain.snapshot,
        signal=_signal(),
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 28),
        event_time_utc_ns=1_900_000_000_700_000_000,
    )

    assert refs is not None

    journal.start()
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(database_path) as con:
        binding = con.execute(
            """
            SELECT
                binding_kind,
                canonical_event_id,
                reason
            FROM population_bindings
            WHERE slot_id = ?
            """,
            (slot.slot_id,),
        ).fetchone()

        event_payload = con.execute(
            """
            SELECT payload_json
            FROM journal_events
            WHERE event_id = ?
            """,
            (refs.event_id,),
        ).fetchone()

    assert binding == (
        "gap",
        None,
        "protocol_cutoff_gap",
    )

    assert event_payload is not None

    payload = json.loads(event_payload[0])

    assert payload["population_status"] == "excluded_by_gap"
    assert payload["retry_ordinal"] == 0
    assert payload["canonical_decision"] is None
    assert payload["canonical_captured_ns"] is None
