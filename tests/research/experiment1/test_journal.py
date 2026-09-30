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
        create_new=True,
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
        create_new=True,
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
        create_new=True,
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
        create_new=True,
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
    assert (
        "existing Experiment 1 journal schema is "
        "incomplete or incompatible"
    ) in message
    assert "SQLite application_id does not match" in message

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
        create_new=True,
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
        create_new=True,
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
        create_new=True,
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
        create_new=True,
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


def test_missing_database_requires_explicit_creation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    assert not database_path.exists()

    with pytest.raises(
        FileNotFoundError,
        match="creation requires create_new=True",
    ):
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    # Validation/opening must not create the missing path.
    assert not database_path.exists()


def test_existing_empty_database_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"
    database_path.touch()

    before = database_path.read_bytes()

    with pytest.raises(
        RuntimeError,
        match="database is empty",
    ):
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    assert before == b""
    assert database_path.read_bytes() == before


def test_create_new_refuses_existing_database_without_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    first = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )
    assert first.close(timeout_seconds=2.0)

    before = database_path.read_bytes()

    with pytest.raises(
        RuntimeError,
        match="database path already exists",
    ):
        ExperimentJournal(
            database_path,
            create_new=True,
            start_immediately=False,
        )

    assert database_path.read_bytes() == before


def test_foreign_database_is_rejected_read_only_without_wal_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "foreign.sqlite"

    with sqlite3.connect(database_path) as con:
        mode = con.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        assert mode is not None
        assert str(mode[0]).lower() == "delete"

        con.execute(
            """
            CREATE TABLE foreign_table (
                value TEXT NOT NULL
            )
            """
        )

    before = database_path.read_bytes()
    wal_path = Path(str(database_path) + "-wal")

    assert not wal_path.exists()

    with pytest.raises(
        RuntimeError,
        match="SQLite application_id does not match",
    ):
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    # Rejection happens during the read-only preflight. It must not
    # mutate the database or switch it into WAL mode.
    assert database_path.read_bytes() == before
    assert not wal_path.exists()

    with sqlite3.connect(
        database_path,
    ) as con:
        mode = con.execute(
            "PRAGMA journal_mode"
        ).fetchone()

    assert mode is not None
    assert str(mode[0]).lower() == "delete"


def test_same_named_schema_with_extra_column_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(database_path) as con:
        mode = con.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        assert mode is not None
        assert str(mode[0]).lower() == "delete"

        con.execute(
            """
            ALTER TABLE source_responses
            ADD COLUMN injected_column TEXT
            """
        )

    before = database_path.read_bytes()
    wal_path = Path(str(database_path) + "-wal")

    assert not wal_path.exists()

    with pytest.raises(
        RuntimeError,
        match="actual SQLite schema descriptor does not match",
    ):
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    assert database_path.read_bytes() == before
    assert not wal_path.exists()

    # All original table names still exist. The descriptor, rather
    # than a table-name presence check, detected the change.
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

    assert "source_responses" in tables
    assert "journal_schema_identity" in tables


def test_altered_append_only_trigger_body_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(database_path) as con:
        mode = con.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        assert mode is not None
        assert str(mode[0]).lower() == "delete"

        con.execute(
            "DROP TRIGGER source_responses_no_update"
        )
        con.execute(
            """
            CREATE TRIGGER source_responses_no_update
            BEFORE UPDATE ON source_responses
            BEGIN
                SELECT 1;
            END
            """
        )

    before = database_path.read_bytes()
    wal_path = Path(str(database_path) + "-wal")

    assert not wal_path.exists()

    with pytest.raises(
        RuntimeError,
        match="actual SQLite schema descriptor does not match",
    ):
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    assert database_path.read_bytes() == before
    assert not wal_path.exists()

    # The expected trigger NAME still exists. Its weakened body is
    # what must make the descriptor fail.
    with sqlite3.connect(database_path) as con:
        trigger = con.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'source_responses_no_update'
            """
        ).fetchone()

    assert trigger is not None
    assert "SELECT 1" in str(trigger[0])


def test_missing_index_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observations.sqlite"

    journal = ExperimentJournal(
        database_path,
        create_new=True,
        start_immediately=False,
    )
    assert journal.close(timeout_seconds=2.0)

    with sqlite3.connect(database_path) as con:
        mode = con.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        assert mode is not None
        assert str(mode[0]).lower() == "delete"

        con.execute(
            "DROP INDEX source_responses_lookup_idx"
        )

    before = database_path.read_bytes()
    wal_path = Path(str(database_path) + "-wal")

    assert not wal_path.exists()

    with pytest.raises(
        RuntimeError,
        match="actual SQLite schema descriptor does not match",
    ):
        ExperimentJournal(
            database_path,
            start_immediately=False,
        )

    assert database_path.read_bytes() == before
    assert not wal_path.exists()
