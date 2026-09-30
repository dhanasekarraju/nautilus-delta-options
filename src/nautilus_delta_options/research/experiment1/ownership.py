from __future__ import annotations

import errno
import fcntl
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self


class CollectorLease:
    """Exclusive host-level ownership of one Experiment 1 journal.

    The lock file is only a stable inode used by ``flock``.  Its
    continued existence does not mean the collector is running:
    ownership is held by the kernel for the lifetime of the open
    file descriptor and is automatically released on process exit.

    The lock file must not be unlinked on normal release because
    unlinking could let two processes lock different inodes under
    the same pathname.
    """

    def __init__(
        self,
        *,
        database_path: Path,
        lock_path: Path,
        handle: BinaryIO,
    ) -> None:
        self._database_path = database_path
        self._lock_path = lock_path
        self._handle: BinaryIO | None = handle

    @classmethod
    def acquire(
        cls,
        database_path: str | Path,
    ) -> Self:
        database = Path(database_path).resolve()

        if not database.exists():
            raise FileNotFoundError(
                "Experiment 1 journal database does not exist"
            )

        if not database.is_file():
            raise RuntimeError(
                "Experiment 1 journal path is not a file"
            )

        lock_path = database.with_name(
            database.name + ".collector.lock"
        )

        handle = lock_path.open("a+b")

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as error:
            handle.close()

            if error.errno in (
                errno.EACCES,
                errno.EAGAIN,
            ):
                raise RuntimeError(
                    "Experiment 1 collector lease is already held"
                ) from error

            raise

        return cls(
            database_path=database,
            lock_path=lock_path,
            handle=handle,
        )

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def is_held(self) -> bool:
        return self._handle is not None

    def close(self) -> None:
        handle = self._handle

        if handle is None:
            return

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_UN,
            )
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        if self._handle is None:
            raise RuntimeError(
                "collector lease has already been released"
            )

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()
