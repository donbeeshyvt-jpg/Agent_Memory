"""Cross-platform lock file helper."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def file_lock(target: Path, *, timeout: float = 5.0, poll_interval: float = 0.05) -> Iterator[None]:
    """Acquire an exclusive lock using sidecar lock file."""

    lock_path = Path(f"{target}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd: int | None = None
    started = time.monotonic()
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"鎖定逾時：{lock_path}") from None
            time.sleep(poll_interval)

    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except PermissionError:
                pass
