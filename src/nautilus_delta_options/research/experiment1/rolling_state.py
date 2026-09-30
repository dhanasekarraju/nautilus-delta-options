from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nautilus_delta_options.delta.public_client import (
    DeltaUnderlying,
)
from nautilus_delta_options.research.experiment1.manifest import (
    sha256_text,
)
from nautilus_delta_options.research.experiment1.models import (
    JournalEventType,
)
from nautilus_delta_options.research.experiment1.schema_identity import (
    build_schema_expectation,
    validate_database_identity,
)
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34ContractState,
)

_RESOLUTION_NS = 300_000_000_000
_NS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class RecoveredObserverState:
    """Last durably committed frozen observer rolling state."""

    cycle_event_id: str
    candle_close_boundary_ns: int
    chains: tuple[V34ChainState, ...]

    def __post_init__(self) -> None:
        if not self.cycle_event_id:
            raise ValueError(
                "cycle_event_id must be non-empty"
            )

        if self.candle_close_boundary_ns < 0:
            raise ValueError(
                "candle boundary cannot be negative"
            )

        if (
            self.candle_close_boundary_ns
            % _RESOLUTION_NS
            != 0
        ):
            raise ValueError(
                "recovered candle boundary must align to five minutes"
            )

        if not self.chains:
            raise ValueError(
                "recovered rolling state must contain chains"
            )

        underlyings = tuple(
            chain.underlying
            for chain in self.chains
        )

        if len(underlyings) != len(set(underlyings)):
            raise ValueError(
                "recovered rolling state contains duplicate underlyings"
            )

        if any(
            chain.captured_ns <= 0
            for chain in self.chains
        ):
            raise ValueError(
                "recovered chain captured_ns must be positive"
            )

    @property
    def candle_close_ms(self) -> int:
        return (
            self.candle_close_boundary_ns
            // _NS_PER_MS
            - 1
        )


def load_last_committed_observer_state(
    database_path: str | Path,
    *,
    experiment_id: str,
    expected_underlyings: Sequence[DeltaUnderlying] = (
        "BTC",
        "ETH",
    ),
    busy_timeout_ms: int = 250,
) -> RecoveredObserverState | None:
    """Recover rolling state from the latest durable completed cycle.

    Only evaluation_completed events explicitly referenced by that
    completed observer cycle are eligible. Evaluation results belonging
    to aborted cycles can never become frozen rolling state.
    """

    if not experiment_id:
        raise ValueError(
            "experiment_id must be non-empty"
        )

    if busy_timeout_ms < 0:
        raise ValueError(
            "busy_timeout_ms cannot be negative"
        )

    expected = tuple(expected_underlyings)

    if not expected:
        raise ValueError(
            "expected_underlyings must be non-empty"
        )

    if len(expected) != len(set(expected)):
        raise ValueError(
            "expected_underlyings must be unique"
        )

    path = Path(database_path)

    if not path.exists():
        raise FileNotFoundError(
            "Experiment 1 journal database does not exist"
        )

    schema_path = Path(__file__).with_name(
        "schema.sql"
    )

    expectation = build_schema_expectation(
        schema_path
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

        validate_database_identity(
            con,
            expectation,
        )

        cycle_row = con.execute(
            """
            SELECT
                event_id,
                run_id,
                payload_json,
                payload_sha256
            FROM journal_events
            WHERE experiment_id = ?
              AND event_type = 'observer_cycle_completed'
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()

        if cycle_row is None:
            _assert_no_post_commit_ambiguity(
                con,
                experiment_id=experiment_id,
                committed_boundary_ns=None,
            )
            return None

        cycle_event_id = str(cycle_row[0])
        cycle_run_id = str(cycle_row[1])
        cycle_payload_json = str(cycle_row[2])

        _verify_payload_hash(
            cycle_payload_json,
            str(cycle_row[3]),
            label="observer cycle",
        )

        cycle_payload = _json_object(
            cycle_payload_json,
            label="observer cycle",
        )

        if cycle_payload.get("status") != "completed":
            raise RuntimeError(
                "completed cycle event has invalid status"
            )

        if (
            cycle_payload.get("state_committed")
            is not True
        ):
            raise RuntimeError(
                "completed cycle event does not mark state committed"
            )

        boundary_ns = _required_int(
            cycle_payload,
            "candle_close_boundary_ns",
            label="observer cycle",
        )

        if (
            boundary_ns < 0
            or boundary_ns % _RESOLUTION_NS != 0
        ):
            raise RuntimeError(
                "completed cycle boundary is invalid"
            )

        raw_event_ids = cycle_payload.get(
            "evaluation_event_ids"
        )

        if not isinstance(raw_event_ids, list):
            raise RuntimeError(
                "completed cycle evaluation_event_ids must be a list"
            )

        evaluation_event_ids: list[str] = []

        for value in raw_event_ids:
            if not isinstance(value, str) or not value:
                raise RuntimeError(
                    "completed cycle contains invalid evaluation event id"
                )

            evaluation_event_ids.append(value)

        if (
            len(evaluation_event_ids)
            != len(set(evaluation_event_ids))
        ):
            raise RuntimeError(
                "completed cycle contains duplicate evaluation event ids"
            )

        if len(evaluation_event_ids) != len(expected):
            raise RuntimeError(
                "completed cycle does not contain exactly one "
                "evaluation per configured underlying"
            )

        chains_by_underlying: dict[
            DeltaUnderlying,
            V34ChainState,
        ] = {}

        for event_id in evaluation_event_ids:
            row = con.execute(
                """
                SELECT
                    event.run_id,
                    event.event_type,
                    event.payload_json,
                    event.payload_sha256,
                    slot.asset,
                    slot.candle_close_boundary_ns
                FROM journal_events AS event
                LEFT JOIN evaluation_slots AS slot
                  ON slot.slot_id = event.slot_id
                WHERE event.experiment_id = ?
                  AND event.event_id = ?
                """,
                (
                    experiment_id,
                    event_id,
                ),
            ).fetchone()

            if row is None:
                raise RuntimeError(
                    "completed cycle references missing evaluation event"
                )

            if str(row[0]) != cycle_run_id:
                raise RuntimeError(
                    "completed cycle references evaluation "
                    "from a different run"
                )

            if (
                str(row[1])
                != JournalEventType.EVALUATION_COMPLETED.value
            ):
                raise RuntimeError(
                    "completed cycle reference is not "
                    "evaluation_completed"
                )

            if row[4] is None or row[5] is None:
                raise RuntimeError(
                    "completed evaluation has no scheduled slot"
                )

            slot_asset = str(row[4])
            slot_boundary_ns = int(row[5])

            if slot_boundary_ns != boundary_ns:
                raise RuntimeError(
                    "completed evaluation slot boundary differs "
                    "from committed cycle"
                )

            if slot_asset not in expected:
                raise RuntimeError(
                    "completed evaluation slot asset is not configured"
                )

            evaluation_payload_json = str(row[2])

            _verify_payload_hash(
                evaluation_payload_json,
                str(row[3]),
                label="evaluation",
            )

            evaluation_payload = _json_object(
                evaluation_payload_json,
                label="evaluation",
            )

            if (
                evaluation_payload.get("asset")
                != slot_asset
            ):
                raise RuntimeError(
                    "evaluation payload asset differs from slot"
                )

            payload_boundary_ns = _required_int(
                evaluation_payload,
                "candle_close_boundary_ns",
                label="evaluation",
            )

            if payload_boundary_ns != boundary_ns:
                raise RuntimeError(
                    "evaluation payload boundary differs "
                    "from committed cycle"
                )

            underlying = slot_asset

            if underlying in chains_by_underlying:
                raise RuntimeError(
                    "completed cycle contains duplicate underlying state"
                )

            chains_by_underlying[underlying] = (
                _parse_chain_state(
                    evaluation_payload.get(
                        "current_chain_state"
                    ),
                    expected_underlying=underlying,
                )
            )

        _assert_no_post_commit_ambiguity(
            con,
            experiment_id=experiment_id,
            committed_boundary_ns=boundary_ns,
        )

    if set(chains_by_underlying) != set(expected):
        raise RuntimeError(
            "completed cycle is missing configured underlying state"
        )

    return RecoveredObserverState(
        cycle_event_id=cycle_event_id,
        candle_close_boundary_ns=boundary_ns,
        chains=tuple(
            chains_by_underlying[underlying]
            for underlying in expected
        ),
    )


def _assert_no_post_commit_ambiguity(
    con: sqlite3.Connection,
    *,
    experiment_id: str,
    committed_boundary_ns: int | None,
) -> None:
    """Fail closed when a crashed process may have advanced RAM state."""

    if committed_boundary_ns is None:
        boundary_predicate = ""
        boundary_params: tuple[object, ...] = (
            experiment_id,
        )
    else:
        boundary_predicate = (
            " AND slot.candle_close_boundary_ns > ?"
        )
        boundary_params = (
            experiment_id,
            committed_boundary_ns,
        )

    unresolved_rows = con.execute(
        f"""
        SELECT
            started.attempt_id,
            slot.asset,
            slot.candle_close_boundary_ns
        FROM journal_events AS started
        JOIN evaluation_slots AS slot
          ON slot.slot_id = started.slot_id
        WHERE started.experiment_id = ?
          AND started.event_type = 'evaluation_started'
          {boundary_predicate}
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
        boundary_params,
    ).fetchall()

    if unresolved_rows:
        attempt_id = str(unresolved_rows[0][0])
        asset = str(unresolved_rows[0][1])
        boundary_ns = int(unresolved_rows[0][2])

        raise RuntimeError(
            "rolling-state recovery is ambiguous: "
            "unresolved evaluation_started exists after "
            "the last durable committed cycle "
            f"(attempt_id={attempt_id}, "
            f"asset={asset}, "
            f"boundary_ns={boundary_ns})"
        )

    aborted_refs = _load_aborted_evaluation_refs(
        con,
        experiment_id=experiment_id,
    )

    if committed_boundary_ns is None:
        completed_predicate = ""
        completed_params: tuple[object, ...] = (
            experiment_id,
        )
    else:
        completed_predicate = (
            " AND slot.candle_close_boundary_ns > ?"
        )
        completed_params = (
            experiment_id,
            committed_boundary_ns,
        )

    completed_rows = con.execute(
        f"""
        SELECT
            event.event_id,
            event.run_id,
            slot.asset,
            slot.candle_close_boundary_ns
        FROM journal_events AS event
        JOIN evaluation_slots AS slot
          ON slot.slot_id = event.slot_id
        WHERE event.experiment_id = ?
          AND event.event_type = 'evaluation_completed'
          {completed_predicate}
        ORDER BY
            slot.candle_close_boundary_ns,
            slot.asset,
            event.sequence
        """,
        completed_params,
    ).fetchall()

    for row in completed_rows:
        event_id = str(row[0])
        run_id = str(row[1])
        asset = str(row[2])
        boundary_ns = int(row[3])

        terminal = aborted_refs.get(event_id)

        if terminal is None:
            raise RuntimeError(
                "rolling-state recovery is ambiguous: "
                "evaluation_completed exists after the last "
                "durable committed cycle without a durable "
                "observer_cycle_aborted terminal "
                f"(event_id={event_id}, "
                f"asset={asset}, "
                f"boundary_ns={boundary_ns})"
            )

        terminal_run_id, terminal_boundary_ns = terminal

        if terminal_run_id != run_id:
            raise RuntimeError(
                "aborted cycle references evaluation from "
                "a different run"
            )

        if terminal_boundary_ns != boundary_ns:
            raise RuntimeError(
                "aborted cycle boundary differs from "
                "referenced evaluation"
            )


def _load_aborted_evaluation_refs(
    con: sqlite3.Connection,
    *,
    experiment_id: str,
) -> dict[str, tuple[str, int]]:
    rows = con.execute(
        """
        SELECT
            run_id,
            payload_json,
            payload_sha256
        FROM journal_events
        WHERE experiment_id = ?
          AND event_type = 'observer_cycle_aborted'
        ORDER BY sequence
        """,
        (experiment_id,),
    ).fetchall()

    refs: dict[str, tuple[str, int]] = {}

    for row in rows:
        run_id = str(row[0])
        payload_json = str(row[1])

        _verify_payload_hash(
            payload_json,
            str(row[2]),
            label="aborted observer cycle",
        )

        payload = _json_object(
            payload_json,
            label="aborted observer cycle",
        )

        if payload.get("status") != "aborted":
            raise RuntimeError(
                "aborted cycle event has invalid status"
            )

        if payload.get("state_committed") is not False:
            raise RuntimeError(
                "aborted cycle event incorrectly marks state committed"
            )

        boundary_value = payload.get(
            "candle_close_boundary_ns"
        )

        if boundary_value is None:
            # A pre-evaluation failure can legitimately have no
            # resolved candle boundary and therefore no returned
            # evaluation event IDs.
            boundary_ns: int | None = None
        elif (
            isinstance(boundary_value, bool)
            or not isinstance(boundary_value, int)
        ):
            raise RuntimeError(
                "aborted cycle candle boundary must be an integer or null"
            )
        else:
            boundary_ns = boundary_value

        raw_event_ids = payload.get(
            "evaluation_event_ids"
        )

        if not isinstance(raw_event_ids, list):
            raise RuntimeError(
                "aborted cycle evaluation_event_ids must be a list"
            )

        if raw_event_ids and boundary_ns is None:
            raise RuntimeError(
                "aborted cycle with evaluator returns "
                "must have a candle boundary"
            )

        for value in raw_event_ids:
            if not isinstance(value, str) or not value:
                raise RuntimeError(
                    "aborted cycle contains invalid evaluation event id"
                )

            if value in refs:
                raise RuntimeError(
                    "evaluation event is referenced by multiple "
                    "aborted cycles"
                )

            assert boundary_ns is not None

            refs[value] = (
                run_id,
                boundary_ns,
            )

    return refs


def _verify_payload_hash(
    payload_json: str,
    payload_sha256: str,
    *,
    label: str,
) -> None:
    if sha256_text(payload_json) != payload_sha256:
        raise RuntimeError(
            f"{label} payload hash mismatch"
        )


def _json_object(
    payload_json: str,
    *,
    label: str,
) -> Mapping[str, object]:
    try:
        value = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{label} payload is not valid JSON"
        ) from error

    if not isinstance(value, dict):
        raise RuntimeError(
            f"{label} payload must be an object"
        )

    return cast(
        Mapping[str, object],
        value,
    )


def _required_int(
    payload: Mapping[str, object],
    field: str,
    *,
    label: str,
) -> int:
    value = payload.get(field)

    if isinstance(value, bool) or not isinstance(
        value,
        int,
    ):
        raise RuntimeError(
            f"{label} {field} must be an integer"
        )

    return value


def _required_float(
    payload: Mapping[str, object],
    field: str,
) -> float:
    value = payload.get(field)

    if isinstance(value, bool) or not isinstance(
        value,
        (int, float),
    ):
        raise RuntimeError(
            f"chain contract {field} must be numeric"
        )

    result = float(value)

    if not math.isfinite(result):
        raise RuntimeError(
            f"chain contract {field} must be finite"
        )

    return result


def _parse_chain_state(
    value: object,
    *,
    expected_underlying: DeltaUnderlying,
) -> V34ChainState:
    if not isinstance(value, dict):
        raise RuntimeError(
            "evaluation current_chain_state must be an object"
        )

    payload = cast(
        Mapping[str, object],
        value,
    )

    if (
        payload.get("underlying")
        != expected_underlying
    ):
        raise RuntimeError(
            "recovered chain underlying differs from slot"
        )

    captured_ns = _required_int(
        payload,
        "captured_ns",
        label="chain state",
    )

    if captured_ns <= 0:
        raise RuntimeError(
            "recovered chain captured_ns must be positive"
        )

    raw_contracts = payload.get("contracts")

    if not isinstance(raw_contracts, list):
        raise RuntimeError(
            "recovered chain contracts must be a list"
        )

    if not raw_contracts:
        raise RuntimeError(
            "recovered chain contracts must be non-empty"
        )

    contracts: list[V34ContractState] = []

    for raw_contract in raw_contracts:
        if not isinstance(raw_contract, dict):
            raise RuntimeError(
                "recovered chain contract must be an object"
            )

        contract = cast(
            Mapping[str, object],
            raw_contract,
        )

        symbol = contract.get("symbol")

        if not isinstance(symbol, str) or not symbol:
            raise RuntimeError(
                "recovered chain contract symbol is invalid"
            )

        contract_type_raw = contract.get(
            "contract_type"
        )

        if contract_type_raw not in (
            "call_options",
            "put_options",
        ):
            raise RuntimeError(
                "recovered chain contract_type is invalid"
            )

        contracts.append(
            V34ContractState(
                symbol=symbol,
                contract_type=contract_type_raw,
                iv=_required_float(
                    contract,
                    "iv",
                ),
                mark_price=_required_float(
                    contract,
                    "mark_price",
                ),
                open_interest=_required_float(
                    contract,
                    "open_interest",
                ),
                volume=_required_float(
                    contract,
                    "volume",
                ),
                bid_size=_required_float(
                    contract,
                    "bid_size",
                ),
                ask_size=_required_float(
                    contract,
                    "ask_size",
                ),
            )
        )

    return V34ChainState(
        underlying=expected_underlying,
        captured_ns=captured_ns,
        contracts=tuple(contracts),
    )
