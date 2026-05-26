"""V3 C9 Preference Tracker — Working / Episodic only (Phase 1 MVP).

對齊 V3 §10.2 Preference Memory Lifecycle 5 階段.

Phase 1 MVP **只入 Working / Episodic** (不升 Semantic 以上, 防注入操控人格).
Phase 3 V3-C20 preference_consolidator 才開 Semantic / Habit / Persona Candidate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class PreferenceCandidate:
    preference_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    preference_type: str = "topic"  # topic / tone / interaction_style / ...
    claim: str = ""
    scope: str = "general"  # general / session / specific
    strength: float = 0.3
    confidence: float = 0.3
    evidence_count: int = 1
    contradiction_count: int = 0
    derived_from: str = ""  # event_id reference
    status: str = "working"  # working / episodic / semantic / habit / persona
    first_seen_at: str = ""
    last_seen_at: str = ""

    def as_dict(self) -> dict:
        return {
            "preference_id": self.preference_id, "user_id": self.user_id,
            "preference_type": self.preference_type, "claim": self.claim,
            "scope": self.scope, "strength": self.strength, "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "contradiction_count": self.contradiction_count,
            "derived_from": self.derived_from, "status": self.status,
            "first_seen_at": self.first_seen_at, "last_seen_at": self.last_seen_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_or_reinforce(
    vault_root: Path,
    user_id: str,
    preference_type: str,
    claim: str,
    *,
    strength: float = 0.3,
    derived_from: str = "",
) -> PreferenceCandidate:
    """V3 C9: 加新 preference 或 reinforce 既有 (同 user + type + claim).

    Phase 1: evidence_count >= 3 自動升 episodic (不再升 semantic+).
    """
    now = _now_iso()
    with open_companion_db(vault_root) as conn:
        # 試找既有 (同 user + type + claim)
        existing = conn.execute(
            "SELECT * FROM preference_memories WHERE user_id=? AND preference_type=? AND claim=? LIMIT 1",
            (user_id, preference_type, claim),
        ).fetchone()
        if existing:
            new_evidence = existing["evidence_count"] + 1
            new_status = existing["status"]
            # Phase 1: working → episodic 升格 (evidence>=2-3)
            if new_status == "working" and new_evidence >= 2:
                new_status = "episodic"
            conn.execute(
                "UPDATE preference_memories SET evidence_count=?, last_seen_at=?, strength=?, status=? WHERE preference_id=?",
                (new_evidence, now, max(existing["strength"], strength), new_status, existing["preference_id"]),
            )
            conn.commit()
            existing_dict = dict(existing)
            existing_dict["evidence_count"] = new_evidence
            existing_dict["last_seen_at"] = now
            existing_dict["status"] = new_status
            existing_dict["strength"] = max(existing["strength"], strength)
            return PreferenceCandidate(**existing_dict)
        else:
            pref = PreferenceCandidate(
                user_id=user_id, preference_type=preference_type, claim=claim,
                strength=strength, derived_from=derived_from,
                first_seen_at=now, last_seen_at=now,
            )
            conn.execute(
                "INSERT INTO preference_memories (preference_id, user_id, preference_type, claim, scope, strength, confidence, evidence_count, contradiction_count, derived_from, status, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pref.preference_id, pref.user_id, pref.preference_type, pref.claim, pref.scope, pref.strength, pref.confidence, pref.evidence_count, pref.contradiction_count, pref.derived_from, pref.status, pref.first_seen_at, pref.last_seen_at),
            )
            conn.commit()
            return pref


def record_contradiction(vault_root: Path, preference_id: str) -> None:
    """V3 C9: 標 contradiction (對話內偵測 user 反向偏好)."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "UPDATE preference_memories SET contradiction_count=contradiction_count+1 WHERE preference_id=?",
            (preference_id,),
        )
        conn.commit()


def list_preferences(
    vault_root: Path, user_id: str,
    *, status: str = "", min_evidence: int = 1,
) -> list[PreferenceCandidate]:
    """V3 C9: 列 user 的 preferences (給 Memory Router / Decision Engine 用)."""
    query = "SELECT * FROM preference_memories WHERE user_id=? AND evidence_count>=?"
    params: list = [user_id, min_evidence]
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY confidence DESC, last_seen_at DESC"
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(query, params).fetchall()
    return [PreferenceCandidate(**dict(r)) for r in rows]
