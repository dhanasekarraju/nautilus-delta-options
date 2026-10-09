from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_APPLICATION_ID = "nautilus-delta-options.experiment1.journal"
_SQLITE_APPLICATION_ID = 0x45583131
_SCHEMA_VERSION = 3
_SERIALIZATION_VERSION = 1

_INCOMPATIBLE_PREFIX = (
    "existing Experiment 1 journal schema is incomplete or incompatible: "
)


@dataclass(frozen=True, slots=True)
class SchemaExpectation:
    schema_sql: str
    schema_sql_sha256: str
    schema_descriptor_sha256: str


def build_schema_expectation(
    schema_path: Path,
) -> SchemaExpectation:
    schema_bytes = schema_path.read_bytes()
    schema_sql = schema_bytes.decode("utf-8")
    schema_sql_sha256 = hashlib.sha256(schema_bytes).hexdigest()

    with sqlite3.connect(":memory:") as con:
        con.executescript(schema_sql)
        descriptor_sha256 = schema_descriptor_sha256(con)

    return SchemaExpectation(
        schema_sql=schema_sql,
        schema_sql_sha256=schema_sql_sha256,
        schema_descriptor_sha256=descriptor_sha256,
    )


def initialize_new_database_identity(
    con: sqlite3.Connection,
    expectation: SchemaExpectation,
    *,
    created_at_utc_ns: int,
) -> None:
    con.execute(
        f"PRAGMA application_id = {_SQLITE_APPLICATION_ID}"
    )
    con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    actual_descriptor = schema_descriptor_sha256(con)

    if actual_descriptor != expectation.schema_descriptor_sha256:
        raise RuntimeError(
            "packaged Experiment 1 schema descriptor changed "
            "during database creation"
        )

    con.execute(
        """
        INSERT INTO journal_schema_identity (
            identity_id,
            application_id,
            schema_version,
            schema_sql_sha256,
            schema_descriptor_sha256,
            serialization_version,
            created_at_utc_ns
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            _APPLICATION_ID,
            _SCHEMA_VERSION,
            expectation.schema_sql_sha256,
            expectation.schema_descriptor_sha256,
            _SERIALIZATION_VERSION,
            created_at_utc_ns,
        ),
    )


def validate_database_identity(
    con: sqlite3.Connection,
    expectation: SchemaExpectation,
) -> None:
    application_row = con.execute(
        "PRAGMA application_id"
    ).fetchone()
    application_id = (
        None if application_row is None else int(application_row[0])
    )

    if application_id != _SQLITE_APPLICATION_ID:
        raise _incompatible(
            "SQLite application_id does not match"
        )

    version_row = con.execute("PRAGMA user_version").fetchone()
    schema_version = (
        None if version_row is None else int(version_row[0])
    )

    if schema_version != _SCHEMA_VERSION:
        raise _incompatible(
            "SQLite user_version does not match"
        )

    try:
        rows = con.execute(
            """
            SELECT
                identity_id,
                application_id,
                schema_version,
                schema_sql_sha256,
                schema_descriptor_sha256,
                serialization_version
            FROM journal_schema_identity
            ORDER BY identity_id
            """
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise _incompatible(
            "journal_schema_identity is missing or unreadable"
        ) from exc

    if len(rows) != 1:
        raise _incompatible(
            "journal_schema_identity must contain exactly one row"
        )

    row = rows[0]

    expected_identity = (
        1,
        _APPLICATION_ID,
        _SCHEMA_VERSION,
        expectation.schema_sql_sha256,
        expectation.schema_descriptor_sha256,
        _SERIALIZATION_VERSION,
    )

    if tuple(row) != expected_identity:
        raise _incompatible(
            "stored schema identity does not match packaged schema"
        )

    actual_descriptor = schema_descriptor_sha256(con)

    if actual_descriptor != expectation.schema_descriptor_sha256:
        raise _incompatible(
            "actual SQLite schema descriptor does not match "
            "packaged schema"
        )


def schema_descriptor_sha256(
    con: sqlite3.Connection,
) -> str:
    descriptor = _schema_descriptor(con)

    canonical = json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hashlib.sha256(canonical).hexdigest()


def _schema_descriptor(
    con: sqlite3.Connection,
) -> dict[str, object]:
    master_rows = con.execute(
        """
        SELECT
            type,
            name,
            tbl_name,
            sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()

    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": None if row[3] is None else str(row[3]),
        }
        for row in master_rows
    ]

    table_names = sorted(
        str(row[1])
        for row in master_rows
        if str(row[0]) == "table"
    )

    tables: list[dict[str, object]] = []

    for table_name in table_names:
        columns = [
            {
                "cid": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2]),
                "notnull": int(row[3]),
                "default": row[4],
                "pk": int(row[5]),
                "hidden": int(row[6]),
            }
            for row in con.execute(
                "PRAGMA table_xinfo("
                + _pragma_argument(table_name)
                + ")"
            ).fetchall()
        ]

        foreign_keys = [
            {
                "id": int(row[0]),
                "seq": int(row[1]),
                "table": str(row[2]),
                "from": str(row[3]),
                "to": None if row[4] is None else str(row[4]),
                "on_update": str(row[5]),
                "on_delete": str(row[6]),
                "match": str(row[7]),
            }
            for row in con.execute(
                "PRAGMA foreign_key_list("
                + _pragma_argument(table_name)
                + ")"
            ).fetchall()
        ]

        indexes: list[dict[str, object]] = []

        index_rows = con.execute(
            "PRAGMA index_list("
            + _pragma_argument(table_name)
            + ")"
        ).fetchall()

        for index_row in index_rows:
            index_name = str(index_row[1])

            index_columns = [
                {
                    "seqno": int(row[0]),
                    "cid": int(row[1]),
                    "name": (
                        None
                        if row[2] is None
                        else str(row[2])
                    ),
                    "desc": int(row[3]),
                    "coll": (
                        None
                        if row[4] is None
                        else str(row[4])
                    ),
                    "key": int(row[5]),
                }
                for row in con.execute(
                    "PRAGMA index_xinfo("
                    + _pragma_argument(index_name)
                    + ")"
                ).fetchall()
            ]

            indexes.append(
                {
                    "name": index_name,
                    "unique": int(index_row[2]),
                    "origin": str(index_row[3]),
                    "partial": int(index_row[4]),
                    "columns": index_columns,
                }
            )

        indexes.sort(key=lambda item: str(item["name"]))

        tables.append(
            {
                "name": table_name,
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        )

    return {
        "descriptor_version": 1,
        "objects": objects,
        "tables": tables,
    }


def _pragma_argument(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _incompatible(reason: str) -> RuntimeError:
    return RuntimeError(_INCOMPATIBLE_PREFIX + reason)
