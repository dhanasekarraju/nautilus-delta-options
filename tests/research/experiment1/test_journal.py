from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
)


def _create_probe_table(database_path: Path) -> None:
    with sqlite3.connect(database_path) as con:
        con.execute(
            """
            CREATE TABLE writer_probe (
                sequence INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def test_writer_drains_bounded_queue_in_fifo_order(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        max_queue_items=2,
        batch_size=2,
        flush_interval_seconds=0.01,
        start_immediately=False,
    )

    _create_probe_table(database_path)

    sql = """
        INSERT INTO writer_probe (sequence, value)
        VALUES (?, ?)
    """

    assert journal.enqueue(sql, (1, "first"))
    assert journal.enqueue(sql, (2, "second"))

    # The writer has not started, so capacity is deterministically full.
    assert not journal.enqueue(sql, (3, "rejected"))

    before = journal.stats()
    assert before.accepted == 2
    assert before.rejected == 1
    assert before.written == 0
    assert before.queued_bytes > 0
    assert before.fatal_error is None

    journal.start()

    assert journal.close(timeout_seconds=2.0)

    after = journal.stats()
    assert after.accepted == 2
    assert after.rejected == 1
    assert after.written == 2
    assert after.queued_bytes == 0
    assert after.fatal_error is None

    with sqlite3.connect(database_path) as con:
        rows = con.execute(
            """
            SELECT sequence, value
            FROM writer_probe
            ORDER BY sequence
            """
        ).fetchall()

    assert rows == [
        (1, "first"),
        (2, "second"),
    ]


def test_writer_rejects_byte_capacity_without_blocking(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        max_queue_bytes=8,
        start_immediately=False,
    )

    assert not journal.enqueue(
        "INSERT INTO impossible_table VALUES (?)",
        ("payload-too-large",),
    )

    stats = journal.stats()
    assert stats.accepted == 0
    assert stats.rejected == 1
    assert stats.written == 0
    assert stats.queued_bytes == 0
    assert stats.fatal_error is None


def test_writer_failure_is_contained_and_reported(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        batch_size=1,
        flush_interval_seconds=0.01,
        start_immediately=False,
    )

    assert journal.enqueue(
        "INSERT INTO table_that_does_not_exist VALUES (?)",
        ("x",),
    )

    journal.start()

    assert not journal.close(timeout_seconds=2.0)

    stats = journal.stats()
    assert stats.accepted == 1
    assert stats.written == 0
    assert stats.queued_bytes == 0
    assert stats.fatal_error is not None
    assert "OperationalError" in stats.fatal_error

    # Once the research journal has failed, later evidence is rejected
    # rather than affecting any trading path.
    assert not journal.enqueue("SELECT 1")


def test_existing_journal_database_can_be_reopened(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    first = ExperimentJournal(
        database_path,
        start_immediately=False,
    )
    assert first.close(timeout_seconds=2.0)

    second = ExperimentJournal(
        database_path,
        start_immediately=False,
    )
    assert second.close(timeout_seconds=2.0)


def test_incomplete_existing_database_is_rejected_without_repair(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    with sqlite3.connect(database_path) as con:
        con.execute(
            """
            CREATE TABLE experiment_manifest (
                experiment_id TEXT PRIMARY KEY
            )
            """
        )

    with pytest.raises(
        RuntimeError,
        match="incomplete or incompatible",
    ) as exc_info:
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    message = str(exc_info.value)
    assert "missing tables:" in message
    assert "evaluation_slots" in message
    assert "journal_events" in message
    assert "source_responses" in message

    # Initialization must not silently repair or extend the partial DB.
    with sqlite3.connect(database_path) as con:
        tables = {
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
        triggers = {
            str(row[0])
            for row in con.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'trigger'
                """
            )
        }

    assert tables == {"experiment_manifest"}
    assert triggers == set()


def test_batch_is_admitted_as_one_queue_envelope_and_committed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        max_queue_items=1,
        batch_size=1,
        flush_interval_seconds=0.01,
        start_immediately=False,
    )

    _create_probe_table(database_path)

    sql = """
        INSERT INTO writer_probe (sequence, value)
        VALUES (?, ?)
    """

    assert journal.enqueue_batch(
        (
            (sql, (1, "first")),
            (sql, (2, "second")),
        )
    )

    # One multi-statement bundle occupies one queue envelope.
    assert not journal.enqueue(
        sql,
        (3, "rejected"),
    )

    before = journal.stats()
    assert before.accepted == 1
    assert before.rejected == 1
    assert before.written == 0
    assert before.queued_bytes > 0

    journal.start()

    assert journal.close(timeout_seconds=2.0)

    after = journal.stats()
    assert after.accepted == 1
    assert after.rejected == 1
    assert after.written == 1
    assert after.queued_bytes == 0
    assert after.fatal_error is None

    with sqlite3.connect(database_path) as con:
        rows = con.execute(
            """
            SELECT sequence, value
            FROM writer_probe
            ORDER BY sequence
            """
        ).fetchall()

    assert rows == [
        (1, "first"),
        (2, "second"),
    ]


def test_batch_rolls_back_all_statements_on_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        batch_size=1,
        flush_interval_seconds=0.01,
        start_immediately=False,
    )

    _create_probe_table(database_path)

    assert journal.enqueue_batch(
        (
            (
                """
                INSERT INTO writer_probe (sequence, value)
                VALUES (?, ?)
                """,
                (1, "must-roll-back"),
            ),
            (
                "INSERT INTO table_that_does_not_exist VALUES (?)",
                ("boom",),
            ),
        )
    )

    journal.start()

    assert not journal.close(timeout_seconds=2.0)

    stats = journal.stats()
    assert stats.accepted == 1
    assert stats.written == 0
    assert stats.queued_bytes == 0
    assert stats.fatal_error is not None
    assert "OperationalError" in stats.fatal_error

    with sqlite3.connect(database_path) as con:
        rows = con.execute(
            """
            SELECT sequence, value
            FROM writer_probe
            """
        ).fetchall()

    assert rows == []


def test_batch_byte_capacity_is_checked_for_whole_envelope(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        max_queue_bytes=15,
        start_immediately=False,
    )

    # Each statement is 11 estimated bytes:
    # len("SELECT ?") == 8 plus len("abc") == 3.
    # Either alone fits, while the two-statement envelope does not.
    assert not journal.enqueue_batch(
        (
            ("SELECT ?", ("abc",)),
            ("SELECT ?", ("def",)),
        )
    )

    stats = journal.stats()
    assert stats.accepted == 0
    assert stats.rejected == 1
    assert stats.written == 0
    assert stats.queued_bytes == 0


def test_batch_rejects_invalid_shape_before_queue_admission(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        start_immediately=False,
    )

    with pytest.raises(
        ValueError,
        match="at least one statement",
    ):
        journal.enqueue_batch(())

    with pytest.raises(ValueError, match="sql must be non-empty"):
        journal.enqueue_batch(
            (
                ("   ", ()),
            )
        )

    stats = journal.stats()
    assert stats.accepted == 0
    assert stats.rejected == 0
    assert stats.written == 0
    assert stats.queued_bytes == 0
