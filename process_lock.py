"""OS-released single-process lock for one downloader state file."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path


class DownloadInUse(RuntimeError):
    pass


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_local = threading.local()


def _thread_lock(path: Path) -> tuple[str, threading.RLock]:
    key = os.path.normcase(str(path.resolve()))
    with _registry_guard:
        lock = _thread_locks.setdefault(key, threading.RLock())
    return key, lock


@contextmanager
def process_lock(state_path: Path):
    """Prevent two CLI runs from bypassing a shared per-domain rate state.

    The lock file remains after exit, but the OS lock is released on normal
    exit or a crash. Its presence alone does not mean a process is running.
    """
    lock_path = Path(state_path).with_name(Path(state_path).name + ".lock")
    key, thread_lock = _thread_lock(lock_path)
    thread_lock.acquire()
    depths = getattr(_local, "depths", None)
    if depths is None:
        depths = _local.depths = {}
    depth = depths.get(key, 0)
    if depth:
        depths[key] = depth + 1
        try:
            yield
        finally:
            depths[key] -= 1
            thread_lock.release()
        return

    depths[key] = 1
    descriptor = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise DownloadInUse(f"Another downloader is using {state_path}") from exc
        yield
    finally:
        if acquired and descriptor is not None:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        if descriptor is not None:
            os.close(descriptor)
        depths.pop(key, None)
        thread_lock.release()
