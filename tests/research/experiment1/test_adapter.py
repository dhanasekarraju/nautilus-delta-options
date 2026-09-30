from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from nautilus_delta_options.delta.history import (
    DeltaCandle,
    DeltaCandleSnapshot,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
)
from nautilus_delta_options.research.experiment1 import (
    adapter as adapter_module,
)
from nautilus_delta_options.research.experiment1.adapter import (
    Experiment1EvaluationAdapter,
)
from nautilus_delta_options.research.experiment1.capture import (
    CapturedHistorySnapshot,
    CapturedOptionChainSnapshot,
    RawPublicResponse,
)
from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
)
from nautilus_delta_options.research.experiment1.manifest import (
    build_schedule,
)
from nautilus_delta_options.research.experiment1.market_data import (
    Experiment1CapturedMarketData,
)
from nautilus_delta_options.research.experiment1.ownership import (
    CollectorLease,
)
from nautilus_delta_options.research.experiment1.state_machine import (
    EvaluationAttemptContext,
    EvaluationReturn,
)
from nautilus_delta_options.signals.v34 import (
    V34Config,
    V34Decision,
    V34ShadowSignal,
)

_STEP = 300_000_000_000
_WINDOW_START = 1_800_000_000_000_000_000
_WINDOW_END = _WINDOW_START + _STEP


def _database(
    tmp_path: Path,
    slots,
) -> Path:
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
                'v1',
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
                _WINDOW_START,
                _WINDOW_END,
                _WINDOW_END
                + 1_800_000_000_000,
                _WINDOW_END
                + 1_860_000_000_000,
            ),
        )

        for slot in slots:
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

    return path


def _context(
    underlying: str,
) -> EvaluationAttemptContext:
    close_boundary_ns = _WINDOW_START
    open_ns = close_boundary_ns - _STEP

    candle = DeltaCandle(
        time_s=open_ns // 1_000_000_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
    )

    candles = DeltaCandleSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        symbol=f"{underlying}USD",
        resolution="5m",
        candles=(candle,),
        captured_ns=1,
    )

    source_chain = DeltaOptionChainSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        tickers=(),
        rejected_records=(),
    )

    evaluated_chain = DeltaOptionChainSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        tickers=(),
        rejected_records=(),
    )

    return EvaluationAttemptContext(
        underlying=underlying,  # type: ignore[arg-type]
        candles=candles,
        source_option_chain=source_chain,
        evaluated_option_chain=evaluated_chain,
        previous_chain=None,
        as_of=date(2026, 9, 30),
        captured_ns=123,
    )


def test_adapter_maps_context_to_prebuilt_slot(
    tmp_path: Path,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: 100,
            attempt_id_factory=lambda: "attempt-1",
        )

        slot = adapter.slot_for_context(
            _context("BTC")
        )

    assert slot.asset == "BTC"
    assert (
        slot.candle_close_boundary_ns
        == _WINDOW_START
    )


def test_attempt_start_is_durable_and_bound_to_slot(
    tmp_path: Path,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: 100,
            attempt_id_factory=lambda: "attempt-1",
        )

        attempt_id = adapter.on_attempt_start(
            _context("BTC")
        )

        binding = adapter.binding_for_attempt(
            attempt_id
        )

        assert binding.slot.asset == "BTC"

        with sqlite3.connect(path) as con:
            row = con.execute(
                """
                SELECT
                    event_type,
                    slot_id,
                    attempt_id
                FROM journal_events
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()

    assert row == (
        "evaluation_started",
        binding.slot.slot_id,
        "attempt-1",
    )


def test_evaluator_failure_uses_same_attempt_id(
    tmp_path: Path,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)

    times = iter((100, 101))

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: next(times),
            attempt_id_factory=lambda: "attempt-1",
        )

        context = _context("BTC")

        attempt_id = adapter.on_attempt_start(
            context
        )

        assert adapter.on_attempt_failure(
            context,
            attempt_id,
            RuntimeError("synthetic evaluator error"),
        )

        with pytest.raises(
            RuntimeError,
            match="unknown active",
        ):
            adapter.binding_for_attempt(
                attempt_id
            )

        with sqlite3.connect(path) as con:
            rows = con.execute(
                """
                SELECT event_type, attempt_id
                FROM journal_events
                WHERE attempt_id = ?
                ORDER BY sequence
                """,
                (attempt_id,),
            ).fetchall()

    assert rows == [
        ("evaluation_started", "attempt-1"),
        ("evaluation_failed", "attempt-1"),
    ]


def test_missing_schedule_slot_is_rejected(
    tmp_path: Path,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)

    later_context = _context("BTC")

    object.__setattr__(
        later_context.candles,
        "candles",
        (
            DeltaCandle(
                time_s=(
                    _WINDOW_START
                    // 1_000_000_000
                ),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10"),
            ),
        ),
    )

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: 100,
        )

        with pytest.raises(
            RuntimeError,
            match="no scheduled Experiment 1 slot",
        ):
            adapter.slot_for_context(
                later_context
            )


def test_completed_attempt_is_removed_only_explicitly(
    tmp_path: Path,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: 100,
            attempt_id_factory=lambda: "attempt-1",
        )

        attempt_id = adapter.on_attempt_start(
            _context("ETH")
        )

        binding = adapter.complete_attempt(
            attempt_id
        )

        assert binding.slot.asset == "ETH"

        with pytest.raises(
            RuntimeError,
            match="unknown active",
        ):
            adapter.binding_for_attempt(
                attempt_id
            )



class _RetainedMarketData:
    def __init__(
        self,
        context: EvaluationAttemptContext,
    ) -> None:
        response = RawPublicResponse(
            url=(
                "https://api.india.delta.exchange/"
                "v2/history/candles"
            ),
            request_started_utc_ns=10,
            response_received_utc_ns=11,
            request_elapsed_ns=1,
            body=b"{}",
            sha256="raw-sha",
        )

        option_response = RawPublicResponse(
            url=(
                "https://api.india.delta.exchange/"
                "v2/tickers"
            ),
            request_started_utc_ns=12,
            response_received_utc_ns=13,
            request_elapsed_ns=1,
            body=b"{}",
            sha256="option-sha",
        )

        self.history = CapturedHistorySnapshot(
            response=response,
            snapshot=context.candles,
        )

        self.option_chain = CapturedOptionChainSnapshot(
            response=option_response,
            snapshot=context.source_option_chain,
        )

    def captured_history(
        self,
        underlying: str,
    ) -> CapturedHistorySnapshot:
        assert underlying == self.history.snapshot.underlying
        return self.history

    def captured_option_chain(
        self,
        underlying: str,
    ) -> CapturedOptionChainSnapshot:
        assert (
            underlying
            == self.option_chain.snapshot.underlying
        )
        return self.option_chain


def _evaluation_return(
    context: EvaluationAttemptContext,
    *,
    attempt_id: str | None,
    population_status: str = "canonical",
) -> EvaluationReturn:
    signal = cast(
        V34ShadowSignal,
        SimpleNamespace(
            underlying=context.underlying,
            candle_close_ms=(
                context.candles.candle_close_ms
            ),
            decision=V34Decision.WAIT,
        ),
    )

    return EvaluationReturn(
        underlying=context.underlying,
        candles=context.candles,
        source_option_chain=context.source_option_chain,
        evaluated_option_chain=(
            context.evaluated_option_chain
        ),
        previous_chain=context.previous_chain,
        signal=signal,
        as_of=context.as_of,
        captured_ns=context.captured_ns,
        attempt_id=attempt_id,
        population_status=population_status,  # type: ignore[arg-type]
        retry_ordinal=0,
        canonical_decision=(
            V34Decision.WAIT
            if population_status == "canonical"
            else None
        ),
        canonical_captured_ns=(
            context.captured_ns
            if population_status == "canonical"
            else None
        ),
    )


def test_successful_return_uses_same_durable_attempt_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)
    context = _context("BTC")
    retained = _RetainedMarketData(context)

    seen: dict[str, object] = {}

    def fake_enqueue(
        journal,
        **kwargs,
    ):
        seen["journal"] = journal
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(
        adapter_module,
        "enqueue_completed_evaluation",
        fake_enqueue,
    )

    journal_token = object()

    times = iter((100, 101))

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: next(times),
            attempt_id_factory=lambda: "attempt-1",
            journal=journal_token,  # type: ignore[arg-type]
            market_data=cast(
                Experiment1CapturedMarketData,
                retained,
            ),
            config=V34Config(),
        )

        attempt_id = adapter.on_attempt_start(
            context
        )

        result = _evaluation_return(
            context,
            attempt_id=attempt_id,
        )

        assert adapter.on_evaluation_return(
            result
        )

        assert seen["journal"] is journal_token
        assert seen["attempt_id"] == "attempt-1"
        assert seen["slot"] == slots[0]
        assert seen["history"] is retained.history
        assert (
            seen["option_chain"]
            is retained.option_chain
        )
        assert (
            seen["evaluated_option_chain"]
            is context.evaluated_option_chain
        )
        assert seen["signal"] is result.signal

        with pytest.raises(
            RuntimeError,
            match="unknown active",
        ):
            adapter.binding_for_attempt(
                attempt_id
            )


def test_completed_queue_rejection_keeps_attempt_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)
    context = _context("BTC")
    retained = _RetainedMarketData(context)

    monkeypatch.setattr(
        adapter_module,
        "enqueue_completed_evaluation",
        lambda *args, **kwargs: None,
    )

    times = iter((100, 101))

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: next(times),
            attempt_id_factory=lambda: "attempt-1",
            journal=object(),  # type: ignore[arg-type]
            market_data=cast(
                Experiment1CapturedMarketData,
                retained,
            ),
            config=V34Config(),
        )

        attempt_id = adapter.on_attempt_start(
            context
        )

        assert (
            adapter.on_evaluation_return(
                _evaluation_return(
                    context,
                    attempt_id=attempt_id,
                )
            )
            is False
        )

        assert (
            adapter.binding_for_attempt(
                attempt_id
            ).attempt_id
            == attempt_id
        )


def test_return_without_durable_start_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)
    context = _context("BTC")
    retained = _RetainedMarketData(context)

    called = False

    def fake_enqueue(*args, **kwargs):
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(
        adapter_module,
        "enqueue_completed_evaluation",
        fake_enqueue,
    )

    with CollectorLease.acquire(path) as lease:
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: 100,
            journal=object(),  # type: ignore[arg-type]
            market_data=cast(
                Experiment1CapturedMarketData,
                retained,
            ),
            config=V34Config(),
        )

        assert (
            adapter.on_evaluation_return(
                _evaluation_return(
                    context,
                    attempt_id=None,
                    population_status="excluded_by_gap",
                )
            )
            is False
        )

    assert called is False


def test_partial_evidence_configuration_is_rejected(
    tmp_path: Path,
) -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=_WINDOW_START,
        window_end_utc_ns=_WINDOW_END,
    )

    path = _database(tmp_path, slots)

    with CollectorLease.acquire(path) as lease, pytest.raises(
        ValueError,
        match="configured together",
    ):
        Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id="exp-1",
            run_id="run-1",
            slots=slots,
            utc_ns=lambda: 100,
            journal=object(),  # type: ignore[arg-type]
        )
