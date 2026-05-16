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


class LifecycleState(str, Enum):
    """三層升降格狀態 (Round 7 C16). 對應 V2_Round7 §2-§4.

    跟 etl_status 互補:
    - etl_status     描述「資料成熟度」(raw → processing → internalised → archived)
    - lifecycle_state 描述「升降格狀態」(short → mid → long → stale → archived)
    - 一個檔可能 etl_status=internalised + lifecycle_state=stale (已升長期但 90d 無命中)

    狀態:
    - short    : 短期 (Session_Logs / daily_flush) — N1=2 + 24h grace + 跨 2 session → mid
    - mid      : 中期可變 (10_Permanent/Mid_Term/) — N2=3 + stable≥7d + no-edit≥3d → long
    - long     : 長期凍結 (MEMORY/Profiles/Facts/Concepts/Manual_Inputs) — 90d 無命中 → stale
    - stale    : 標記但保留位置 — 180d 無命中 → archived
    - archived : 已移到 99_Archive/auto_archived/<YYYY>/ (不刪檔, pinned 可保護)
    """

    SHORT = "short"
    MID = "mid"
    LONG = "long"
    STALE = "stale"
    ARCHIVED = "archived"


@dataclass(slots=True)
class Frontmatter:
    """Standard frontmatter schema for memory markdown.

    V2 新增 (commit C1):
    - ai_ready: 是否允許 RAG 檢索 (path 黑名單壓過此欄位; 預設依 path 推斷)
    - etl_status: 資料成熟度 (見 EtlStatus enum)
    - security_level: 權限分級 (見 SecurityLevel enum)
    - aliases: 同義詞 (BM25 命中提升, GraphRAG entity link)

    Round 7 新增 (commit C16):
    - lifecycle_state: 升降格狀態 (見 LifecycleState enum)
    - mention_count: 累計被提及次數 (短→中升 N1=2 / 中→長升 N2=3 用)
    - last_activity_at: 最後一次被 RAG 命中 / LLM 動到的時間 (本機時區 ISO, 含 offset)
    - pinned: 使用者保護旗標 (true → curator 永遠跳過自動降級, hermes 抄)

    背向相容: 既有檔 YAML 缺欄位時, parse 用 sensible default.
    """

    type: MemoryType
    source: MemorySource
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agent: str = "agent-memory-core"
    status: str = "active"
    schema_version: int = 3  # bump: R7 加 4 欄 (lifecycle_state / mention_count / last_activity_at / pinned)
    tags: list[str] = field(default_factory=list)
    char_count: int = 0
    extras: dict[str, Any] = field(default_factory=dict)
    # V2 新欄位 (C1)
    ai_ready: bool = True
    etl_status: EtlStatus = EtlStatus.PROCESSING
    security_level: SecurityLevel = SecurityLevel.SAFE_DATA
    aliases: list[str] = field(default_factory=list)
    # Round 7 新欄位 (C16) — 預設 long + pinned False 是「最保守安全」選擇
    # daily_flush / session_log 寫入時 caller 自行明示 lifecycle_state=SHORT
    lifecycle_state: LifecycleState = LifecycleState.LONG
    mention_count: int = 0
    last_activity_at: str = ""  # 空字串表示從未被命中, 本機時區 ISO with offset (例: 2026-05-16T14:30:00+08:00)
    pinned: bool = False


@dataclass(slots=True)
class MemoryNote:
    """Memory note object."""

    path: str
    frontmatter: Frontmatter
    body: str
