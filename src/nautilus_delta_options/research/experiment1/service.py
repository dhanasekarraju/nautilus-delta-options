from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date

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
    JournalStatement,
)
from nautilus_delta_options.research.experiment1.manifest import (
    canonical_json,
    sha256_text,
)
from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
    JournalEventType,
)
from nautilus_delta_options.research.experiment1.population import (
    PopulationStatus,
    build_canonical_population_statement,
)
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Config,
    V34Decision,
    V34ShadowSignal,
)

_NS_PER_SECOND = 1_000_000_000
_MS_PER_SECOND = 1_000


@dataclass(frozen=True, slots=True)
class EvaluationEvidenceRefs:
    event_id: str
    history_request_id: str
    history_response_id: str
    option_request_id: str
    option_response_id: str
    p0_observation_id: str


def enqueue_completed_evaluation(
    journal: ExperimentJournal,
    *,
    experiment_id: str,
    run_id: str,
    slot: EvaluationSlot,
    attempt_id: str,
    history: CapturedHistorySnapshot,
    option_chain: CapturedOptionChainSnapshot,
    evaluated_option_chain: DeltaOptionChainSnapshot,
    signal: V34ShadowSignal,
    population_status: PopulationStatus,
    retry_ordinal: int,
    previous_chain: V34ChainState | None,
    config: V34Config,
    as_of: date,
    event_time_utc_ns: int,
    canonical_decision: V34Decision | None = None,
    canonical_captured_ns: int | None = None,
    monotonic_elapsed_ns: int | None = None,
) -> EvaluationEvidenceRefs | None:
    """Persist one successful evaluation as one atomic journal envelope."""

    statements, refs = build_completed_evaluation_statements(
        experiment_id=experiment_id,
        run_id=run_id,
        slot=slot,
        attempt_id=attempt_id,
        history=history,
        option_chain=option_chain,
        evaluated_option_chain=evaluated_option_chain,
        signal=signal,
        population_status=population_status,
        retry_ordinal=retry_ordinal,
        previous_chain=previous_chain,
        config=config,
        as_of=as_of,
        event_time_utc_ns=event_time_utc_ns,
        canonical_decision=canonical_decision,
        canonical_captured_ns=canonical_captured_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    if not journal.enqueue_batch(statements):
        return None

    return refs


def build_completed_evaluation_statements(
    *,
    experiment_id: str,
    run_id: str,
    slot: EvaluationSlot,
    attempt_id: str,
    history: CapturedHistorySnapshot,
    option_chain: CapturedOptionChainSnapshot,
    evaluated_option_chain: DeltaOptionChainSnapshot,
    signal: V34ShadowSignal,
    population_status: PopulationStatus,
    retry_ordinal: int,
    previous_chain: V34ChainState | None,
    config: V34Config,
    as_of: date,
    event_time_utc_ns: int,
    canonical_decision: V34Decision | None = None,
    canonical_captured_ns: int | None = None,
    monotonic_elapsed_ns: int | None = None,
) -> tuple[tuple[JournalStatement, ...], EvaluationEvidenceRefs]:
    """Build the append-only SQL envelope for one completed evaluation."""

    _validate_completed_evaluation(
        experiment_id=experiment_id,
        run_id=run_id,
        slot=slot,
        attempt_id=attempt_id,
        history=history,
        option_chain=option_chain,
        evaluated_option_chain=evaluated_option_chain,
        signal=signal,
        previous_chain=previous_chain,
        event_time_utc_ns=event_time_utc_ns,
        monotonic_elapsed_ns=monotonic_elapsed_ns,
    )

    (
        effective_canonical_decision,
        effective_canonical_captured_ns,
    ) = _normalize_population_metadata(
        signal=signal,
        population_status=population_status,
        retry_ordinal=retry_ordinal,
        canonical_decision=canonical_decision,
        canonical_captured_ns=canonical_captured_ns,
    )

    history_request_id = _stable_id(
        "request",
        experiment_id,
        run_id,
        attempt_id,
        "history",
    )
    option_request_id = _stable_id(
        "request",
        experiment_id,
        run_id,
        attempt_id,
        "option_chain",
    )

    history_response_id = _response_id(
        history_request_id,
        history.response,
    )
    option_response_id = _response_id(
        option_request_id,
        option_chain.response,
    )

    statements: list[JournalStatement] = [
        _source_response_statement(
            response_id=history_response_id,
            experiment_id=experiment_id,
            run_id=run_id,
            request_id=history_request_id,
            response_kind="history",
            asset=slot.asset,
            source_symbol=history.snapshot.symbol,
            response=history.response,
        ),
        _source_response_statement(
            response_id=option_response_id,
            experiment_id=experiment_id,
            run_id=run_id,
            request_id=option_request_id,
            response_kind="option_chain",
            asset=slot.asset,
            source_symbol=slot.asset,
            response=option_chain.response,
        ),
    ]

    raw_by_time = _raw_candle_records(history.response)

    observation_ids: list[str] = []

    for ordinal, candle in enumerate(history.snapshot.candles):
        raw_record = raw_by_time.get(candle.time_s)
        if raw_record is None:
            raise ValueError(
                "parsed candle has no matching raw source record: "
                f"{candle.time_s}"
            )

        observation_id = _stable_id(
            "observation",
            history_response_id,
            candle.time_s,
        )
        observation_ids.append(observation_id)

        statements.append(
            (
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
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    observation_id,
                    experiment_id,
                    run_id,
                    history_request_id,
                    history_response_id,
                    slot.asset,
                    history.snapshot.symbol,
                    canonical_json(raw_record.get("time")),
                    candle.time_s * _NS_PER_SECOND,
                    (
                        candle.time_s + 300
                    )
                    * _NS_PER_SECOND,
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                    str(candle.volume),
                    history.response.request_started_utc_ns,
                    history.response.response_received_utc_ns,
                    history.response.request_elapsed_ns,
                    history.response.sha256,
                    canonical_json(raw_record),
                    "parsed",
                ),
            )
        )

        statements.append(
            (
                """
                INSERT INTO evaluation_candle_inputs (
                    experiment_id,
                    attempt_id,
                    ordinal,
                    observation_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    attempt_id,
                    ordinal,
                    observation_id,
                ),
            )
        )

    if not observation_ids:
        raise ValueError(
            "completed evaluation must contain at least one candle input"
        )

    p0_observation_id = observation_ids[-1]

    event_payload = {
        "payload_version": 1,
        "asset": slot.asset,
        "slot_id": slot.slot_id,
        "attempt_id": attempt_id,
        "candle_close_boundary_ns": slot.candle_close_boundary_ns,
        "candle_close_ms": signal.candle_close_ms,
        "decision": signal.decision.value,
        "call_score": signal.call_score,
        "put_score": signal.put_score,
        "confidence": signal.confidence,
        "reasons": list(signal.reasons),
        "underlying_state": asdict(signal.underlying_state),
        "flow_state": (
            asdict(signal.flow_state)
            if signal.flow_state is not None
            else None
        ),
        "current_chain_state": asdict(signal.chain_state),
        "previous_chain_state": (
            asdict(previous_chain)
            if previous_chain is not None
            else None
        ),
        "config": asdict(config),
        "as_of": as_of.isoformat(),
        "captured_ns": signal.chain_state.captured_ns,
        "transport_history_response_id": history.response.capture_id,
        "transport_option_response_id": option_chain.response.capture_id,
        "history_response_id": history_response_id,
        "option_response_id": option_response_id,
        "option_rejected_records": list(
            option_chain.snapshot.rejected_records
        ),
        "evaluated_option_chain": _option_chain_payload(
            evaluated_option_chain
        ),
        "candle_input_count": len(observation_ids),
        "p0_observation_id": p0_observation_id,
        "population_status": population_status,
        "retry_ordinal": retry_ordinal,
        "canonical_decision": (
            effective_canonical_decision.value
            if effective_canonical_decision is not None
            else None
        ),
        "canonical_captured_ns": (
            effective_canonical_captured_ns
        ),
    }

    payload_json = canonical_json(event_payload)
    payload_sha256 = sha256_text(payload_json)

    event_id = _stable_id(
        "event",
        experiment_id,
        run_id,
        slot.slot_id,
        attempt_id,
        JournalEventType.EVALUATION_COMPLETED.value,
    )

    statements.append(
        (
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
                JournalEventType.EVALUATION_COMPLETED.value,
                event_time_utc_ns,
                monotonic_elapsed_ns,
                payload_json,
                payload_sha256,
            ),
        )
    )

    if population_status == "canonical":
        if (
            effective_canonical_decision is None
            or effective_canonical_captured_ns is None
        ):
            raise RuntimeError(
                "canonical population metadata was not resolved"
            )

        statements.append(
            build_canonical_population_statement(
                experiment_id=experiment_id,
                run_id=run_id,
                slot_id=slot.slot_id,
                event_id=event_id,
                attempt_id=attempt_id,
                decision=effective_canonical_decision,
                captured_ns=effective_canonical_captured_ns,
                recorded_at_utc_ns=event_time_utc_ns,
            )
        )

    refs = EvaluationEvidenceRefs(
        event_id=event_id,
        history_request_id=history_request_id,
        history_response_id=history_response_id,
        option_request_id=option_request_id,
        option_response_id=option_response_id,
        p0_observation_id=p0_observation_id,
    )

    return tuple(statements), refs



def _normalize_population_metadata(
    *,
    signal: V34ShadowSignal,
    population_status: PopulationStatus,
    retry_ordinal: int,
    canonical_decision: V34Decision | None,
    canonical_captured_ns: int | None,
) -> tuple[V34Decision | None, int | None]:
    actual_decision = signal.decision
    actual_captured_ns = signal.chain_state.captured_ns

    if population_status not in (
        "canonical",
        "same_decision_retry",
        "conflicting_retry",
        "excluded_by_gap",
    ):
        raise ValueError("unsupported population_status")

    if retry_ordinal < 0:
        raise ValueError("retry_ordinal cannot be negative")

    if population_status == "excluded_by_gap":
        if canonical_decision is not None:
            raise ValueError(
                "excluded gap cannot have canonical_decision"
            )

        if canonical_captured_ns is not None:
            raise ValueError(
                "excluded gap cannot have canonical_captured_ns"
            )

        return None, None

    if population_status == "canonical":
        if retry_ordinal != 0:
            raise ValueError(
                "canonical evaluator return must have retry_ordinal 0"
            )

        if (
            canonical_decision is not None
            and canonical_decision is not actual_decision
        ):
            raise ValueError(
                "canonical decision must match evaluator result"
            )

        if (
            canonical_captured_ns is not None
            and canonical_captured_ns != actual_captured_ns
        ):
            raise ValueError(
                "canonical captured_ns must match evaluator result"
            )

        return actual_decision, actual_captured_ns

    if retry_ordinal == 0:
        raise ValueError(
            "retry evaluator return must have retry_ordinal > 0"
        )

    if canonical_decision is None:
        raise ValueError(
            "retry requires canonical_decision"
        )

    if canonical_captured_ns is None:
        raise ValueError(
            "retry requires canonical_captured_ns"
        )

    if canonical_captured_ns <= 0:
        raise ValueError(
            "canonical_captured_ns must be positive"
        )

    if (
        population_status == "same_decision_retry"
        and canonical_decision is not actual_decision
    ):
        raise ValueError(
            "same_decision_retry must match canonical decision"
        )

    if (
        population_status == "conflicting_retry"
        and canonical_decision is actual_decision
    ):
        raise ValueError(
            "conflicting_retry must differ from canonical decision"
        )

    return canonical_decision, canonical_captured_ns


def _validate_completed_evaluation(
    *,
    experiment_id: str,
    run_id: str,
    slot: EvaluationSlot,
    attempt_id: str,
    history: CapturedHistorySnapshot,
    option_chain: CapturedOptionChainSnapshot,
    evaluated_option_chain: DeltaOptionChainSnapshot,
    signal: V34ShadowSignal,
    previous_chain: V34ChainState | None,
    event_time_utc_ns: int,
    monotonic_elapsed_ns: int | None,
) -> None:
    if not experiment_id:
        raise ValueError("experiment_id must be non-empty")
    if slot.experiment_id != experiment_id:
        raise ValueError("slot experiment does not match")
    if not run_id:
        raise ValueError("run_id must be non-empty")
    if not attempt_id:
        raise ValueError("attempt_id must be non-empty")

    underlying = slot.asset

    if history.snapshot.underlying != underlying:
        raise ValueError("history underlying does not match slot")
    if option_chain.snapshot.underlying != underlying:
        raise ValueError("option-chain underlying does not match slot")
    if evaluated_option_chain.underlying != underlying:
        raise ValueError(
            "evaluated option-chain underlying does not match slot"
        )
    if not evaluated_option_chain.tickers:
        raise ValueError("evaluated option chain must not be empty")
    if any(
        ticker not in option_chain.snapshot.tickers
        for ticker in evaluated_option_chain.tickers
    ):
        raise ValueError(
            "evaluated option chain contains ticker absent from captured chain"
        )
    if (
        evaluated_option_chain.rejected_records
        != option_chain.snapshot.rejected_records
    ):
        raise ValueError(
            "evaluated option-chain rejection provenance changed"
        )
    if signal.underlying != underlying:
        raise ValueError("signal underlying does not match slot")
    if signal.chain_state.underlying != underlying:
        raise ValueError("signal chain underlying does not match slot")
    if history.snapshot.symbol != slot.source_symbol:
        raise ValueError("history source symbol does not match slot")
    if history.snapshot.resolution != "5m":
        raise ValueError("history resolution must remain five minutes")

    if previous_chain is not None and previous_chain.underlying != underlying:
        raise ValueError("previous chain underlying does not match slot")

    if not history.snapshot.candles:
        raise ValueError("history snapshot is empty")

    last_candle = history.snapshot.candles[-1]

    if last_candle.time_s * _NS_PER_SECOND != slot.candle_open_ns:
        raise ValueError("signal candle open does not match slot")

    expected_close_ms = (
        slot.candle_close_boundary_ns
        // (_NS_PER_SECOND // _MS_PER_SECOND)
        - 1
    )

    if history.snapshot.candle_close_ms != expected_close_ms:
        raise ValueError("history candle close does not match slot")
    if signal.candle_close_ms != expected_close_ms:
        raise ValueError("signal candle close does not match slot")

    if signal.chain_state.captured_ns <= 0:
        raise ValueError("signal captured_ns must be positive")

    if event_time_utc_ns <= 0:
        raise ValueError("event_time_utc_ns must be positive")

    latest_response_ns = max(
        history.response.response_received_utc_ns,
        option_chain.response.response_received_utc_ns,
    )
    if event_time_utc_ns < latest_response_ns:
        raise ValueError(
            "completed event cannot precede source response receipt"
        )

    if monotonic_elapsed_ns is not None and monotonic_elapsed_ns < 0:
        raise ValueError("monotonic_elapsed_ns cannot be negative")



def _option_chain_payload(
    chain: DeltaOptionChainSnapshot,
) -> dict[str, object]:
    return {
        "underlying": chain.underlying,
        "rejected_records": list(chain.rejected_records),
        "tickers": [
            _option_ticker_payload(ticker)
            for ticker in chain.tickers
        ],
    }


def _option_ticker_payload(
    ticker: DeltaOptionTicker,
) -> dict[str, object]:
    def optional_decimal(value: object | None) -> str | None:
        return None if value is None else str(value)

    return {
        "product_id": ticker.product_id,
        "symbol": ticker.symbol,
        "underlying": ticker.underlying,
        "contract_type": ticker.contract_type,
        "strike_price": str(ticker.strike_price),
        "expiry": ticker.expiry.isoformat(),
        "mark_price": str(ticker.mark_price),
        "spot_price": str(ticker.spot_price),
        "contract_value": str(ticker.contract_value),
        "tick_size": str(ticker.tick_size),
        "best_bid": optional_decimal(ticker.best_bid),
        "best_ask": optional_decimal(ticker.best_ask),
        "bid_size": optional_decimal(ticker.bid_size),
        "ask_size": optional_decimal(ticker.ask_size),
        "mark_iv": optional_decimal(ticker.mark_iv),
        "bid_iv": optional_decimal(ticker.bid_iv),
        "ask_iv": optional_decimal(ticker.ask_iv),
        "delta": optional_decimal(ticker.delta),
        "gamma": optional_decimal(ticker.gamma),
        "theta": optional_decimal(ticker.theta),
        "rho": optional_decimal(ticker.rho),
        "vega": optional_decimal(ticker.vega),
        "open_interest_contracts": str(
            ticker.open_interest_contracts
        ),
        "volume": optional_decimal(ticker.volume),
        "exchange_timestamp": ticker.exchange_timestamp,
        "trading_status": ticker.trading_status,
    }

def _source_response_statement(
    *,
    response_id: str,
    experiment_id: str,
    run_id: str,
    request_id: str,
    response_kind: str,
    asset: str,
    source_symbol: str,
    response: RawPublicResponse,
) -> JournalStatement:
    return (
        """
        INSERT INTO source_responses (
            response_id,
            experiment_id,
            run_id,
            request_id,
            response_kind,
            request_url,
            asset,
            source_symbol,
            request_started_utc_ns,
            response_received_utc_ns,
            request_elapsed_ns,
            clock_anomaly,
            source_payload_sha256,
            raw_response_body,
            parse_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            response_id,
            experiment_id,
            run_id,
            request_id,
            response_kind,
            response.url,
            asset,
            source_symbol,
            response.request_started_utc_ns,
            response.response_received_utc_ns,
            response.request_elapsed_ns,
            int(response.clock_anomaly),
            response.sha256,
            response.body,
            "parsed",
        ),
    )


def _raw_candle_records(
    response: RawPublicResponse,
) -> dict[int, Mapping[str, object]]:
    payload = response.json_payload()

    if not isinstance(payload, Mapping):
        raise ValueError("raw history response must be an object")

    raw_result = payload.get("result")
    if not isinstance(raw_result, list):
        raise ValueError("raw history result must be a list")

    by_time: dict[int, Mapping[str, object]] = {}

    for raw_record in raw_result:
        if not isinstance(raw_record, Mapping):
            continue

        raw_time = raw_record.get("time")

        if isinstance(raw_time, bool) or not isinstance(
            raw_time,
            (int, str),
        ):
            continue

        try:
            time_s = int(raw_time)
        except ValueError:
            continue

        by_time[time_s] = raw_record

    return by_time


def _response_id(
    request_id: str,
    response: RawPublicResponse,
) -> str:
    return _stable_id(
        "response",
        request_id,
        response.request_started_utc_ns,
        response.response_received_utc_ns,
        response.sha256,
    )


def _stable_id(*parts: object) -> str:
    return sha256_text(
        ":".join(str(part) for part in parts)
    )
