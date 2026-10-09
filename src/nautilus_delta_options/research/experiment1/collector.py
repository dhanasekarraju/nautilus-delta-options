"""Isolated observational lifecycle. Never imports a paper app/session or places orders."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from threading import Event
from typing import cast

from nautilus_delta_options.research.experiment1.adapter import Experiment1EvaluationAdapter
from nautilus_delta_options.research.experiment1.archive import (
    ArchivedMarketData,
    EvidenceArchive,
)
from nautilus_delta_options.research.experiment1.cycle_events import (
    enqueue_cycle_started,
    enqueue_cycle_terminal,
)
from nautilus_delta_options.research.experiment1.endpoints import build_endpoint_statements
from nautilus_delta_options.research.experiment1.journal import ExperimentJournal, SqlParam
from nautilus_delta_options.research.experiment1.manifest import (
    build_schedule,
    canonical_json,
    manifest_payload,
    manifest_sha256,
    schedule_sha256,
    sha256_text,
)
from nautilus_delta_options.research.experiment1.models import EvaluationSlot, ExperimentManifest
from nautilus_delta_options.research.experiment1.ownership import CollectorLease
from nautilus_delta_options.research.experiment1.population import (
    load_population_bindings,
    seal_population_gaps_through,
)
from nautilus_delta_options.research.experiment1.rolling_state import (
    load_last_committed_observer_state,
)
from nautilus_delta_options.research.experiment1.schema_identity import (
    build_schema_expectation,
    validate_database_identity,
)
from nautilus_delta_options.research.experiment1.service import _stable_id
from nautilus_delta_options.research.experiment1.state_machine import (
    EvaluationAttemptContext,
    EvaluationReturn,
    Experiment1ObserverStateMachine,
)
from nautilus_delta_options.selection.v34_quality import V34QualityConfig
from nautilus_delta_options.signals.v34 import V34Config

NS = 10**9
POLICIES = {
    "population": {
        "window": "close-indexed [start,end)",
        "canonical": "first_actual_return",
        "wait": "retained",
        "conflict": "quarantine",
        "late_seconds": 300,
    },
    "missing": {"interpolate": False, "invent_wait": False, "retry_lost_result": False},
    "endpoint": {
        "seconds": 1800,
        "binding": "first_archived_exact_completed_close",
        "revision": "retain_raw_never_replace",
        "finalize": "acquisition_deadline",
    },
    "clustering": {
        "unit": "UTC_day_both_assets",
        "block_days": 7,
        "bootstrap_replicates": 10000,
        "seed": 3494,
    },
    "failure": {
        "restart": "restore_last_committed_cycle",
        "ambiguous_crash": "refuse_restart",
        "journal_fault": "stop_research_after_cycle",
        "minimum_slot_coverage": 0.95,
        "minimum_endpoint_coverage": 0.95,
    },
}


def runtime_identity() -> dict[str, object]:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Application requires Python 3.12; leave host Python unchanged")
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "dependencies": {p: version(p) for p in ("nautilus-trader", "numpy", "TA-Lib")},
    }


def source_hash(engine_only: bool = False) -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.suffix not in (".py", ".sql"):
            continue
        relative = p.relative_to(root).as_posix()
        if engine_only and relative.startswith("research/"):
            continue
        digest.update(relative.encode() + b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest()


def effective_config() -> dict[str, object]:
    return {
        "signal": asdict(V34Config()),
        "quality": asdict(V34QualityConfig()),
        "poll_seconds": 5,
        "candle_count": 240,
        "request_timeout_seconds": 20,
        "ack_seconds": 2,
        "minimum_free_bytes": 512 * 1024**2,
        "maximum_database_bytes": 8 * 1024**3,
    }


def make_manifest(
    experiment_id: str,
    build_id: str,
    start_ns: int,
    end_ns: int,
    deadline_ns: int,
    protocol: bytes,
    now_ns: int,
) -> ExperimentManifest:
    if not build_id.strip() or not protocol.strip() or now_ns >= start_ns:
        raise ValueError("nonempty build/protocol and a future untouched window are required")
    if end_ns - start_ns > 90 * 86400 * NS:
        raise ValueError("collection window exceeds resource bound of 90 days")
    config = canonical_json(effective_config())
    return ExperimentManifest(
        experiment_id=experiment_id,
        protocol_version="experiment1-30m-v1",
        protocol_sha256=hashlib.sha256(protocol).hexdigest(),
        window_start_utc_ns=start_ns,
        window_end_utc_ns=end_ns,
        endpoint_collection_end_utc_ns=end_ns + 1800 * NS,
        endpoint_acquisition_deadline_utc_ns=deadline_ns,
        signal_engine_source_sha256=source_hash(True),
        collector_build_id=build_id,
        collector_source_sha256=source_hash(),
        effective_config_json=config,
        config_sha256=sha256_text(config),
        runtime_identity_json=canonical_json(runtime_identity()),
        population_rules_json=canonical_json(POLICIES["population"]),
        missing_data_rules_json=canonical_json(POLICIES["missing"]),
        endpoint_rules_json=canonical_json(POLICIES["endpoint"]),
        clustering_rules_json=canonical_json(POLICIES["clustering"]),
        failure_rules_json=canonical_json(POLICIES["failure"]),
        created_at_utc_ns=now_ns,
    )


def validate_manifest(manifest: ExperimentManifest, build_id: str) -> None:
    expected = make_manifest(
        manifest.experiment_id,
        build_id,
        manifest.window_start_utc_ns,
        manifest.window_end_utc_ns,
        manifest.endpoint_acquisition_deadline_utc_ns,
        b"protocol checked separately",
        manifest.created_at_utc_ns,
    )
    for field, value in asdict(expected).items():
        if field == "protocol_sha256":
            continue
        if getattr(manifest, field) != value:
            raise ValueError(f"incompatible manifest: {field}")


def checked_path(root: Path, database: Path) -> Path:
    root = root.resolve(strict=True)
    path = database.absolute()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or root == Path("/"):
        raise ValueError("database must be inside dedicated research root, without aliases")
    path = path.resolve()
    if Path("/data") in path.parents or path == Path("/data"):
        raise ValueError("primary data directory is forbidden")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("database must be an unaliased regular file")
    return path


def schedule_for(manifest: ExperimentManifest) -> tuple[EvaluationSlot, ...]:
    return build_schedule(
        experiment_id=manifest.experiment_id,
        window_start_utc_ns=manifest.window_start_utc_ns,
        window_end_utc_ns=manifest.window_end_utc_ns,
    )


def initialize(path: Path, manifest: ExperimentManifest, now_ns: int) -> None:
    if now_ns >= manifest.window_start_utc_ns:
        raise ValueError("database must be initialized before the sealed window starts")
    slots = schedule_for(manifest)
    schedule_hash = schedule_sha256(slots)
    payload = manifest_payload(manifest, schedule_hash=schedule_hash)
    payload["manifest_sha256"] = manifest_sha256(manifest, schedule_hash=schedule_hash)
    journal = ExperimentJournal(path, create_new=True)
    try:
        columns = tuple(payload)
        statements = [
            (
                f"INSERT INTO experiment_manifest ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(cast(SqlParam, payload[c]) for c in columns),
            )
        ]
        for slot in slots:
            row = asdict(slot)
            statements.append(
                (
                    f"INSERT INTO evaluation_slots ({','.join(row)}) "
                    f"VALUES ({','.join('?' for _ in row)})",
                    tuple(cast(SqlParam, value) for value in row.values()),
                )
            )
        if not journal.enqueue_batch(statements) or not journal.flush():
            raise RuntimeError("manifest/schedule initialization failed; do not repair or reuse")
    finally:
        journal.close()


def verify_database(path: Path, manifest: ExperimentManifest) -> None:
    slots = schedule_for(manifest)
    expected = manifest_payload(manifest, schedule_hash=schedule_sha256(slots))
    expected["manifest_sha256"] = manifest_sha256(manifest, schedule_hash=schedule_sha256(slots))
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
        validate_database_identity(
            con, build_schema_expectation(Path(__file__).with_name("schema.sql"))
        )
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM experiment_manifest").fetchall()
        if len(rows) != 1 or dict(rows[0]) != expected:
            raise RuntimeError("durable manifest mismatch")
        actual = con.execute(
            "SELECT * FROM evaluation_slots ORDER BY candle_close_boundary_ns,asset"
        ).fetchall()
        if [dict(row) for row in actual] != [asdict(slot) for slot in slots]:
            raise RuntimeError("durable slot schedule mismatch")


def _marker(path: Path, payload: dict[str, object]) -> None:
    with path.open("x") as handle:
        handle.write(canonical_json(payload))
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_collection(path: Path, manifest: ExperimentManifest, stop: Event) -> None:
    """Explicitly authorized public collection only. No startup from dashboard code."""
    dirty = path.with_name(path.name + ".inflight")
    with CollectorLease.acquire(path) as lease:
        if dirty.exists():
            raise RuntimeError(
                "ambiguous prior collection cycle; preserve evidence, refuse restart"
            )
        verify_database(path, manifest)
        recovered = load_last_committed_observer_state(path, experiment_id=manifest.experiment_id)
        population = load_population_bindings(path, experiment_id=manifest.experiment_id)
        slots = schedule_for(manifest)
        run_id = uuid.uuid4().hex
        journal = ExperimentJournal(path)
        archive = EvidenceArchive(
            journal,
            manifest.experiment_id,
            run_id,
            {s.endpoint_close_boundary_ns for s in slots},
            manifest.endpoint_acquisition_deadline_utc_ns,
        )
        market = ArchivedMarketData(
            archive,
            stop=stop,
            utc_ns=time.time_ns,
            start_ns=manifest.window_start_utc_ns,
            end_ns=manifest.window_end_utc_ns,
        )
        adapter = Experiment1EvaluationAdapter(
            lease=lease,
            experiment_id=manifest.experiment_id,
            run_id=run_id,
            slots=slots,
            utc_ns=time.time_ns,
            journal=journal,
            market_data=market,
            config=V34Config(),
        )
        refs: list[str] = []

        def start(context: EvaluationAttemptContext) -> str:
            # Drain the asynchronous writer before the bounded durable-intent transaction.
            # This avoids contention between the two deliberate write paths.
            if not journal.flush(2):
                raise RuntimeError("pre-evaluation evidence acknowledgement failed")
            before = time.monotonic_ns()
            attempt = adapter.on_attempt_start(context)
            archive.record(
                "collection_diagnostic",
                {
                    "kind": "intent_latency",
                    "attempt_id": attempt,
                    "elapsed_ns": time.monotonic_ns() - before,
                },
            )
            return attempt

        def returned(result: EvaluationReturn) -> bool:
            accepted = adapter.on_evaluation_return(result)
            if accepted:
                slot_id = adapter.slot_for_context(
                    EvaluationAttemptContext(
                        underlying=result.underlying,
                        candles=result.candles,
                        source_option_chain=result.source_option_chain,
                        evaluated_option_chain=result.evaluated_option_chain,
                        previous_chain=result.previous_chain,
                        as_of=result.as_of,
                        captured_ns=result.captured_ns,
                    )
                ).slot_id
                refs.append(
                    _stable_id(
                        "event",
                        manifest.experiment_id,
                        run_id,
                        slot_id,
                        result.attempt_id,
                        "evaluation_completed",
                    )
                )
            return accepted

        observer = Experiment1ObserverStateMachine(
            history_client=market,
            delta_client=market,
            clock_ns=time.time_ns,
            on_evaluation_attempt_start=start,
            on_evaluation_attempt_failure=adapter.on_attempt_failure,
            on_evaluation_return=returned,
            population_bindings=population,
            recovered_rolling_state=recovered,
        )
        archive.record(
            "run_started",
            {
                "run_id": run_id,
                "build_id": manifest.collector_build_id,
                "manifest_sha256": manifest_sha256(manifest, schedule_hash=schedule_sha256(slots)),
            },
        )
        previous_wall, previous_mono = time.time_ns(), time.monotonic_ns()
        clean = False
        try:
            while (
                not stop.is_set() and time.time_ns() < manifest.endpoint_acquisition_deadline_utc_ns
            ):
                now, mono = time.time_ns(), time.monotonic_ns()
                if abs((now - previous_wall) - (mono - previous_mono)) > 5 * NS:
                    raise RuntimeError("wall-clock discontinuity; collection halted")
                previous_wall, previous_mono = now, mono
                if now < manifest.window_start_utc_ns:
                    stop.wait(min(5, (manifest.window_start_utc_ns - now) / NS))
                    continue
                if (
                    shutil.disk_usage(path.parent).free < 512 * 1024**2
                    or path.stat().st_size > 8 * 1024**3
                ):
                    raise RuntimeError("research storage resource limit reached")
                cycle_id = uuid.uuid4().hex
                _marker(dirty, {"run_id": run_id, "cycle_id": cycle_id})
                refs.clear()
                if now < manifest.window_end_utc_ns:
                    enqueue_cycle_started(
                        journal,
                        experiment_id=manifest.experiment_id,
                        run_id=run_id,
                        cycle_id=cycle_id,
                        event_time_utc_ns=now,
                    )
                    cycle = observer.run_cycle()
                    if cycle.evidence_faults:
                        raise RuntimeError("; ".join(cycle.evidence_faults))
                    enqueue_cycle_terminal(
                        journal,
                        experiment_id=manifest.experiment_id,
                        run_id=run_id,
                        cycle_id=cycle_id,
                        status="completed" if cycle.evaluated else "aborted",
                        candle_close_boundary_ns=None
                        if cycle.candle_close_ms is None
                        else (cycle.candle_close_ms + 1) * 10**6,
                        evaluation_event_ids=tuple(refs),
                        event_time_utc_ns=time.time_ns(),
                        failure_stage=None if cycle.evaluated else cycle.failure_stage or "idle",
                        detail="; ".join(cycle.warnings),
                    )
                else:
                    market.tail = True
                    for asset in ("BTC", "ETH"):
                        try:
                            market.fetch_5m_candles(asset)
                        except Exception as exc:
                            archive.record(
                                "collection_fault",
                                {"phase": "endpoint_tail", "asset": asset, "error": str(exc)},
                            )
                if archive.fault or not journal.flush(2) or journal.stats().rejected:
                    raise RuntimeError(archive.fault or "journal durability failed")
                dirty.unlink()
                _sync_directory(path.parent)
                stop.wait(5)
            if not stop.is_set():
                seal_population_gaps_through(
                    path,
                    experiment_id=manifest.experiment_id,
                    run_id=run_id,
                    cutoff_boundary_ns=manifest.window_end_utc_ns - 1,
                    recorded_at_utc_ns=time.time_ns(),
                )
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
                    statements = build_endpoint_statements(
                        con, manifest.experiment_id, time.time_ns()
                    )
                if statements and not journal.enqueue_batch(statements):
                    raise RuntimeError("endpoint finalization rejected")
            archive.record(
                "run_stopped", {"reason": "shutdown" if stop.is_set() else "window_complete"}
            )
            clean = journal.flush(2) and archive.fault is None
        finally:
            closed = journal.close(10)
            if (not clean or not closed) and not dirty.exists():
                _marker(dirty, {"run_id": run_id, "reason": "unclean_exit"})
            if (not closed or not clean) and sys.exc_info()[0] is None:
                raise RuntimeError("collector stopped without complete durable evidence")
