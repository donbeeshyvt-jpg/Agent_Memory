"""Memory providers for runtime write operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter


def _infer_type_from_path(path: str) -> MemoryType:
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized.startswith("10_Permanent/Profiles/"):
        return MemoryType.USER_PROFILE
    if normalized.startswith("11_AI_Mirror/ingestion_logs/daily_flush/"):
        return MemoryType.SHORT_TERM
    if normalized.startswith("00_System/Skills/"):
        return MemoryType.SKILL
    if normalized.startswith("70_Active_Plans/Session_Logs/"):
        return MemoryType.SESSION
    if normalized.startswith("10_Permanent/Concepts/"):
        return MemoryType.CONCEPT
    return MemoryType.LONG_TERM


def _normalize_relative(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/")


class MemoryProvider(ABC):
    """Abstract memory provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider short name."""

    @abstractmethod
    def add_memory(
        self,
        *,
        path: str,
        content: str,
        agent: str,
        source: MemorySource,
        tags: list[str] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> MemoryNote:
        """Add one memory entry."""

    @abstractmethod
    def replace_memory(
        self,
        *,
        path: str,
        content: str,
        agent: str,
        source: MemorySource,
        tags: list[str] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> MemoryNote:
        """Replace full note body."""

    @abstractmethod
    def remove_memory(self, *, path: str, reason: str, agent: str) -> MemoryNote | None:
        """Remove/Archive one note and return resulting note."""

    @abstractmethod
    def get_memory(self, *, path: str) -> MemoryNote | None:
        """Get one note."""


class BuiltinMemoryProvider(MemoryProvider):
    """Local markdown provider backed by ObsidianVaultAdapter."""

    def __init__(self, adapter: ObsidianVaultAdapter):
        self.adapter = adapter

    @property
    def name(self) -> str:
        return "builtin"

    def add_memory(
        self,
        *,
        path: str,
        content: str,
        agent: str,
        source: MemorySource,
        tags: list[str] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> MemoryNote:
        normalized = _normalize_relative(path)
        existing = self.adapter.read_note(normalized)
        if existing is None:
            mtype = _infer_type_from_path(normalized)
            frontmatter = Frontmatter(
                type=mtype,
                source=source,
                agent=agent,
                tags=tags or [mtype.value],
                extras=extras or {},
            )
            title = normalized.split("/")[-1].removesuffix(".md")
            body = f"# {title}\n\n## Entries\n\n- {content.strip()}\n"
            note = MemoryNote(path=normalized, frontmatter=frontmatter, body=body)
        else:
            existing.frontmatter.agent = agent
            if tags:
                existing.frontmatter.tags = sorted(set(existing.frontmatter.tags + tags))
            if extras:
                existing.frontmatter.extras.update(extras)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            body = existing.body.rstrip() + f"\n- [{stamp}] {content.strip()}\n"
            note = MemoryNote(path=normalized, frontmatter=existing.frontmatter, body=body)

        self.adapter.write_note(note)
        stored = self.adapter.read_note(normalized)
        return stored or note

    def replace_memory(
        self,
        *,
        path: str,
        content: str,
        agent: str,
        source: MemorySource,
        tags: list[str] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> MemoryNote:
        normalized = _normalize_relative(path)
        existing = self.adapter.read_note(normalized)
        if existing is None:
            mtype = _infer_type_from_path(normalized)
            frontmatter = Frontmatter(
                type=mtype,
                source=source,
                agent=agent,
                tags=tags or [mtype.value],
                extras=extras or {},
            )
        else:
            frontmatter = existing.frontmatter
            frontmatter.agent = agent
            if tags:
                frontmatter.tags = sorted(set(frontmatter.tags + tags))
            if extras:
                frontmatter.extras.update(extras)

        body = content.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
        note = MemoryNote(path=normalized, frontmatter=frontmatter, body=body)
        self.adapter.write_note(note)
        stored = self.adapter.read_note(normalized)
        return stored or note

    def remove_memory(self, *, path: str, reason: str, agent: str) -> MemoryNote | None:
        normalized = _normalize_relative(path)
        existing = self.adapter.read_note(normalized)
        if existing is None:
            return None
        existing.frontmatter.agent = agent
        existing.frontmatter.status = "archived"
        existing.frontmatter.extras["removed"] = True
        existing.frontmatter.extras["removed_reason"] = reason or "no reason"
        self.adapter.write_note(existing)
        return self.adapter.read_note(normalized)

    def get_memory(self, *, path: str) -> MemoryNote | None:
        return self.adapter.read_note(_normalize_relative(path))
