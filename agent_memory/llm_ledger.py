"""LLM route event ledger for provider/model replay."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

LLM_ROUTE_EVENTS_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/llm_route_events.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: str | None, *, max_len: int = 240) -> str:
    compact = (value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def _ensure_ledger_file(vault_root: Path) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / LLM_ROUTE_EVENTS_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, "")
    return target


def _append_json_line(target: Path, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=False) + "\n"
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        atomic_write(target, existing + line)


def record_llm_route_event(
    vault_root: Path,
    *,
    persona_id: str,
    context_id: str,
    session_id: str,
    llm: dict[str, Any],
    memory_paths: dict[str, Any],
    message: str = "",
    response: str = "",
    transport: str = "",
    channel_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """Append one route event to ledger and return stored payload."""

    target = _ensure_ledger_file(vault_root)
    failures = llm.get("fallback_failures", [])
    if not isinstance(failures, list):
        failures = []

    payload: dict[str, Any] = {
        "timestamp": _now_iso(),
        "persona_id": _normalize_text(persona_id, max_len=64),
        "context_id": _normalize_text(context_id, max_len=120),
        "session_id": _normalize_text(session_id, max_len=120),
        "transport": _normalize_text(transport, max_len=32),
        "channel_id": _normalize_text(channel_id, max_len=120),
        "user_id": _normalize_text(user_id, max_len=120),
        "profile": _normalize_text(str(llm.get("profile", "")), max_len=64),
        "model": _normalize_text(str(llm.get("model", "")), max_len=120),
        "kind": _normalize_text(str(llm.get("kind", "")), max_len=64),
        "base_url": _normalize_text(str(llm.get("base_url", "")), max_len=160),
        "fallback_failures": failures,
        "memory_session_path": _normalize_text(str(memory_paths.get("session", "")), max_len=200),
        "memory_daily_path": _normalize_text(str(memory_paths.get("daily", "")), max_len=200),
        "message_preview": _normalize_text(message, max_len=180),
        "response_preview": _normalize_text(response, max_len=180),
    }
    _append_json_line(target, payload)
    return payload


def list_llm_route_events(
    vault_root: Path,
    *,
    limit: int = 20,
    persona_id: str = "",
    session_id: str = "",
    transport: str = "",
) -> list[dict[str, Any]]:
    """List newest route events with optional filters."""

    target = _ensure_ledger_file(vault_root)
    raw = target.read_text(encoding="utf-8")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    persona = (persona_id or "").strip()
    session = (session_id or "").strip()
    transport_filter = (transport or "").strip().lower()

    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if persona and str(item.get("persona_id", "")) != persona:
            continue
        if session and str(item.get("session_id", "")) != session:
            continue
        if transport_filter and str(item.get("transport", "")).lower() != transport_filter:
            continue
        events.append(item)

    events.reverse()
    return events[: max(1, int(limit))]
