"""A lock that holds across threads and processes, for files several mcpbrain
processes write (the records repo, config.json).

Two layers, taken in this order and released in reverse:

- an in-process RLock, so threads of one process queue up and a holder can
  take it again (reentrant);
- an exclusive OS lock on a lock file (flock on POSIX, msvcrt.locking on
  Windows), so other processes queue up too.

The lock file is never deleted. A holder that dies has its OS lock dropped by
the OS, so nothing goes stale and nothing needs cleaning up.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Callable


def _try_lock(fd: int) -> bool:
    if os.name == "nt":  # pragma: no cover -- Windows
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover -- Windows
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class CrossProcessLock:
    """One in-process RLock plus an OS lock per lock-file path."""

    def __init__(self) -> None:
        self._rlock = threading.RLock()
        # Open OS locks this process holds, by resolved path: [fd, depth].
        # Only the thread holding _rlock reads or writes it.
        self._held: dict[str, list] = {}

    @contextmanager
    def hold(self, path: str | None, *, timeout: float, busy: Callable[[], Exception]):
        """Hold the lock (and the OS lock on `path`, when given) or raise
        busy() once `timeout` seconds pass. Without a path only this
        process's threads are serialised."""
        deadline = time.monotonic() + timeout
        if not self._rlock.acquire(timeout=timeout):
            raise busy()
        key = os.path.realpath(path) if path else None
        try:
            if key is not None:
                self._acquire(key, deadline, busy)
        except BaseException:
            self._rlock.release()
            raise
        try:
            yield
        finally:
            try:
                if key is not None:
                    self._release(key)
            finally:
                self._rlock.release()

    def _acquire(self, key: str, deadline: float, busy) -> None:
        held = self._held.get(key)
        if held is not None:          # reentrant: this process already holds it
            held[1] += 1
            return
        fd = os.open(key, os.O_RDWR | os.O_CREAT, 0o600)
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise busy()
            time.sleep(0.02)
        self._held[key] = [fd, 1]

    def _release(self, key: str) -> None:
        held = self._held[key]
        held[1] -= 1
        if held[1] == 0:
            del self._held[key]
            _unlock(held[0])
