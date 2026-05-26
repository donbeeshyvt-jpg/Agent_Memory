"""V3 C21 Trait Evolution — 慢速性格變化.

對齊 V3 §22 + §10.5 evidence window + D-V3-12.

機制 (V3 §22):
- evidence>=7 篇 TPL_Emotion_Event → 提 trait_evolution candidate
- identity_relevance > 0.75 → 走 drift guard 嚴審
- Persona Candidate 必須人工確認 (中之人在 70_Persona_Versions/73_Candidates/ active)
- backup 上一版 (對應 hermes curator.backup.keep=5)

Phase 3 MVP: rule-based + LLM stub (Phase 4 真實 LLM 整合).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


_MIN_EVIDENCE_FOR_CANDIDATE = 7  # V3 §22 拍板
_DRIFT_THRESHOLD = 0.3  # current_value vs proposed_value 差 > 0.3 → 走 drift_guard


@dataclass(slots=True)
class TraitCandidate:
    user_id: str
    trait_name: str
    evidence_count: int = 0
    current_value: float = 0.0
    proposed_value: float = 0.0
    awaiting_drift_guard: bool = False
    events_json: str = ""


def add_trait_evidence(
    vault_root: Path,
    user_id: str,
    trait_name: str,
    *,
    observation_value: float,
    event_id: str = "",
) -> dict:
    """V3 §22: 加 trait evidence (對話內偵測到 trait 跡象).

    觀察值會跟 current_value 平均算 proposed_value;
    evidence>=7 → 標 awaiting_drift_guard.
    """
    now = datetime.now(timezone.utc).isoformat()
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT evidence_count, events_json, current_value, proposed_value FROM trait_evolution WHERE user_id=? AND trait_name=?",
            (user_id, trait_name),
        ).fetchone()

        if row is None:
            # 新 trait — current_value=0 (baseline 未 active), proposed_value=observation
            events = [event_id] if event_id else []
            conn.execute(
                "INSERT INTO trait_evolution (user_id, trait_name, evidence_count, events_json, current_value, proposed_value, awaiting_drift_guard, last_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, trait_name, 1, json.dumps(events), 0.0, observation_value, 0, now),
            )
            conn.commit()
            return {"action": "created", "evidence_count": 1}

        new_count = row["evidence_count"] + 1
        events = json.loads(row["events_json"] or "[]")
        if event_id:
            events.append(event_id)
        # proposed_value 動態平均
        prev_proposed = row["proposed_value"] or 0.0
        new_proposed = (prev_proposed * row["evidence_count"] + observation_value) / new_count
        # V3 §22: evidence>=7 → 提 candidate 等 drift_guard 評估 (不強制 drift>0.3)
        awaiting = new_count >= _MIN_EVIDENCE_FOR_CANDIDATE

        conn.execute(
            "UPDATE trait_evolution SET evidence_count=?, events_json=?, proposed_value=?, awaiting_drift_guard=?, last_updated_at=? WHERE user_id=? AND trait_name=?",
            (new_count, json.dumps(events), new_proposed, int(awaiting), now, user_id, trait_name),
        )
        conn.commit()
        return {
            "action": "candidate_proposed" if awaiting else "evidence_added",
            "evidence_count": new_count,
            "proposed_value": new_proposed,
            "awaiting_drift_guard": awaiting,
        }


def list_pending_candidates(vault_root: Path) -> list[dict]:
    """V3 §22: 列 awaiting_drift_guard 的 trait candidates (給 drift_guard / 中之人審)."""
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT user_id, trait_name, evidence_count, current_value, proposed_value, last_updated_at "
            "FROM trait_evolution WHERE awaiting_drift_guard=1 ORDER BY evidence_count DESC"
        ).fetchall()
    return [dict(r) for r in rows]
