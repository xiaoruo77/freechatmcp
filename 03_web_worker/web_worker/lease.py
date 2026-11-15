"""One OS-owned execution lease per task database; crashes release it automatically."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class WorkerAlreadyRunning(RuntimeError):
    pass


class WorkerLease:
    def __init__(self, database: Path):
        canonical = database.resolve()
        self.path = canonical.with_name(canonical.name + ".worker.lock")
        self._file: BinaryIO | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise WorkerAlreadyRunning("An execution worker already owns this task database") from exc
        self._file = handle
        return self

    def __exit__(self, exc_type, exc, traceback):
        handle, self._file = self._file, None
        if handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                # Keep the path stable: deleting a lock file lets a new worker
                # create another inode while the first still owns the old one.
                handle.close()
