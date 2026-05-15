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


class EtlStatus(str, Enum):
    """資料成熟度 (V2 schema)。對應規格 13_universal_memory_format.md.

    - raw         : 原始輸入 (20 Literature / 80 Fleeting / 90 Daily)
    - processing  : 處理中 / 沙盒 (30-60 領域 / 70 Active Plans)
    - internalised: 已內化 / 永久 (10 Permanent / 00 System / 11 AI Mirror promoted)
    - archived    : 已歸檔 (99 Archive)
    """

    RAW = "raw"
    PROCESSING = "processing"
    INTERNALISED = "internalised"
    ARCHIVED = "archived"


class SecurityLevel(str, Enum):
    """權限分級 (V2 schema). 結合 path filter + metadata filter 控制 RAG 邊界.

    - safe_data    : 預設, AI 可讀寫 (絕大多數)
    - restricted   : raw 區或個人敏感 (20/80/90, 個人偏好), AI 預設不主動引用
    - confidential : 私密 (token / API key / 私人對話), AI 永遠不可外洩到回覆
    """

    SAFE_DATA = "safe_data"
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"


@dataclass(slots=True)
class Frontmatter:
    """Standard frontmatter schema for memory markdown.

    V2 新增 (commit C1):
    - ai_ready: 是否允許 RAG 檢索 (path 黑名單壓過此欄位; 預設依 path 推斷)
    - etl_status: 資料成熟度 (見 EtlStatus enum)
    - security_level: 權限分級 (見 SecurityLevel enum)
    - aliases: 同義詞 (BM25 命中提升, GraphRAG entity link)

    背向相容: 既有檔 YAML 缺欄位時, parse 用 sensible default.
    """

    type: MemoryType
    source: MemorySource
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agent: str = "agent-memory-core"
    status: str = "active"
    schema_version: int = 2  # bump: V2 加 3 欄位 + aliases
    tags: list[str] = field(default_factory=list)
    char_count: int = 0
    extras: dict[str, Any] = field(default_factory=dict)
    # V2 新欄位
    ai_ready: bool = True
    etl_status: EtlStatus = EtlStatus.PROCESSING
    security_level: SecurityLevel = SecurityLevel.SAFE_DATA
    aliases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemoryNote:
    """Memory note object."""

    path: str
    frontmatter: Frontmatter
    body: str
