"""Immutable prospective cohort identity, separate from per-process run identity."""

import hashlib
import json
import os
import platform
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path


def _json(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False)


def make_provenance(config: dict[str, object]) -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.suffix in (".py", ".html"):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    source_id = digest.hexdigest()
    build_id = os.getenv("NAUTILUS_BUILD_ID") or "source-sha256:" + source_id
    if not re.fullmatch(r"[A-Za-z0-9._:+-]{1,160}", build_id):
        raise ValueError("NAUTILUS_BUILD_ID must be an immutable plain build identifier")
    dependencies = {
        name: version(name)
        for name in ("nautilus_trader", "numpy", "TA-Lib", "fastapi", "uvicorn", "pydantic",
                     "starlette")
    }
    identity = {
        "schema": 1,
        "build_id": build_id,
        "source_sha256": source_id,
        "config": config,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "dependencies": dependencies,
    }
    return {
        "identity": identity,
        "fingerprint": hashlib.sha256(_json(identity).encode()).hexdigest(),
        "config_fingerprint": hashlib.sha256(_json(config).encode()).hexdigest(),
        "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
    }


def bind_provenance(
    path: Path, provenance: dict[str, object], *, check_only: bool = False,
) -> None:
    """Call with service database leases held. Unknown historical trades fail closed."""
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        old = (
            db.execute("SELECT fingerprint FROM validation_identity WHERE id=1").fetchone()
            if "validation_identity" in tables else None
        )
        if old is not None and old[0] != provenance["fingerprint"]:
            raise ValueError("Validation identity changed; use a fresh database pair")
        if old is None:
            if "paper_ledger_state" in tables:
                row = db.execute("SELECT payload FROM paper_ledger_state WHERE id=1").fetchone()
                if row:
                    ledger = json.loads(row[0])
                    if ledger["open_positions"] or ledger["closed_trades"]:
                        raise ValueError("Legacy trades have no provenance; preserve and reconcile "
                                         "them separately before starting a fresh validation pair")
                if db.execute("SELECT 1 FROM paper_signal_receipts LIMIT 1").fetchone():
                    raise ValueError("Legacy receipts have no provenance")
            if "cohorts" in tables and db.execute("SELECT 1 FROM cohorts LIMIT 1").fetchone():
                raise ValueError("Legacy research cohorts have no provenance")
        if check_only:
            return
        db.execute("""CREATE TABLE IF NOT EXISTS validation_identity (
            id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
            payload TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS validation_runs (
            run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
        db.execute("INSERT OR IGNORE INTO validation_identity VALUES (1, ?, ?)",
                   (provenance["fingerprint"], _json(provenance["identity"])))
        db.execute("INSERT OR IGNORE INTO validation_runs VALUES (?, ?)",
                   (provenance["run_id"], _json(provenance)))
