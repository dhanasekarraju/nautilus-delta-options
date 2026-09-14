"""Linux service ownership and cancellation-safe blocking I/O."""

import asyncio
import fcntl
import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self


async def run_blocking[**P, T](
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Drain the worker before allowing shutdown to release database ownership."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        # Repeated cancellation must not detach the still-running worker.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except Exception:
            logging.getLogger(__name__).exception("Blocking operation failed during shutdown")
        raise cancelled


class DatabaseLease:
    """Advisory single-service ownership; SQLite revision checks remain authoritative."""

    def __init__(self, database: Path) -> None:
        self.path = Path(str(database.resolve()) + ".service.lock")
        self._fd: int | None = None

    def __enter__(self) -> Self:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            raise RuntimeError(f"Another service owns database lease: {self.path}") from error
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
