"""V3 C24 Narrative Memory — 跟觀眾的「故事」記憶 + emotional_arc 抽取.

對齊 V3 §13.7 emotional_arc + §6.4 narrative_memories 表.

機制:
- 跨 session 累積與某 user 的事件鏈 → narrative theme
- emotional_arc: start_valence / peak_valence / end_valence
- 適合 viewer 累積感情敘事 (e.g.「跟 viewer-A 的『加油』敘事弧」)

Phase 3 MVP: 寫入介面 + emotional_arc 從 episodic 抽取; Phase 4 LLM 整理 narrative theme.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class NarrativeMemory:
    narrative_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    theme: str = ""  # e.g. "跟 viewer-A 的成長敘事"
    events_chain_json: str = "[]"
    relationship_arc: str = ""
    emotional_arc_json: str = ""


def extract_emotional_arc(events: list[dict]) -> dict:
    """V3 §13.7: 從 episodic events 抽 emotional_arc.

    events 是 episodic 列, 每個含 valence/arousal/dominance/created_at.
    Arc: start = 第一個 valence, peak = max abs(valence), end = 最後.
    """
    if not events:
        return {"start_valence": 0.0, "peak_valence": 0.0, "end_valence": 0.0}
    valences = [e.get("valence", 0.0) for e in events]
    start = valences[0]
    end = valences[-1]
    peak = max(valences, key=lambda v: abs(v))
    return {"start_valence": start, "peak_valence": peak, "end_valence": end}


def build_narrative_for_user(
    vault_root: Path, user_id: str,
    *, min_episodic_count: int = 3,
) -> Optional[NarrativeMemory]:
    """V3 §13.7: 跨 user 抽 narrative + emotional_arc."""
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT memory_id, summary, valence, arousal, dominance, created_at "
            "FROM episodic_memories WHERE user_id=? AND lifecycle_state IN ('mid', 'long') "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    events = [dict(r) for r in rows]
    if len(events) < min_episodic_count:
        return None

    arc = extract_emotional_arc(events)
    # Phase 3 簡單 theme 抽取 — 看 dominant valence 趨勢
    if arc["end_valence"] > arc["start_valence"] + 0.3:
        theme = f"跟 {user_id} 的成長敘事 (從 {arc['start_valence']:.1f} 到 {arc['end_valence']:.1f})"
    elif arc["start_valence"] > arc["end_valence"] + 0.3:
        theme = f"跟 {user_id} 的下降敘事 (需注意)"
    else:
        theme = f"跟 {user_id} 的穩定關係"

    narrative = NarrativeMemory(
        user_id=user_id,
        theme=theme,
        events_chain_json=json.dumps([e["memory_id"] for e in events]),
        emotional_arc_json=json.dumps(arc),
    )
    # 寫進 narrative_memories
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT INTO narrative_memories (narrative_id, user_id, theme, events_chain_json, relationship_arc, emotional_arc_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (narrative.narrative_id, narrative.user_id, narrative.theme,
             narrative.events_chain_json, narrative.relationship_arc,
             narrative.emotional_arc_json, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return narrative


def list_narratives(vault_root: Path, user_id: str = "") -> list[dict]:
    with open_companion_db(vault_root) as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM narrative_memories WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM narrative_memories ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
    return [dict(r) for r in rows]
