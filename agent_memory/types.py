"""Core shared types for Agent Memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """Memory layers mapped to vault folders."""

    USER_PROFILE = "user_profile"
    LONG_TERM = "long_term"
    SHORT_TERM = "short_term"
    SKILL = "skill"
    SESSION = "session"
    CONCEPT = "concept"


class MemorySource(str, Enum):
    """Source of memory entry."""

    USER = "user"
    AGENT = "agent"
    FLUSH = "flush"
    MIRROR = "mirror"
    PROMOTION = "promotion"


@dataclass(slots=True)
class Frontmatter:
    """Standard frontmatter schema for memory markdown."""

    type: MemoryType
    source: MemorySource
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agent: str = "agent-memory-core"
    status: str = "active"
    schema_version: int = 1
    tags: list[str] = field(default_factory=list)
    char_count: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryNote:
    """Memory note object."""

    path: str
    frontmatter: Frontmatter
    body: str
