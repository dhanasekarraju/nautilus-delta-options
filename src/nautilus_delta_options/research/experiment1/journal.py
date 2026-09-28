from __future__ import annotations

import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SqlParam = str | int | float | bytes | None


@dataclass(frozen=True, slots=True)
class JournalStats:
    accepted: int
    rejected: int
    written: int
    queued_bytes: int
    fatal_error: str | None


@dataclass(frozen=True, slots=True)
class _WriteEnvelope:
    sql: str
    params: tuple[SqlParam, ...]
    estimated_bytes: int


class ExperimentJournal:
    """Bounded, single-writer SQLite journal for Experiment 1 evidence."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_queue_items: int = 256,
        max_queue_bytes: int = 32 * 1024 * 1024,
        batch_size: int = 64,
        flush_interval_seconds: float = 0.250,
        busy_timeout_ms: int = 250,
        start_immediately: bool = True,
    ) -> None:
        if max_queue_items <= 0:
            raise ValueError("max_queue_items must be positive")
        if max_queue_bytes <= 0:
            raise ValueError("max_queue_bytes must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")

        self._database_path = Path(database_path)
        self._max_queue_bytes = max_queue_bytes
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._busy_timeout_ms = busy_timeout_ms

        self._queue: queue.Queue[_WriteEnvelope | None] = queue.Queue(
            maxsize=max_queue_items
        )

        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None

        self._closed = False
        self._accepted = 0
        self._rejected = 0
        self._written = 0
        self._queued_bytes = 0
        self._fatal_error: str | None = None

        self._prepare_database()

        if start_immediately:
            self.start()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("journal is closed")
            if self._fatal_error is not None:
                raise RuntimeError(
                    f"journal has failed: {self._fatal_error}"
                )
            if self._thread is not None:
                return

            thread = threading.Thread(
                target=self._writer_main,
                name="experiment1-sqlite-writer",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def enqueue(
        self,
        sql: str,
        params: tuple[SqlParam, ...] = (),
    ) -> bool:
        """Attempt a non-blocking journal write admission."""

        if not sql.strip():
            raise ValueError("sql must be non-empty")

        estimated_bytes = _estimate_envelope_bytes(sql, params)
        envelope = _WriteEnvelope(
            sql=sql,
            params=params,
            estimated_bytes=estimated_bytes,
        )

        with self._state_lock:
            if self._closed or self._fatal_error is not None:
                self._rejected += 1
                return False

            if (
                self._queued_bytes + estimated_bytes
                > self._max_queue_bytes
            ):
                self._rejected += 1
                return False

            try:
                self._queue.put_nowait(envelope)
            except queue.Full:
                self._rejected += 1
                return False

            self._queued_bytes += estimated_bytes
            self._accepted += 1
            return True

    def stats(self) -> JournalStats:
        with self._state_lock:
            return JournalStats(
                accepted=self._accepted,
                rejected=self._rejected,
                written=self._written,
                queued_bytes=self._queued_bytes,
                fatal_error=self._fatal_error,
            )

    def close(self, timeout_seconds: float = 10.0) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        with self._state_lock:
            if self._closed:
                thread = self._thread
                fatal_error = self._fatal_error
            else:
                self._closed = True
                thread = self._thread
                fatal_error = self._fatal_error

        if thread is None:
            return fatal_error is None and self._queue.empty()

        if fatal_error is not None:
            thread.join(timeout=timeout_seconds)
            return False

        deadline = time.monotonic() + timeout_seconds

        try:
            self._queue.put(
                None,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except queue.Full:
            return False

        thread.join(timeout=max(0.0, deadline - time.monotonic()))

        if thread.is_alive():
            return False

        with self._state_lock:
            return self._fatal_error is None

    def _prepare_database(self) -> None:
        self._database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        schema_path = Path(__file__).with_name("schema.sql")
        schema = schema_path.read_text(encoding="utf-8")

        required_tables = {
            "experiment_manifest",
            "evaluation_slots",
            "journal_events",
            "source_responses",
            "candle_observations",
            "evaluation_candle_inputs",
            "signal_endpoints",
        }
        required_triggers = {
            f"{table}_{operation}"
            for table in required_tables
            for operation in ("no_update", "no_delete")
        }

        with sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1000,
        ) as con:
            con.execute("PRAGMA foreign_keys = ON")
            con.execute(
                f"PRAGMA busy_timeout = {self._busy_timeout_ms}"
            )
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = FULL")

            existing_tables = {
                str(row[0])
                for row in con.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite_%'
                    """
                )
            }

            if not existing_tables:
                con.executescript(schema)
                return

            missing_tables = required_tables - existing_tables

            existing_triggers = {
                str(row[0])
                for row in con.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'trigger'
                    """
                )
            }
            missing_triggers = required_triggers - existing_triggers

            if missing_tables or missing_triggers:
                problems: list[str] = []

                if missing_tables:
                    problems.append(
                        "missing tables: "
                        + ", ".join(sorted(missing_tables))
                    )

                if missing_triggers:
                    problems.append(
                        "missing triggers: "
                        + ", ".join(sorted(missing_triggers))
                    )

                raise RuntimeError(
                    "existing Experiment 1 journal schema is "
                    "incomplete or incompatible: "
                    + "; ".join(problems)
                )

    def _writer_main(self) -> None:
        con: sqlite3.Connection | None = None

        try:
            con = sqlite3.connect(
                self._database_path,
                timeout=self._busy_timeout_ms / 1000,
            )
            con.execute("PRAGMA foreign_keys = ON")
            con.execute(
                f"PRAGMA busy_timeout = {self._busy_timeout_ms}"
            )
            con.execute("PRAGMA synchronous = FULL")

            stop_after_batch = False

            while not stop_after_batch:
                first = self._queue.get()

                if first is None:
                    self._queue.task_done()
                    break

                self._mark_dequeued(first)

                batch = [first]
                deadline = (
                    time.monotonic()
                    + self._flush_interval_seconds
                )

                while len(batch) < self._batch_size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break

                    try:
                        item = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break

                    if item is None:
                        self._queue.task_done()
                        stop_after_batch = True
                        break

                    self._mark_dequeued(item)
                    batch.append(item)

                try:
                    con.execute("BEGIN IMMEDIATE")

                    for item in batch:
                        con.execute(item.sql, item.params)

                    con.commit()
                except Exception:
                    con.rollback()
                    raise

                with self._state_lock:
                    self._written += len(batch)

                for _item in batch:
                    self._queue.task_done()

        except Exception as exc:
            self._set_fatal(exc)
            self._drain_without_writing()

        finally:
            if con is not None:
                con.close()

    def _mark_dequeued(self, envelope: _WriteEnvelope) -> None:
        with self._state_lock:
            self._queued_bytes -= envelope.estimated_bytes
            if self._queued_bytes < 0:
                self._queued_bytes = 0

    def _set_fatal(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"

        with self._state_lock:
            if self._fatal_error is None:
                self._fatal_error = message

    def _drain_without_writing(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return

            if item is not None:
                self._mark_dequeued(item)

            self._queue.task_done()


def _estimate_envelope_bytes(
    sql: str,
    params: tuple[SqlParam, ...],
) -> int:
    size = len(sql.encode("utf-8"))

    for value in params:
        if value is None:
            size += 1
        elif isinstance(value, bytes):
            size += len(value)
        elif isinstance(value, str):
            size += len(value.encode("utf-8"))
        else:
            size += len(str(value).encode("utf-8"))

    return size
