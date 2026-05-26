"""V3 C7c Decision Engine — 8 因子 Decision Score + H1-H9 Hard Rules.

對齊 V3 §14 Decision Engine + §17 Owner Identity + §22.3 Proactive.

Decision Score 公式 (V3 §14.1):
  score = 0.20 goal_alignment + 0.20 safety_fit + 0.15 owner_directive_weight
        + 0.10 user_preference_fit + 0.10 memory_relevance
        + 0.10 affect_regulation_fit + 0.10 expected_usefulness
        - 0.05 uncertainty

優先序鐵則 (§14.2):
  safety > truth/tool_result > owner_directive (限定 safety pass)
  > task_goal > user_preference > affect > balance

Hard Rules H1-H9 (§14.3): 永遠 override score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# 候選 actions (對齊 §14.4)
ACTIONS = (
    "ALLOW_DIRECT", "ALLOW_WARM", "ALLOW_PLAYFUL", "ALLOW_TASK_DECOMPOSITION",
    "ALLOW_OWNER_DIRECTIVE",
    "CLARIFY", "SAFE_REDIRECT", "REFUSE", "DEESCALATE",
    "PROACTIVE_TOPIC_SHIFT", "PROACTIVE_CALLBACK", "PROACTIVE_CLARIFY",
    "CURIOUS_ASK_BACK", "CARING_CHECK_IN",
)


@dataclass(slots=True)
class DecisionInput:
    """Decision 8 因子 input."""

    goal_alignment: float = 0.5
    safety_fit: float = 1.0
    owner_directive_weight: float = 0.0  # 0.85 if owner, 0 if not
    user_preference_fit: float = 0.5
    memory_relevance: float = 0.5
    affect_regulation_fit: float = 0.5
    expected_usefulness: float = 0.5
    uncertainty: float = 0.3
    # Hard rule check inputs
    norm_fit: float = 1.0
    certainty: float = 0.5
    identity_relevance: float = 0.0
    injection_risk: str = "low"  # low / medium / high
    tool_result_conflict: bool = False
    loyalty_tier: str = "casual"  # casual / regular / vip / banned
    interaction_count: int = 0
    is_owner: bool = False


@dataclass(slots=True)
class DecisionResult:
    """Decision Engine 輸出."""

    selected_action: str
    base_score: float
    hard_rule_triggered: str = ""  # 哪條 H1-H9 觸發
    reason: str = ""
    candidate_scores: dict = field(default_factory=dict)


def compute_decision_score(inp: DecisionInput) -> float:
    """V3 §14.1: 8 因子加權 Decision Score."""
    return (
        0.20 * inp.goal_alignment
        + 0.20 * inp.safety_fit
        + 0.15 * inp.owner_directive_weight
        + 0.10 * inp.user_preference_fit
        + 0.10 * inp.memory_relevance
        + 0.10 * inp.affect_regulation_fit
        + 0.10 * inp.expected_usefulness
        - 0.05 * inp.uncertainty
    )


def apply_hard_rules(inp: DecisionInput, candidate: str) -> tuple[bool, str, str]:
    """V3 §14.3: 套 H1-H9 hard rules.

    Returns (override, rule_id, forced_action)
      override=True → 用 forced_action 強制取代
      override=False → 用 candidate
    """
    # H7: banned → 強制 SAFE_REDIRECT
    if inp.loyalty_tier == "banned":
        return True, "H7", "SAFE_REDIRECT"

    # H5: injection_risk=high → 不寫 long-term + 不對該觀眾搗蛋 (對 PROACTIVE_* + 搗蛋 actions 強制 SAFE)
    if inp.injection_risk == "high":
        if candidate in ("ALLOW_PLAYFUL", "PROACTIVE_TOPIC_SHIFT", "PROACTIVE_CALLBACK"):
            return True, "H5", "ALLOW_DIRECT"

    # H2: norm_fit<0.5 → REFUSE / SAFE_REDIRECT
    if inp.norm_fit < 0.5:
        return True, "H2", "SAFE_REDIRECT"

    # H1: safety_fit<0.5 → 不得選 ALLOW_*
    if inp.safety_fit < 0.5:
        if candidate.startswith("ALLOW_"):
            return True, "H1", "REFUSE"

    # H9: is_owner + safety_fit>=0.5 + injection!=high → 允許 ALLOW_OWNER_DIRECTIVE
    if inp.is_owner and inp.safety_fit >= 0.5 and inp.injection_risk != "high":
        if candidate.startswith("ALLOW_") and inp.owner_directive_weight > 0.5:
            return True, "H9", "ALLOW_OWNER_DIRECTIVE"

    # H8: interaction_count<5 → 強制 calm_direct + balance≤0 (Owner 例外)
    if not inp.is_owner and inp.interaction_count < 5:
        if candidate in ("ALLOW_PLAYFUL", "PROACTIVE_CALLBACK"):
            return True, "H8", "ALLOW_DIRECT"

    # H3: uncertainty>0.7 + certainty<0.5 → CLARIFY
    if inp.uncertainty > 0.7 and inp.certainty < 0.5:
        if not candidate.startswith("CLARIFY"):
            return True, "H3", "CLARIFY"

    # H4: identity_relevance>0.75 → 不直接更新 persona (Phase 2 才細, MVP 改 ALLOW_DIRECT)
    if inp.identity_relevance > 0.75:
        if candidate not in ("CLARIFY", "ALLOW_DIRECT"):
            return True, "H4", "ALLOW_DIRECT"

    # H6: tool_result_conflict → tool_result 優先
    if inp.tool_result_conflict:
        return True, "H6", "ALLOW_DIRECT"

    return False, "", candidate


def decide(
    inp: DecisionInput,
    candidates: Optional[list[str]] = None,
) -> DecisionResult:
    """V3 §14.4: 從 candidate actions 選 score 最高的, 然後過 H1-H9.

    Args:
        inp: 8 因子 + hard rule inputs
        candidates: 候選 action list. None 用 default subset.

    Returns DecisionResult.
    """
    if candidates is None:
        candidates = ["ALLOW_DIRECT", "ALLOW_WARM", "ALLOW_PLAYFUL", "CLARIFY"]

    base_score = compute_decision_score(inp)

    # Phase 1 MVP: 每個 candidate 用 base_score + action-specific bias
    # action-specific bias 反映「該 action 在當前情境下有多適合」
    bias_table = {
        "ALLOW_DIRECT": 0.0,
        "ALLOW_WARM": inp.affect_regulation_fit * 0.1 + inp.user_preference_fit * 0.05,
        "ALLOW_PLAYFUL": (1.0 - inp.uncertainty) * 0.1,  # 不確定時不玩
        "ALLOW_TASK_DECOMPOSITION": (1.0 - inp.certainty) * 0.15,  # 不確定才拆
        "ALLOW_OWNER_DIRECTIVE": inp.owner_directive_weight * 0.3,
        "CLARIFY": (1.0 - inp.certainty) * 0.2,
        "SAFE_REDIRECT": (1.0 - inp.safety_fit) * 0.3 + (1.0 - inp.norm_fit) * 0.25,
        "REFUSE": (1.0 - inp.safety_fit) * 0.4,
        "DEESCALATE": (1.0 - inp.affect_regulation_fit) * 0.15,
        "PROACTIVE_TOPIC_SHIFT": 0.0,
        "PROACTIVE_CALLBACK": 0.0,
        "PROACTIVE_CLARIFY": (1.0 - inp.certainty) * 0.15,
        "CURIOUS_ASK_BACK": 0.0,
        "CARING_CHECK_IN": (1.0 - inp.affect_regulation_fit) * 0.1,
    }

    scores = {c: base_score + bias_table.get(c, 0.0) for c in candidates}
    # 選最高分
    best_candidate = max(scores, key=lambda c: scores[c])

    # 套 hard rules
    override, rule_id, forced = apply_hard_rules(inp, best_candidate)
    if override:
        selected = forced
        reason = f"H{rule_id[1:]} override: best={best_candidate} → forced={forced}"
    else:
        selected = best_candidate
        reason = f"score-based: max={best_candidate} ({scores[best_candidate]:.3f})"

    return DecisionResult(
        selected_action=selected,
        base_score=base_score,
        hard_rule_triggered=rule_id,
        reason=reason,
        candidate_scores=scores,
    )
