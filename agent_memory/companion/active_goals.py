"""V3 C8b Active Goals — §29.2 H2 跨 session 持續目標.

對齊 V3 §29.2 + D-V3-30 (Phase 1 必上).

active_goals 表持久化跨 session "我想做這件事" 目標:
- description + importance + target_audience
- last_pursued_at + pursuit_count → curator weekly 算 reminder
- 來源: owner_directive / self_proposed / observed_trait

Memory Router Layer 3 抓 active_goals 進 context → 主動發言可優先 callback.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class ActiveGoal:
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    source: str = "self_proposed"  # owner_directive / self_proposed / observed_trait
    importance: float = 0.5
    created_at: str = ""
    last_pursued_at: str = ""
    pursuit_count: int = 0
    target_audience: str = "all"
    status: str = "active"  # active / paused / completed / abandoned
    related_memory_ids: str = ""  # JSON list str

    def as_dict(self) -> dict:
        return {
            "goal_id": self.goal_id, "description": self.description,
            "source": self.source, "importance": self.importance,
            "created_at": self.created_at, "last_pursued_at": self.last_pursued_at,
            "pursuit_count": self.pursuit_count, "target_audience": self.target_audience,
            "status": self.status, "related_memory_ids": self.related_memory_ids,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_goal(
    vault_root: Path,
    description: str,
    *,
    source: str = "self_proposed",
    importance: float = 0.5,
    target_audience: str = "all",
) -> ActiveGoal:
    """V3 C8b: 加新 active goal. 來源 owner_directive / self_proposed / observed_trait."""
    goal = ActiveGoal(
        description=description,
        source=source,
        importance=importance,
        target_audience=target_audience,
        created_at=_now_iso(),
    )
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT INTO active_goals (goal_id, description, source, importance, created_at, last_pursued_at, pursuit_count, target_audience, status, related_memory_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (goal.goal_id, goal.description, goal.source, goal.importance, goal.created_at, goal.last_pursued_at, goal.pursuit_count, goal.target_audience, goal.status, goal.related_memory_ids),
        )
        conn.commit()
    return goal


def mark_pursued(vault_root: Path, goal_id: str) -> None:
    """V3 C8b: 標 goal 被推進過 (chat 內 mention 對應 goal description 就 call 此)."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "UPDATE active_goals SET last_pursued_at=?, pursuit_count=pursuit_count+1 WHERE goal_id=?",
            (_now_iso(), goal_id),
        )
        conn.commit()


def list_active_goals(vault_root: Path, *, target_audience: str = "") -> list[ActiveGoal]:
    """V3 C8b: 列 active goals (給 Memory Router Layer 3 用)."""
    query = "SELECT * FROM active_goals WHERE status='active'"
    params: tuple = ()
    if target_audience:
        query += " AND (target_audience=? OR target_audience='all')"
        params = (target_audience,)
    query += " ORDER BY importance DESC, created_at DESC"
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(query, params).fetchall()
    return [ActiveGoal(**dict(r)) for r in rows]


def update_status(vault_root: Path, goal_id: str, status: str) -> None:
    """V3 C8b: 改 goal status (paused / completed / abandoned)."""
    assert status in ("active", "paused", "completed", "abandoned")
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "UPDATE active_goals SET status=? WHERE goal_id=?",
            (status, goal_id),
        )
        conn.commit()
