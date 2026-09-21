"""Exclusive run lock, so two backup runs cannot overlap."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Lives in the state directory rather than /run: the systemd unit declares
# RuntimeDirectory nowhere, and a runtime directory would be recreated under a
# holder still holding its open descriptor, letting a second run lock the new
# file and proceed. The state directory persists, so both invocations always
# contend for the same inode.
LOCK_PATH = Path("/var/lib/zpbs-backup/run.lock")

# sysexits.h EX_TEMPFAIL. Distinct from 1 so a concurrent invocation is not
# reported as a backup failure; the systemd unit lists it in SuccessExitStatus.
EXIT_ALREADY_RUNNING = 75


class AlreadyRunning(RuntimeError):
    """Another zpbs-backup run holds the lock."""


@contextmanager
def run_lock(path: Path | None = None) -> Iterator[Path]:
    """Hold an exclusive lock for the duration of a run.

    Uses flock, so the kernel releases the lock when the holder exits for any
    reason, including SIGKILL — there is no stale lock file to clean up, and a
    leftover file on disk means nothing by itself.

    The descriptor is opened non-inheritable (Python's default since 3.4), so
    proxmox-backup-client and other children do not keep the lock alive past
    the run that took it.

    Args:
        path: Lock file to use (defaults to LOCK_PATH).

    Yields:
        The lock file path.

    Raises:
        AlreadyRunning: If another process already holds the lock.
        OSError: If the lock file cannot be created or opened.
    """
    lock_path = path or LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"another zpbs-backup run holds {lock_path}") from None

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
