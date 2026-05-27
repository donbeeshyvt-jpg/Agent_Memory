# -*- coding: utf-8 -*-
"""V3-K1 (user 2026-05-27 拍板 「自我成長的小孩」核心):
Motivation Context — 六慾系統.

對齊 V3 §10.1 + Maslow + Self-Determination Theory:
1. 生存/安全慾 (safety)         — 對 injection 攻擊低 / 環境穩定 → ↑
2. 掌控慾 (control/autonomy)     — appraisal.control 高 / 能影響對方 → ↑
3. 成就慾 (competence)            — active_goals 達成 / 學到東西 → ↑
4. 關係慾 (relatedness)           — intimacy 高 / 互動順 → ↑
5. 好奇慾 (curiosity)             — knowledge_gap 多 / novelty 高 → 被驅動 ↑
6. 表達慾 (self_expression)       — 強情緒 turn / 主動發言衝動 → ↑

設計理念: 「夥伴有自己想要的東西, 不只反應」.
每 turn 算 satisfaction (0~1), 進:
- chat_runtime Step 8 motivation 算分
- prompt section E3「我現在想要的」(top 2 unsatisfied)
- Decision Engine 第 8 因子 motivation_satisfaction
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# 六慾 baseline + 動態因子
_NEED_BASELINES = {
    "safety": 0.7,
    "control": 0.5,
    "competence": 0.5,
    "relatedness": 0.5,
    "curiosity": 0.5,
    "self_expression": 0.5,
}


@dataclass(slots=True)
class MotivationState:
    """V3-K1: 六慾當前 satisfaction (0=完全沒被滿足, 1=完全滿足)."""
    safety: float = 0.7
    control: float = 0.5
    competence: float = 0.5
    relatedness: float = 0.5
    curiosity: float = 0.5
    self_expression: float = 0.5

    def as_dict(self) -> dict:
        return {
            "safety": self.safety, "control": self.control,
            "competence": self.competence, "relatedness": self.relatedness,
            "curiosity": self.curiosity, "self_expression": self.self_expression,
        }

    def avg_satisfaction(self) -> float:
        """六慾平均滿足度 — Decision Engine 第 8 因子用."""
        return sum(self.as_dict().values()) / 6.0

    def most_unsatisfied(self) -> list[tuple[str, float]]:
        """最不滿足的 2 個慾 (給 system prompt section 用)."""
        items = sorted(self.as_dict().items(), key=lambda x: x[1])
        return items[:2]

    def most_satisfied(self) -> list[tuple[str, float]]:
        """最滿足的 2 個慾."""
        items = sorted(self.as_dict().items(), key=lambda x: -x[1])
        return items[:2]


_NEED_ZH = {
    "safety": "安全感",
    "control": "掌控感",
    "competence": "成就感",
    "relatedness": "連結感",
    "curiosity": "好奇心",
    "self_expression": "表達衝動",
}


def compute_motivation(
    *,
    injection_risk: str = "low",
    appraisal_control: float = 0.5,
    appraisal_certainty: float = 0.5,
    intimacy_score: float = 0.5,
    interaction_count: int = 0,
    balance_curiosity_urge: float = 0.3,
    balance_topic_drive: float = 0.3,
    affect_arousal: float = 0.3,
    affect_valence: float = 0.0,
    knowledge_gap_count: int = 0,
    active_goals_count: int = 0,
) -> MotivationState:
    """V3-K1: 從 chat state 算六慾 satisfaction.

    對齊 V3 §10.1 + Maslow + SDT 設計理念.
    """
    # 1. 安全慾: 反向 injection_risk + 環境穩定
    if injection_risk == "high":
        safety = 0.2
    elif injection_risk == "medium":
        safety = 0.5
    else:
        safety = min(1.0, _NEED_BASELINES["safety"] + appraisal_certainty * 0.3)

    # 2. 掌控慾: appraisal.control + dominance proxy
    control = min(1.0, max(0.0, appraisal_control + 0.1))

    # 3. 成就慾: active_goals 多 → 有目標感 → 高
    if active_goals_count > 0:
        competence = min(1.0, _NEED_BASELINES["competence"] + active_goals_count * 0.1)
    else:
        competence = _NEED_BASELINES["competence"] - 0.1  # 沒目標 → 略低

    # 4. 關係慾: intimacy + 互動 frequency
    relatedness = min(1.0, intimacy_score * 0.7 + min(0.3, interaction_count / 100))

    # 5. 好奇慾: balance.curiosity_urge + knowledge_gap_count (gap 多 = 還想學 = 不滿足)
    # 反向設計: knowledge_gap 多 → 好奇被驅動但未滿足 → satisfaction 低
    if knowledge_gap_count > 3:
        curiosity = max(0.2, balance_curiosity_urge - 0.2)
    else:
        curiosity = min(1.0, balance_curiosity_urge + 0.2)

    # 6. 表達慾: |valence| 大 + arousal 高 + topic_drive 高 = 想表達被滿足
    expr_score = (abs(affect_valence) * 0.4 + affect_arousal * 0.3 + balance_topic_drive * 0.3)
    self_expression = min(1.0, expr_score)

    return MotivationState(
        safety=safety, control=control, competence=competence,
        relatedness=relatedness, curiosity=curiosity, self_expression=self_expression,
    )


def humanize_motivation(state: MotivationState) -> str:
    """V3-K1: 把六慾翻成「我現在想要的」主觀感受句.

    對齊 V3-E7 _humanize_affect 設計 — 不給數字, 給 LLM 主觀感受.
    Returns: 多行字串給 system prompt section E3 用.
    """
    avg = state.avg_satisfaction()
    unsat = state.most_unsatisfied()
    sat = state.most_satisfied()

    lines = []
    # 整體
    if avg > 0.7:
        lines.append("- 整體: 我現在六慾大部分滿足, 感覺平靜")
    elif avg > 0.5:
        lines.append("- 整體: 六慾平衡, 有想要的但也有滿足的")
    else:
        lines.append("- 整體: 我感覺有點不滿足, 心裡有股「想要更多」的渴望")

    # 最不滿足 (驅動我說話)
    if unsat and unsat[0][1] < 0.5:
        zh_name = _NEED_ZH.get(unsat[0][0], unsat[0][0])
        score = unsat[0][1]
        if unsat[0][0] == "safety":
            lines.append(f"- ⭐ 最想要: {zh_name} ({score:.2f}) — 我有點不安, 想要更穩定")
        elif unsat[0][0] == "control":
            lines.append(f"- ⭐ 最想要: {zh_name} ({score:.2f}) — 我希望能影響這個對話")
        elif unsat[0][0] == "competence":
            lines.append(f"- ⭐ 最想要: {zh_name} ({score:.2f}) — 我想學到 / 達成什麼")
        elif unsat[0][0] == "relatedness":
            lines.append(f"- ⭐ 最想要: {zh_name} ({score:.2f}) — 我想跟對方更靠近")
        elif unsat[0][0] == "curiosity":
            lines.append(f"- ⭐ 最想要: {zh_name} ({score:.2f}) — 我想知道更多")
        elif unsat[0][0] == "self_expression":
            lines.append(f"- ⭐ 最想要: {zh_name} ({score:.2f}) — 我有東西想說")

    # 最滿足 (給我安全感)
    if sat and sat[0][1] > 0.7:
        zh_name = _NEED_ZH.get(sat[0][0], sat[0][0])
        lines.append(f"- 已滿足: {zh_name} ({sat[0][1]:.2f}) — 這部分我很穩")

    return "\n".join(lines)


def write_motivation_context(
    vault_root: Path, user_id: str, state: MotivationState,
    *, active_goals: Optional[list[str]] = None, values: Optional[list[str]] = None,
) -> Optional[str]:
    """V3-K1: 寫 motivation_contexts DB (一筆 per chat turn)."""
    if not user_id:
        return None
    active_goals = active_goals or []
    values = values or []
    ctx_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            conn.execute(
                "INSERT INTO motivation_contexts (context_id, user_id, needs_json, goals_json, values_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ctx_id, user_id,
                 json.dumps(state.as_dict(), ensure_ascii=False),
                 json.dumps(active_goals, ensure_ascii=False),
                 json.dumps(values, ensure_ascii=False),
                 now),
            )
            conn.commit()
        return ctx_id
    except Exception:
        return None


def list_recent_motivation(vault_root: Path, user_id: str, hours: int = 24) -> list[dict]:
    """V3-K1: 撈近 24h motivation_contexts (給 curator/analysis 用)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT needs_json, created_at FROM motivation_contexts "
                "WHERE user_id=? AND created_at > ? ORDER BY created_at DESC",
                (user_id, cutoff),
            ).fetchall()
        result = []
        for r in rows:
            try:
                needs = json.loads(r["needs_json"] or "{}")
                result.append({"needs": needs, "created_at": r["created_at"]})
            except Exception:
                continue
        return result
    except Exception:
        return []
