"""V3 C20 Preference Consolidator — Episodic → Semantic / Habit 升格.

對齊 V3 §10.2 5 階段 + Phase 3 開放 Semantic / Habit (Phase 1/2 只 Working/Episodic).

升格規則 (§10.2):
- Working (evidence=1) → Episodic (evidence=2-3, conf>=0.5)
- Episodic → Semantic (evidence>=3 跨 2 session, conf>=0.7, LLM 確認)
- Semantic → Habit (evidence>=7, 7d 穩定, conf>=0.8, drift guard 審)
- Habit → Persona Candidate (evidence>=15, 30d 穩定, conf>=0.9, 人工確認)

Phase 3 MVP: 純機械 evidence count + 時間判定; LLM 確認 stub.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


def consolidate_preferences(
    vault_root: Path,
    *,
    require_llm_confirm: bool = False,
) -> dict:
    """V3 §10.2 升格主流程. Returns 統計 dict."""
    promoted_to_semantic = 0
    promoted_to_habit = 0
    promoted_to_persona_candidate = 0
    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    with open_companion_db(vault_root) as conn:
        # episodic → semantic (evidence>=3 + first_seen<=7d ago)
        rows = conn.execute(
            "SELECT preference_id, user_id, preference_type AS topic, claim, evidence_count, first_seen_at, strength "
            "FROM preference_memories WHERE status='episodic' AND evidence_count>=3"
        ).fetchall()
        for r in rows:
            new_status = "semantic" if not require_llm_confirm else "semantic_candidate"
            conn.execute(
                "UPDATE preference_memories SET status=?, confidence=0.7 WHERE preference_id=?",
                (new_status, r["preference_id"]),
            )
            promoted_to_semantic += 1
            # ⭐ V3-H2 殘-03: 升 semantic 同步寫 markdown 進 60_Preference_Memory/{61_Owner,62_Viewer}/
            try:
                from agent_memory.companion.markdown_writers import write_preference_md
                # 判斷 is_owner (從 owner_state 表)
                owner_row = conn.execute(
                    "SELECT 1 FROM owner_state WHERE owner_user_id=?",
                    (r["user_id"],),
                ).fetchone()
                is_owner = bool(owner_row)
                write_preference_md(
                    vault_root,
                    topic=(r["topic"] or "未命名")[:60],
                    claim=(r["claim"] or "")[:300],
                    user_id=r["user_id"] or "anonymous",
                    is_owner=is_owner,
                    strength=float(r["strength"] or 0.5),
                    confidence=0.7,
                    evidence_count=int(r["evidence_count"] or 0),
                    status=new_status,
                )
            except Exception:
                pass  # non-critical 失敗不阻塞升格

        # semantic → habit (evidence>=7 + 7d 穩定 + first_seen <= 7d 前)
        rows = conn.execute(
            "SELECT preference_id, first_seen_at, evidence_count FROM preference_memories "
            "WHERE status='semantic' AND evidence_count>=7 AND first_seen_at<?",
            (cutoff_7d,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE preference_memories SET status='habit_candidate', confidence=0.8 WHERE preference_id=?",
                (r["preference_id"],),
            )
            promoted_to_habit += 1

        # habit → persona_candidate (evidence>=15 + 30d 穩定)
        rows = conn.execute(
            "SELECT preference_id FROM preference_memories "
            "WHERE status='habit_candidate' AND evidence_count>=15 AND first_seen_at<?",
            (cutoff_30d,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE preference_memories SET status='persona_candidate', confidence=0.9 WHERE preference_id=?",
                (r["preference_id"],),
            )
            promoted_to_persona_candidate += 1

        conn.commit()

    return {
        "promoted_to_semantic": promoted_to_semantic,
        "promoted_to_habit": promoted_to_habit,
        "promoted_to_persona_candidate": promoted_to_persona_candidate,
    }
