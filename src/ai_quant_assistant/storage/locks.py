from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import RLock, local


def serialized_download(dataset: str | None = None):
    """Coordinate every entry point, including direct CLI syncs, without a DB lock.

    Fixed order: optional operation/managed slot -> dataset download -> DB access.
    Contention is explicit and bounded; callers may queue/retry, never bypass it.
    """
    def decorate(function):
        @wraps(function)
        def run(service, *args, **kwargs):
            selected = dataset or args[0]
            if selected not in {"daily_bar", "benchmark_daily_bar", "announcement", "financial_metric"}:
                raise ValueError("unsupported download lock dataset")
            lock = InterProcessFileLock(
                service.database.layout.root / f".{selected}-download.lock",
                description=f"{selected} download already active; retry after it completes",
            )
            with lock.hold(timeout_seconds=0):
                return function(service, *args, **kwargs)
        return run
    return decorate

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX production path
    msvcrt = None


class InterProcessFileLock:
    """A re-entrant process and host-local file lock with a bounded wait."""

    def __init__(self, path: Path, *, description: str) -> None:
        self.path = path
        self.description = description
        self._local_lock = RLock()
        self._thread_state = local()

    @contextmanager
    def hold(self, *, timeout_seconds: float = 30.0) -> Iterator[None]:
        timeout = max(0.0, float(timeout_seconds))
        if not self._local_lock.acquire(timeout=timeout):
            raise RuntimeError(f"timed out after {timeout:g}s waiting for {self.description}")
        depth = int(getattr(self._thread_state, "depth", 0))
        if depth:
            self._thread_state.depth = depth + 1
            try:
                yield
            finally:
                self._thread_state.depth -= 1
                self._local_lock.release()
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.tell() == 0 and self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            self._acquire_os_lock(handle, timeout)
            self._thread_state.depth = 1
            try:
                yield
            finally:
                self._thread_state.depth = 0
                self._release_os_lock(handle)
        finally:
            handle.close()
            self._local_lock.release()

    def _acquire_os_lock(self, handle, timeout: float) -> None:  # noqa: ANN001
        deadline = time.monotonic() + timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows development fallback
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"timed out after {timeout:g}s waiting for {self.description}"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _release_os_lock(handle) -> None:  # noqa: ANN001
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows development fallback
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
