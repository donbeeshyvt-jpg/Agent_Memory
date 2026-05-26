"""V3 C8 Intimacy State — 5 階段親密度 + 防裝熟.

對齊 V3 §10.4 親密度公式 + §10.5 觀眾治理 + D11-V3.

intimacy_score = 0.3 × normalize(interaction_count, cap=100)
               + 0.4 × normalize(emotional_resonance_density)
               + 0.3 × normalize(narrative_identification)

5 階段:
- 初識 < 0.2 (interaction<5)
- 熟悉 0.2-0.4 (>=5)
- 信任 0.4-0.6 (>=20)
- 親密 0.6-0.8 (>=50)
- 深度理解 > 0.8 (>=100)

衰減: 7d ×0.95 / 30d ×0.8 / 90d ×0.5 (有 floor 不歸初識)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


_STAGES = (
    (0.0, 0, "初識"),
    (0.2, 5, "熟悉"),
    (0.4, 20, "信任"),
    (0.6, 50, "親密"),
    (0.8, 100, "深度理解"),
)


@dataclass(slots=True)
class IntimacyState:
    user_id: str
    interaction_count: int = 0
    emotional_resonance_density: float = 0.0
    narrative_identification: float = 0.0
    intimacy_score: float = 0.0
    intimacy_stage: str = "初識"
    last_interaction_at: str = ""

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "interaction_count": self.interaction_count,
            "emotional_resonance_density": self.emotional_resonance_density,
            "narrative_identification": self.narrative_identification,
            "intimacy_score": self.intimacy_score,
            "intimacy_stage": self.intimacy_stage,
            "last_interaction_at": self.last_interaction_at,
        }


def _normalize(value: float, cap: float) -> float:
    return max(0.0, min(1.0, value / cap))


def compute_intimacy(state: IntimacyState) -> tuple[float, str]:
    """V3 §10.4 公式 + 5 階段判定. Returns (score, stage)."""
    score = (
        0.3 * _normalize(state.interaction_count, 100)
        + 0.4 * _normalize(state.emotional_resonance_density, 1.0)
        + 0.3 * _normalize(state.narrative_identification, 1.0)
    )
    # 5 階段 lookup (對齊 §10.4 雙條件 — score AND interaction_count 都達到才升級)
    stage = "初識"
    for threshold, ic_min, name in _STAGES:
        if score >= threshold and state.interaction_count >= ic_min:
            stage = name
    return score, stage


def update_intimacy_on_interaction(
    state: IntimacyState,
    *,
    valence: float = 0.0,
    arousal: float = 0.3,
    intent_match: bool = False,
    is_owner: bool = False,
    iso_now: Optional[str] = None,
) -> IntimacyState:
    """V3 C8: 一次互動後更新 intimacy.

    valence/arousal 高 → emotional_resonance_density ↑
    intent_match (對話 callback 命中) → narrative_identification ↑
    """
    iso_now = iso_now or datetime.now(timezone.utc).isoformat()
    state.interaction_count += 1
    # 高情緒密度 → resonance
    emotional_contrib = abs(valence) * arousal * 0.01
    state.emotional_resonance_density = min(1.0, state.emotional_resonance_density + emotional_contrib)
    if intent_match:
        state.narrative_identification = min(1.0, state.narrative_identification + 0.02)
    state.last_interaction_at = iso_now
    state.intimacy_score, state.intimacy_stage = compute_intimacy(state)
    # Owner created_intimacy_baseline 0.8 直接親密 (D-V3-15)
    if is_owner and state.intimacy_score < 0.8:
        state.intimacy_score = max(state.intimacy_score, 0.8)
        state.intimacy_stage = "親密"
    return state


def decay_intimacy(state: IntimacyState, *, days_idle: int = 0, floor: float = 0.05) -> IntimacyState:
    """V3 §10.4 衰減. 7d ×0.95 / 30d ×0.8 / 90d ×0.5. floor 不歸初識."""
    if days_idle <= 0:
        return state
    if days_idle >= 90:
        rate = 0.5
    elif days_idle >= 30:
        rate = 0.8
    elif days_idle >= 7:
        rate = 0.95
    else:
        rate = 1.0
    state.emotional_resonance_density = max(floor, state.emotional_resonance_density * rate)
    state.narrative_identification = max(floor, state.narrative_identification * rate)
    state.intimacy_score, state.intimacy_stage = compute_intimacy(state)
    return state


def write_intimacy(vault_root: Path, state: IntimacyState) -> None:
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO intimacy_states (user_id, interaction_count, emotional_resonance_density, narrative_identification, intimacy_score, intimacy_stage, last_interaction_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state.user_id, state.interaction_count, state.emotional_resonance_density, state.narrative_identification, state.intimacy_score, state.intimacy_stage, state.last_interaction_at),
        )
        conn.commit()


def read_intimacy(vault_root: Path, user_id: str) -> Optional[IntimacyState]:
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT user_id, interaction_count, emotional_resonance_density, narrative_identification, intimacy_score, intimacy_stage, last_interaction_at FROM intimacy_states WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return IntimacyState(**dict(row))
