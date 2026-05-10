"""Session pre-flush + compaction helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_memory.chat_session import sanitize_component, session_note_path
from agent_memory.llm_client import LLMClient, LLMClientError
from agent_memory.search import MemorySearchManager
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import MemoryType
from agent_memory.vault import ObsidianVaultAdapter

SESSION_COMPACTION_EVENTS_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/session_compaction.md"
SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] 前段對話已摘要；"
    "以此摘要與後續保留回合延續，不要重複已完成工作。"
)
_TURN_RE = re.compile(
    r"^##\s*(?P<stamp>[^\n]+)\n\n### User\n(?P<user>.*?)\n\n### Assistant\n(?P<assistant>.*?)(?=^##\s|\Z)",
    flags=re.DOTALL | re.MULTILINE,
)


@dataclass(slots=True)
class SessionTurn:
    stamp: str
    user: str
    assistant: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_line(text: str, *, max_len: int = 180) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def _split_header_and_turn_blob(body: str) -> tuple[str, str]:
    idx = body.find("\n## ")
    if idx < 0:
        return body.rstrip() + "\n", ""
    return body[: idx + 1], body[idx + 1 :]


def _parse_turns(turn_blob: str) -> list[SessionTurn]:
    turns: list[SessionTurn] = []
    if not turn_blob.strip():
        return turns
    for match in _TURN_RE.finditer(turn_blob):
        turns.append(
            SessionTurn(
                stamp=match.group("stamp").strip(),
                user=match.group("user").strip(),
                assistant=match.group("assistant").strip(),
            )
        )
    return turns


def _render_turn(turn: SessionTurn) -> str:
    return (
        f"## {turn.stamp}\n\n"
        f"### User\n{turn.user}\n\n"
        f"### Assistant\n{turn.assistant}\n"
    )


def _heuristic_summary(turns: list[SessionTurn], *, max_turns: int = 16) -> str:
    if not turns:
        return "- （無可摘要回合）"
    sampled = turns[-max_turns:]
    lines = ["## 早期對話摘要", ""]
    for item in sampled:
        user_text = _compact_line(item.user, max_len=100)
        assistant_text = _compact_line(item.assistant, max_len=120)
        lines.append(f"- {item.stamp}｜U: {user_text}")
        lines.append(f"  - A: {assistant_text}")
    return "\n".join(lines).rstrip()


def _llm_summary(
    *,
    client: LLMClient,
    persona_id: str,
    transcript: str,
    timeout_s: float,
    override_profile: str | None = None,
    override_model: str | None = None,
) -> str:
    prompt = (
        "請把以下舊對話摘要成繁體中文 markdown，"
        "只保留已解決事項、待辦、決策與上下文延續重點。\n\n"
        f"{transcript}"
    )
    result = client.generate(
        messages=[
            {
                "role": "system",
                "content": "你是對話壓縮助手。只能輸出 markdown 摘要，不可延伸執行任務。",
            },
            {"role": "user", "content": prompt},
        ],
        persona_id=persona_id,
        override_profile=override_profile,
        override_model=override_model,
        temperature=0.0,
        timeout_s=timeout_s,
    )
    text = result.content.strip()
    if not text:
        raise LLMClientError("llm summary empty")
    return text


def _append_compaction_event(vault_root: Path, *, payload: dict[str, Any]) -> str:
    root = Path(vault_root).expanduser().resolve()
    target = (root / SESSION_COMPACTION_EVENTS_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, "# session_compaction\n\n> session pre-flush + compact 事件台帳。\n\n")
    block = (
        f"## {payload.get('timestamp', _now_iso())} compact\n\n"
        f"- session_path: `{payload.get('session_path', '')}`\n"
        f"- daily_flush_path: `{payload.get('daily_flush_path', '')}`\n"
        f"- compacted_turns: `{payload.get('compacted_turns', 0)}`\n"
        f"- kept_turns: `{payload.get('kept_turns', 0)}`\n"
        f"- used_llm_summary: `{payload.get('used_llm_summary', False)}`\n\n"
    )
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + block)
    return str(target.relative_to(root)).replace("\\", "/")


def compact_session_memory(
    vault_root: Path,
    *,
    persona_id: str,
    context_id: str,
    session_id: str,
    date: str | None = None,
    max_chars: int = 12000,
    keep_recent_turns: int = 6,
    use_llm_summary: bool = False,
    llm_persona: str = "core",
    override_profile: str | None = None,
    override_model: str | None = None,
    timeout_s: float = 45.0,
) -> dict[str, Any]:
    """Pre-flush old turns and compact one session log."""

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    session_path = session_note_path(
        adapter,
        persona_id=sanitize_component(persona_id, fallback="core"),
        context_id=sanitize_component(context_id, fallback="cli"),
        session_id=sanitize_component(session_id, fallback="default"),
        date_str=date,
    )
    note = adapter.read_note(session_path)
    if note is None:
        raise FileNotFoundError(f"session note not found: {session_path}")

    body = note.body
    header, turn_blob = _split_header_and_turn_blob(body)
    turns = _parse_turns(turn_blob)
    should_compact = len(body) > max(600, int(max_chars)) and len(turns) > max(2, int(keep_recent_turns))
    if not should_compact:
        return {
            "status": "skipped",
            "session_path": session_path,
            "reason": "below_threshold",
            "body_chars": len(body),
            "turns": len(turns),
        }

    keep_n = max(2, int(keep_recent_turns))
    old_turns = turns[:-keep_n]
    kept_turns = turns[-keep_n:]

    summary = _heuristic_summary(old_turns)
    used_llm = False
    if use_llm_summary and old_turns:
        transcript_lines: list[str] = []
        for item in old_turns[-20:]:
            transcript_lines.append(f"[{item.stamp}] user: {_compact_line(item.user, max_len=260)}")
            transcript_lines.append(f"[{item.stamp}] assistant: {_compact_line(item.assistant, max_len=320)}")
        transcript = "\n".join(transcript_lines).strip()
        if transcript:
            try:
                client = LLMClient(adapter.vault_root)
                summary = _llm_summary(
                    client=client,
                    persona_id=llm_persona,
                    transcript=transcript,
                    timeout_s=float(timeout_s),
                    override_profile=override_profile,
                    override_model=override_model,
                )
                used_llm = True
            except Exception:
                used_llm = False

    stamp = datetime.now()
    flush_entry = (
        f"## {stamp.strftime('%H:%M')} - pre-compaction flush\n\n"
        f"- session_path: `{session_path}`\n"
        f"- compacted_turns: `{len(old_turns)}`\n"
        f"- kept_turns: `{len(kept_turns)}`\n\n"
        f"{summary.strip()}\n"
    )
    today = stamp.strftime("%Y-%m-%d")
    adapter.append_daily(today, flush_entry, agent=f"{sanitize_component(persona_id, fallback='core')}-compactor")
    daily_path = adapter.resolve_path(MemoryType.SHORT_TERM, today)

    compact_header = (
        "## Context Compaction Summary\n\n"
        f"{SUMMARY_PREFIX}\n\n"
        f"- compacted_at: `{_now_iso()}`\n"
        f"- source_turns: `{len(old_turns)}`\n"
        f"- kept_turns: `{len(kept_turns)}`\n"
        f"- used_llm_summary: `{used_llm}`\n\n"
        f"{summary.strip()}\n"
    )
    kept_blob = "\n".join(_render_turn(item).rstrip() for item in kept_turns).rstrip() + "\n"
    note.body = header.rstrip() + "\n\n" + compact_header + "\n" + kept_blob
    note.frontmatter.extras["last_compaction_at"] = _now_iso()
    note.frontmatter.extras["last_compaction_source_turns"] = len(old_turns)
    note.frontmatter.extras["last_compaction_kept_turns"] = len(kept_turns)
    note.frontmatter.extras["last_compaction_used_llm_summary"] = used_llm
    adapter.write_note(note)

    search = MemorySearchManager(adapter)
    search.index_path(session_path)
    search.index_path(daily_path)
    event_payload = {
        "timestamp": _now_iso(),
        "session_path": session_path,
        "daily_flush_path": daily_path,
        "compacted_turns": len(old_turns),
        "kept_turns": len(kept_turns),
        "used_llm_summary": used_llm,
    }
    event_path = _append_compaction_event(root, payload=event_payload)
    return {
        "status": "ok",
        "session_path": session_path,
        "daily_flush_path": daily_path,
        "compacted_turns": len(old_turns),
        "kept_turns": len(kept_turns),
        "used_llm_summary": used_llm,
        "event_path": event_path,
    }
