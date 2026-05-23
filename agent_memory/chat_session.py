"""Session-memory helpers for chat turns."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Tuple

from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

# R19 P2-b C94: 壓測時 set 此 env var (任意字串 sanitize 後當 session_id) 隔離
# shared-channel log 檔. 沒 set 就用 "shared" 維持向後兼容.
_TEST_RUN_ID_ENV = "AGENT_MEMORY_TEST_RUN_ID"


_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_component(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("-", (value or "").strip()).strip("-")
    return cleaned or fallback


def build_session_key(
    *,
    persona_id: str,
    context_id: str,
    session_id: str,
    date_str: str | None = None,
) -> str:
    now = datetime.now()
    date_part = date_str or now.strftime("%Y-%m-%d")
    persona = sanitize_component(persona_id, fallback="core")
    context = sanitize_component(context_id, fallback="cli")
    session = sanitize_component(session_id, fallback=now.strftime("s%H%M%S"))
    return f"{date_part}/{persona}__{context}__{session}"


def session_note_path(
    adapter: ObsidianVaultAdapter,
    *,
    persona_id: str,
    context_id: str,
    session_id: str,
    date_str: str | None = None,
) -> str:
    key = build_session_key(
        persona_id=persona_id,
        context_id=context_id,
        session_id=session_id,
        date_str=date_str,
    )
    return adapter.resolve_path(MemoryType.SESSION, key)


def shared_channel_note_path(
    adapter: ObsidianVaultAdapter,
    *,
    transport: str,
    channel_id: str,
    date_str: str | None = None,
) -> str:
    transport_safe = sanitize_component(transport, fallback="transport").lower()
    channel_safe = sanitize_component(channel_id, fallback="channel")
    context_id = f"{transport_safe}-{channel_safe}"
    # R19 P2-b C94: AGENT_MEMORY_TEST_RUN_ID 設了 → session_id 用 sanitized run_id
    # 隔離壓測產出; 沒設 → "shared" 維持向後兼容 (生產日常 session).
    _test_run_raw = (os.environ.get(_TEST_RUN_ID_ENV) or "").strip()
    session_id = (
        sanitize_component(_test_run_raw, fallback="shared")
        if _test_run_raw
        else "shared"
    )
    return session_note_path(
        adapter,
        persona_id="shared-channel",
        context_id=context_id,
        session_id=session_id,
        date_str=date_str,
    )


def append_chat_turn(
    adapter: ObsidianVaultAdapter,
    *,
    persona_id: str,
    context_id: str,
    session_id: str,
    user_message: str,
    assistant_message: str,
    now: datetime | None = None,
) -> str:
    stamp = now or datetime.now()
    path = session_note_path(
        adapter,
        persona_id=persona_id,
        context_id=context_id,
        session_id=session_id,
        date_str=stamp.strftime("%Y-%m-%d"),
    )

    note = adapter.read_note(path)
    turn_md = (
        f"## {stamp.isoformat(timespec='seconds')}\n\n"
        f"### User\n{user_message.strip()}\n\n"
        f"### Assistant\n{assistant_message.strip()}\n\n"
    )
    if note is None:
        frontmatter = Frontmatter(
            type=MemoryType.SESSION,
            source=MemorySource.AGENT,
            tags=["session", sanitize_component(persona_id, fallback="core")],
            agent=sanitize_component(persona_id, fallback="core"),
            created=stamp.astimezone(timezone.utc),
            updated=stamp.astimezone(timezone.utc),
            extras={
                "persona_id": sanitize_component(persona_id, fallback="core"),
                "context_id": sanitize_component(context_id, fallback="cli"),
                "session_id": sanitize_component(session_id, fallback="default"),
            },
        )
        body = (
            f"# Session Log: {sanitize_component(persona_id, fallback='core')} / "
            f"{sanitize_component(context_id, fallback='cli')} / "
            f"{sanitize_component(session_id, fallback='default')}\n\n"
            f"{turn_md}"
        )
        note = MemoryNote(path=path, frontmatter=frontmatter, body=body)
    else:
        note.frontmatter.updated = stamp.astimezone(timezone.utc)
        note.body = note.body.rstrip() + "\n\n" + turn_md

    adapter.write_note(note)
    return path


def append_daily_chat_digest(
    adapter: ObsidianVaultAdapter,
    *,
    persona_id: str,
    session_id: str,
    user_message: str,
    assistant_message: str,
    now: datetime | None = None,
) -> Tuple[str, str]:
    stamp = now or datetime.now()
    date_str = stamp.strftime("%Y-%m-%d")
    user_short = user_message.strip().replace("\n", " ")
    assistant_short = assistant_message.strip().replace("\n", " ")
    if len(user_short) > 220:
        user_short = user_short[:220] + "..."
    if len(assistant_short) > 300:
        assistant_short = assistant_short[:300] + "..."

    digest = (
        f"### chat_digest {stamp.isoformat(timespec='seconds')}\n"
        f"- persona: {sanitize_component(persona_id, fallback='core')}\n"
        f"- session: {sanitize_component(session_id, fallback='default')}\n"
        f"- user: {user_short}\n"
        f"- assistant: {assistant_short}\n"
    )
    adapter.append_daily(date_str, digest, agent=sanitize_component(persona_id, fallback="core"))
    path = adapter.resolve_path(MemoryType.SHORT_TERM, date_str)
    return path, date_str


def append_shared_channel_turn(
    adapter: ObsidianVaultAdapter,
    *,
    transport: str,
    channel_id: str,
    persona_id: str,
    user_id: str,
    user_message: str,
    assistant_message: str,
    now: datetime | None = None,
) -> str:
    """Append one turn into a shared channel log (cross-persona memory view)."""

    stamp = now or datetime.now()
    transport_safe = sanitize_component(transport, fallback="transport").lower()
    channel_safe = sanitize_component(channel_id, fallback="channel")
    path = shared_channel_note_path(
        adapter,
        transport=transport_safe,
        channel_id=channel_safe,
        date_str=stamp.strftime("%Y-%m-%d"),
    )

    note = adapter.read_note(path)
    turn_md = (
        f"## {stamp.isoformat(timespec='seconds')}\n\n"
        f"- transport: `{transport_safe}`\n"
        f"- channel_id: `{channel_safe}`\n"
        f"- persona: `{sanitize_component(persona_id, fallback='core').lower()}`\n"
        f"- user_id: `{sanitize_component(user_id, fallback='user')}`\n\n"
        f"### User\n{user_message.strip()}\n\n"
        f"### Assistant\n{assistant_message.strip()}\n\n"
    )
    if note is None:
        frontmatter = Frontmatter(
            type=MemoryType.SESSION,
            source=MemorySource.AGENT,
            tags=["session", "shared-channel", transport_safe, channel_safe],
            agent="transport-bridge",
            created=stamp.astimezone(timezone.utc),
            updated=stamp.astimezone(timezone.utc),
            extras={
                "transport": transport_safe,
                "channel_id": channel_safe,
                "scope": "shared_channel",
            },
        )
        body = (
            f"# Shared Channel Log: {transport_safe} / {channel_safe}\n\n"
            f"{turn_md}"
        )
        note = MemoryNote(path=path, frontmatter=frontmatter, body=body)
    else:
        note.frontmatter.updated = stamp.astimezone(timezone.utc)
        note.body = note.body.rstrip() + "\n\n" + turn_md

    adapter.write_note(note)
    return path
