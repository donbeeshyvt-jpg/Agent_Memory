"""Atomic file write helpers."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# R19 P0 — Codex 第 30b 暴露 atomic replace race (WinError 5):
# Windows os.replace 在目標檔被另一 process 短暫鎖定 (防毒/索引/編輯器/並發 writer)
# 時會拋 PermissionError. 用 exponential backoff retry, cap 2.0s, 6 次嘗試
# (sleep 0.5+1.0+2.0+2.0+2.0=7.5s 內可恢復), 全部失敗才把錯誤往上拋讓上游清楚知道.
_REPLACE_MAX_ATTEMPTS = 6
_REPLACE_INITIAL_BACKOFF = 0.5
_REPLACE_BACKOFF_MULTIPLIER = 2.0
_REPLACE_MAX_BACKOFF = 2.0


def _compute_replace_backoff(attempt_index: int) -> float:
    raw = _REPLACE_INITIAL_BACKOFF * (_REPLACE_BACKOFF_MULTIPLIER ** attempt_index)
    return min(raw, _REPLACE_MAX_BACKOFF)


def atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write file atomically via temp file + replace.

    Windows-aware: 若 os.replace 撞 PermissionError (WinError 5), 重試最多
    _REPLACE_MAX_ATTEMPTS 次, 每次間隔 exponential backoff cap 2s.
    全部失敗才把最後一次 PermissionError 往上拋, 上游能明確知道沒寫成功.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    replace_succeeded = False
    last_replace_err: PermissionError | None = None
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())

        for attempt in range(_REPLACE_MAX_ATTEMPTS):
            try:
                os.replace(temp_path, path)
                replace_succeeded = True
                break
            except PermissionError as exc:
                last_replace_err = exc
                if attempt == _REPLACE_MAX_ATTEMPTS - 1:
                    break
                time.sleep(_compute_replace_backoff(attempt))

        if not replace_succeeded and last_replace_err is not None:
            raise last_replace_err
    finally:
        if not replace_succeeded and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except (PermissionError, OSError):
                pass  # best-effort cleanup; 不 shadow 真正的 PermissionError
