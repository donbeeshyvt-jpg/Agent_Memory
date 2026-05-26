"""V3 C15 Multi-User Router + Attention Allocator.

對齊 V3 §18 Multi-User Router + §29.9 H9 Attention Allocator + §10.6 觀眾分層自動升降.

Phase 1 single-user pipeline 已通; Phase 2 加 multi-user 治理:
- Discord author_id 路由
- 公開/私聊分流
- rate limit (per user per session)
- Attention Allocator K=3 (D34-V3)
- 觀眾分層自動升降 (24h medium 全升降; in-stream 只升不降 D-V3-39)
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class IncomingMessage:
    """V3 multi-user 入口訊息標準化."""

    user_id: str
    message: str
    channel_type: str = "normal"
    channel_id: str = ""
    is_owner: bool = False
    timestamp: float = field(default_factory=time.time)
    # 內部評分 (allocator 用)
    intimacy: float = 0.0
    emotional_salience: float = 0.5
    goal_relevance: float = 0.5
    novelty: float = 0.5
    attention_score: float = 0.0


# ─── Attention Allocator (§29.9 H9) ─────────────────────────────────────
def compute_attention_score(msg: IncomingMessage) -> float:
    """V3 §29.9: attention_score = intimacy × emotional_salience × goal_relevance × novelty."""
    return msg.intimacy * msg.emotional_salience * msg.goal_relevance * msg.novelty


def allocate_attention(
    messages: list[IncomingMessage], *, top_k: int = 3, owner_priority: bool = True,
) -> tuple[list[IncomingMessage], list[IncomingMessage]]:
    """V3 §29.9 H9: 多訊息選 top-K 完整 process, 其他 backlog.

    Args:
        messages: 同 burst 內收到的訊息
        top_k: 完整 process N 條 (D34-V3 預設 3)
        owner_priority: owner 訊息永遠 top priority

    Returns: (selected, deferred)
    """
    # 算 attention_score
    for m in messages:
        m.attention_score = compute_attention_score(m)

    # 拆 owner vs 其他
    owner_msgs = [m for m in messages if m.is_owner] if owner_priority else []
    other_msgs = [m for m in messages if not m.is_owner] if owner_priority else messages

    # owner 永遠 top
    sorted_other = sorted(other_msgs, key=lambda m: m.attention_score, reverse=True)
    selected = owner_msgs + sorted_other[: max(0, top_k - len(owner_msgs))]
    deferred = sorted_other[max(0, top_k - len(owner_msgs)) :]
    return selected, deferred


# ─── 公開/私聊分流 (對齊 §10.5 + §17 Owner) ────────────────────────────
def classify_channel(channel_type: str, *, concurrent_viewers: int = 0) -> str:
    """V3 §18: 標準化 channel_type → routing tier."""
    if channel_type == "dm":
        return "dm"
    if channel_type == "cli":
        return "cli"
    if channel_type == "public_stream" or (channel_type.startswith("public") and concurrent_viewers >= 5):
        return "public_stream"
    if channel_type == "public_text_channel" or channel_type.startswith("public"):
        return "public_text_channel"
    return "normal"


# ─── Rate Limit (per user per session) ─────────────────────────────────
@dataclass(slots=True)
class RateLimitConfig:
    max_messages_per_minute: int = 20
    max_messages_per_hour: int = 200


class RateLimiter:
    """V3 §18: 簡易 in-memory rate limit (per user+session). Phase 3 可換 sliding window."""

    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()
        self._minute_buckets: dict[str, deque] = defaultdict(deque)
        self._hour_buckets: dict[str, deque] = defaultdict(deque)

    def _evict_old(self, queue: deque, max_age: float, now: float) -> None:
        while queue and (now - queue[0]) > max_age:
            queue.popleft()

    def allow(self, user_id: str, *, channel_id: str = "") -> tuple[bool, str]:
        """check + record. Returns (allowed, reason)."""
        key = f"{user_id}::{channel_id}"
        now = time.time()
        m = self._minute_buckets[key]
        h = self._hour_buckets[key]
        self._evict_old(m, 60.0, now)
        self._evict_old(h, 3600.0, now)
        if len(m) >= self.config.max_messages_per_minute:
            return False, f"rate_limit_minute ({len(m)} >= {self.config.max_messages_per_minute})"
        if len(h) >= self.config.max_messages_per_hour:
            return False, f"rate_limit_hour ({len(h)} >= {self.config.max_messages_per_hour})"
        m.append(now)
        h.append(now)
        return True, "ok"


# ─── 觀眾分層自動升降 (§10.6) ──────────────────────────────────────────
def auto_promote_viewer_tier(
    vault_root: Path, user_id: str,
    *, interaction_count: int, intimacy_score: float,
    in_stream_mode: bool = False,
) -> Optional[str]:
    """V3 §10.6: VIP/casual 自動升降. in_stream 只升不降 (D-V3-39).

    Returns: 'casual'→'vip' / 'vip'→'casual' / None
    """
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT role, loyalty_tier, is_banned FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        current_tier = row["loyalty_tier"] if row else "casual"
        is_banned = row["is_banned"] if row else 0

        if is_banned:
            return None  # banned 永遠不升

        # 升 VIP: interaction>=20 + intimacy>=0.4 + 7d 內有互動
        if interaction_count >= 20 and intimacy_score >= 0.4 and current_tier == "casual":
            conn.execute(
                "UPDATE users SET loyalty_tier='vip' WHERE user_id=?", (user_id,)
            )
            conn.commit()
            return "casual→vip"

        # 降級 (D-V3-39: in_stream 只升不降)
        if not in_stream_mode:
            if intimacy_score < 0.2 and current_tier == "vip":
                conn.execute(
                    "UPDATE users SET loyalty_tier='casual' WHERE user_id=?", (user_id,)
                )
                conn.commit()
                return "vip→casual"

        return None


def ensure_user_record(
    vault_root: Path, user_id: str, *,
    display_name: str = "", role: str = "audience",
) -> None:
    """V3 §18: 確保 users 表有此 user record."""
    now = datetime.now(timezone.utc).isoformat()
    with open_companion_db(vault_root) as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET last_seen_at=? WHERE user_id=?", (now, user_id)
            )
        else:
            conn.execute(
                "INSERT INTO users (user_id, display_name, role, loyalty_tier, is_banned, first_seen_at, last_seen_at) VALUES (?, ?, ?, 'casual', 0, ?, ?)",
                (user_id, display_name or user_id, role, now, now),
            )
        conn.commit()


def ban_user(vault_root: Path, user_id: str, *, reason: str = "") -> None:
    """V3 §10.5: banned tier."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "UPDATE users SET loyalty_tier='banned', is_banned=1 WHERE user_id=?",
            (user_id,),
        )
        conn.commit()
