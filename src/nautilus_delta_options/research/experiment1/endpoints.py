"""Bind exact +30 minute closes after the sealed acquisition deadline."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from nautilus_delta_options.research.experiment1.journal import JournalStatement
from nautilus_delta_options.research.experiment1.manifest import sha256_text


def build_endpoint_statements(
    con: sqlite3.Connection, experiment_id: str, now_ns: int
) -> tuple[JournalStatement, ...]:
    manifest = con.execute(
        "SELECT endpoint_acquisition_deadline_utc_ns FROM experiment_manifest "
        "WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if manifest is None or now_ns < int(manifest[0]):
        raise ValueError("endpoints finalize only at/after sealed acquisition deadline")
    deadline = int(manifest[0])
    rows = con.execute(
        "SELECT b.canonical_event_id,b.slot_id,s.asset,s.candle_close_boundary_ns,"
        "s.endpoint_close_boundary_ns,e.payload_json,e.event_time_utc_ns "
        "FROM population_bindings b JOIN evaluation_slots s ON s.slot_id=b.slot_id "
        "JOIN journal_events e ON e.event_id=b.canonical_event_id "
        "WHERE b.experiment_id=? AND b.binding_kind='canonical' "
        "AND b.canonical_decision IN ('call','put') "
        "AND NOT EXISTS(SELECT 1 FROM signal_endpoints p WHERE p.signal_event_id=e.event_id)",
        (experiment_id,),
    ).fetchall()
    statements: list[JournalStatement] = []
    for event_id, slot_id, asset, boundary, target, raw, returned_ns in rows:
        payload = json.loads(raw)
        p0 = str(payload["p0_observation_id"])
        input_row = con.execute(
            "SELECT c.close_decimal,c.normalized_close_boundary_ns,c.asset,r.clock_anomaly "
            "FROM evaluation_candle_inputs i JOIN candle_observations c "
            "ON c.observation_id=i.observation_id JOIN source_responses r "
            "ON r.response_id=c.response_id WHERE i.experiment_id=? AND i.attempt_id=? "
            "AND c.observation_id=? ORDER BY i.ordinal DESC LIMIT 1",
            (experiment_id, payload["attempt_id"], p0),
        ).fetchone()
        status, reason = "missing", "no_qualifying_exact_endpoint"
        p30: str | None = None
        conflicts = con.execute(
            "SELECT payload_json FROM journal_events WHERE experiment_id=? AND slot_id=? "
            "AND event_type='evaluation_completed'",
            (experiment_id, slot_id),
        ).fetchall()
        if any(json.loads(r[0])["decision"] != payload["decision"] for r in conflicts):
            status, reason = "ambiguous", "conflicting_evaluator_returns"
        elif (
            input_row is None
            or input_row[1] != boundary
            or input_row[2] != asset
            or input_row[3]
            or not _positive(str(input_row[0]))
            or not boundary <= int(payload["captured_ns"]) < boundary + 300 * 10**9
            or not boundary <= returned_ns < boundary + 300 * 10**9
        ):
            status, reason = "invalid", "invalid_or_late_signal_input"
        else:
            candidate = con.execute(
                "SELECT c.observation_id,c.close_decimal FROM candle_observations c "
                "JOIN source_responses r ON r.response_id=c.response_id "
                "WHERE c.experiment_id=? AND c.asset=? AND c.normalized_close_boundary_ns=? "
                "AND c.normalized_open_ns=? AND c.parse_status='parsed' "
                "AND r.clock_anomaly=0 AND c.response_received_utc_ns>=? "
                "AND c.response_received_utc_ns<=? ORDER BY c.rowid LIMIT 1",
                (experiment_id, asset, target, target - 300 * 10**9, target, deadline),
            ).fetchone()
            if candidate is not None:
                if _positive(str(candidate[1])):
                    p30, status, reason = (
                        str(candidate[0]),
                        "complete",
                        "exact_first_archived_close",
                    )
                else:
                    status, reason = "invalid", "nonpositive_or_nonfinite_endpoint"
        statements.append(
            (
                "INSERT INTO signal_endpoints (endpoint_id,experiment_id,signal_event_id,"
                "horizon_seconds,p0_observation_id,target_close_boundary_ns,p30_observation_id,"
                "status,reason,recorded_at_utc_ns) VALUES (?,?,?,1800,?,?,?,?,?,?)",
                (
                    sha256_text(f"{experiment_id}:{event_id}:1800"),
                    experiment_id,
                    event_id,
                    p0,
                    target,
                    p30,
                    status,
                    reason,
                    now_ns,
                ),
            )
        )
    return tuple(statements)


def _positive(value: str) -> bool:
    number = Decimal(value)
    return number.is_finite() and number > 0
