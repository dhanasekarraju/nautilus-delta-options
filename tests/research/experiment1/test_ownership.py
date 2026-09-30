from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nautilus_delta_options.research.experiment1.ownership import (
    CollectorLease,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "observations.sqlite"
    path.write_bytes(b"placeholder")
    return path


def test_second_collector_cannot_hold_same_lease(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    first = CollectorLease.acquire(database)

    try:
        assert first.is_held is True
        assert first.database_path == database.resolve()

        with pytest.raises(
            RuntimeError,
            match="collector lease is already held",
        ):
            CollectorLease.acquire(database)
    finally:
        first.close()


def test_released_lease_can_be_reacquired(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    first = CollectorLease.acquire(database)
    lock_path = first.lock_path

    assert lock_path.exists()

    first.close()

    assert first.is_held is False

    second = CollectorLease.acquire(database)

    try:
        assert second.is_held is True
        assert second.lock_path == lock_path
    finally:
        second.close()

    # The stable inode intentionally remains after release.
    assert lock_path.exists()


def test_process_exit_releases_collector_lease(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    code = """
import sys
import time

from nautilus_delta_options.research.experiment1.ownership import (
    CollectorLease,
)

lease = CollectorLease.acquire(sys.argv[1])
print("LOCKED", flush=True)
time.sleep(60)
"""

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(database),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "LOCKED"

        with pytest.raises(
            RuntimeError,
            match="collector lease is already held",
        ):
            CollectorLease.acquire(database)

    finally:
        process.terminate()
        process.wait(timeout=5)

    # Kernel ownership must disappear with the dead process.
    with CollectorLease.acquire(database) as recovered:
        assert recovered.is_held is True


def test_missing_database_cannot_acquire_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing.sqlite"

    with pytest.raises(
        FileNotFoundError,
        match="journal database does not exist",
    ):
        CollectorLease.acquire(database)
