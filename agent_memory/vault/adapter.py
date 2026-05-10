"""Abstract vault adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from agent_memory.types import MemoryNote, MemoryType


class VaultAdapter(ABC):
    """Obsidian vault read/write abstraction."""

    @property
    @abstractmethod
    def vault_root(self) -> Path:
        """Absolute path of vault root."""

    @abstractmethod
    def ensure_skeleton(self) -> None:
        """Create required folder structure and baseline notes."""

    @abstractmethod
    def resolve_path(self, layer: MemoryType, key: str) -> str:
        """Resolve memory layer + key to vault-relative path."""

    @abstractmethod
    def absolute_path(self, relative: str) -> Path:
        """Convert vault-relative path to absolute path."""

    @abstractmethod
    def read_note(self, path: str) -> Optional[MemoryNote]:
        """Read one markdown note."""

    @abstractmethod
    def list_notes(self, layer: MemoryType) -> list[str]:
        """List notes under one layer."""

    @abstractmethod
    def write_note(self, note: MemoryNote, *, lock_timeout: float = 5.0) -> None:
        """Write note with lock + atomic write."""

    @abstractmethod
    def append_daily(self, date: str, entry: str, *, agent: str = "agent") -> None:
        """Append one short-term memory entry into daily flush note."""

    @abstractmethod
    def archive_note(self, path: str, *, reason: str = "") -> None:
        """Mark a note as archived."""

    @abstractmethod
    def delete_note(self, path: str) -> bool:
        """Delete one note."""

    @abstractmethod
    def parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """Parse markdown text into metadata dict and body."""

    @abstractmethod
    def serialize_frontmatter(self, metadata: dict, body: str) -> str:
        """Serialize metadata + body to markdown text."""

    @abstractmethod
    def obsidian_uri(self, path: str) -> str:
        """Build obsidian:// link for jump-to-note."""
