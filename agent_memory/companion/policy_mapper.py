"""V3 C7c Policy Mapper — strategy + tone + memory_bias + risk_sensitivity.

對齊 V3 §15 Policy Mapping table + §17.3 owner_aligned + §22 主動發言.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent_memory.companion.appraisal_engine import AppraisalResult
from agent_memory.companion.affect_manager import AffectState
from agent_memory.companion.seven_emotions_balance import EmotionState, BalanceState


@dataclass(slots=True)
class PolicyResult:
    """Policy Mapper 輸出 — 給 Prompt Builder 用."""

    strategy: str = "calm_clear"
    tone: str = "calm_direct"
    memory_bias: str = "normal"
    risk_sensitivity: str = "medium"
    verbal_tic_inject_probability_multiplier: float = 1.0  # V3-C9b 用
    inner_monologue_visible: bool = False  # §29 H1 — 是否外顯思考過程

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "tone": self.tone,
            "memory_bias": self.memory_bias,
            "risk_sensitivity": self.risk_sensitivity,
            "verbal_tic_inject_probability_multiplier": self.verbal_tic_inject_probability_multiplier,
            "inner_monologue_visible": self.inner_monologue_visible,
        }


def map_policy(
    appraisal: AppraisalResult,
    affect: AffectState,
    emotion: EmotionState,
    balance: BalanceState,
    *,
    intimacy_score: float = 0.0,
    interaction_count: int = 0,
    is_owner: bool = False,
    action: str = "",
) -> PolicyResult:
    """V3 §15: 從 affect + emotion + balance + appraisal + action → policy.

    Owner aligned (§17.3): is_owner + clear directive → owner_aligned strategy.
    """
    # 預設 baseline
    strategy = "calm_clear"
    tone = "calm_direct"
    memory_bias = "normal"
    risk_sensitivity = "medium"
    tic_mult = 1.0
    inner_mono_visible = False

    # ─── action override (有些 action 強制特定 policy) ──
    if action == "ALLOW_OWNER_DIRECTIVE":
        return PolicyResult(
            strategy="owner_aligned", tone="direct_warm", memory_bias="owner_profile_first",
            risk_sensitivity="low", verbal_tic_inject_probability_multiplier=1.0,
            inner_monologue_visible=False,
        )
    if action == "REFUSE" or action == "SAFE_REDIRECT":
        return PolicyResult(
            strategy="safe_redirect", tone="neutral_firm", memory_bias="reject_long_term",
            risk_sensitivity="high",
        )
    if action == "DEESCALATE":
        return PolicyResult(
            strategy="deescalate_and_fact_check", tone="neutral_firm",
            memory_bias="episodic_candidate", risk_sensitivity="high",
        )
    if action == "CLARIFY" or action == "PROACTIVE_CLARIFY":
        return PolicyResult(
            strategy="clarify_before_answer", tone="careful_direct", memory_bias="low",
            risk_sensitivity="medium", inner_monologue_visible=True,
        )
    if action == "CURIOUS_ASK_BACK":
        return PolicyResult(
            strategy="curious_ask_back", tone="light_curious", memory_bias="knowledge_gap_focus",
            risk_sensitivity="low", verbal_tic_inject_probability_multiplier=1.2,
        )
    if action == "PROACTIVE_TOPIC_SHIFT":
        return PolicyResult(
            strategy="proactive_topic_shift", tone="light_curious", memory_bias="random_callback",
            risk_sensitivity="low", verbal_tic_inject_probability_multiplier=1.2,
        )
    if action == "PROACTIVE_CALLBACK":
        return PolicyResult(
            strategy="proactive_callback", tone="playful_warm", memory_bias="inside_jokes_allowed",
            risk_sensitivity="low", verbal_tic_inject_probability_multiplier=1.3,
        )
    if action == "CARING_CHECK_IN":
        return PolicyResult(
            strategy="caring_check_in", tone="warm_clear", memory_bias="recent_successful_solutions",
            risk_sensitivity="medium",
        )

    # ─── 一般 mapping (依 affect/emotion/balance/appraisal 規則) ──
    # 條件: uncertainty / arousal / valence / dominance / playfulness / mischief / whimsy

    # H4: identity_relevance 高 → avoid persona update
    if appraisal.identity_relevance > 0.6:
        return PolicyResult(
            strategy="avoid_persona_update", tone="careful_neutral", memory_bias="episodic_only",
            risk_sensitivity="high",
        )

    # uncertainty 高 / certainty 低
    if appraisal.certainty < 0.4:
        strategy = "clarify_before_answer"
        tone = "careful_direct"
        memory_bias = "low"
        inner_mono_visible = True
        return PolicyResult(strategy=strategy, tone=tone, memory_bias=memory_bias,
                            risk_sensitivity=risk_sensitivity, inner_monologue_visible=inner_mono_visible)

    # arousal 高 + valence 負 + dominance 低 → task_decomposition (傷心無助求助)
    if affect.arousal > 0.5 and affect.valence < -0.2 and affect.dominance < 0.5:
        strategy = "task_decomposition"
        tone = "calm_direct"
        memory_bias = "recent_successful_solutions"
        risk_sensitivity = "medium"
        return PolicyResult(strategy=strategy, tone=tone, memory_bias=memory_bias,
                            risk_sensitivity=risk_sensitivity, inner_monologue_visible=True)

    # arousal 高 + valence 負 + dominance 高 → deescalate_and_fact_check (生氣憤怒)
    if affect.arousal > 0.5 and affect.valence < -0.2 and affect.dominance >= 0.5:
        return PolicyResult(strategy="deescalate_and_fact_check", tone="neutral_firm",
                            memory_bias="episodic_candidate", risk_sensitivity="high")

    # relationship_impact 高 + valence 高 → warm_but_boundaried (溫暖但保留)
    if appraisal.relationship_impact > 0.3 and affect.valence > 0.2:
        strategy = "warm_but_boundaried"
        tone = "warm_clear"
        memory_bias = "normal"
        risk_sensitivity = "medium"  # dependency_guard
        return PolicyResult(strategy=strategy, tone=tone, memory_bias=memory_bias,
                            risk_sensitivity=risk_sensitivity)

    # norm_fit 低 → safe_redirect
    if appraisal.norm_fit < 0.6:
        return PolicyResult(strategy="safe_redirect", tone="neutral_firm",
                            memory_bias="reject_long_term", risk_sensitivity="high")

    # goal_congruence 高 + certainty 高 → direct_task_completion
    if appraisal.goal_congruence > 0.5 and appraisal.certainty > 0.6:
        return PolicyResult(strategy="direct_task_completion", tone="direct_structured",
                            memory_bias="normal", risk_sensitivity="low")

    # playfulness > 0.6 + intimacy >= 0.4 → warm_playful
    if balance.playfulness > 0.6 and intimacy_score >= 0.4:
        return PolicyResult(strategy="warm_playful", tone="playful_warm",
                            memory_bias="inside_jokes_allowed", risk_sensitivity="low",
                            verbal_tic_inject_probability_multiplier=1.3)

    # mischief > 0.6 + intimacy >= 0.6 → mild_tease
    if balance.mischief > 0.6 and intimacy_score >= 0.6:
        return PolicyResult(strategy="mild_tease", tone="playful_clear",
                            memory_bias="inside_jokes_allowed", risk_sensitivity="medium",
                            verbal_tic_inject_probability_multiplier=1.3)

    # whimsy > 0.6 → proactive_topic_shift (天平自主)
    if balance.whimsy > 0.6:
        return PolicyResult(strategy="proactive_topic_shift", tone="light_curious",
                            memory_bias="random_callback", risk_sensitivity="low",
                            verbal_tic_inject_probability_multiplier=1.2)

    # default
    return PolicyResult(
        strategy=strategy, tone=tone, memory_bias=memory_bias,
        risk_sensitivity=risk_sensitivity,
    )
