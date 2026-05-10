"""Runtime write hooks."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from agent_memory.runtime import MemoryWriteEvent
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock


def write_event_logger(vault_root: Path) -> Callable[[MemoryWriteEvent], None]:
    """Create a hook that appends write events to .ai/memory_write_events.jsonl."""

    log_path = Path(vault_root).resolve() / ".ai" / "memory_write_events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        atomic_write(log_path, "")

    def _hook(event: MemoryWriteEvent) -> None:
        payload = asdict(event)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with file_lock(log_path, timeout=5.0):
            existing = log_path.read_text(encoding="utf-8")
            atomic_write(log_path, existing + line)

    return _hook
