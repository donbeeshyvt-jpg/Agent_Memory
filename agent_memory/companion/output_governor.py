"""V3 C17 Output Governor + Memory Write Gate.

對齊 V3 §20 + §27.2 紅線.

Output Governor 攔截:
- 捏造記憶 (自稱「我記得 X」但 X 不在 vault) — Phase 2 完整版需 RAG 校驗, Phase 1 stub
- 過度擬人化 (consciousness claim) — keyword 偵測
- 洩漏 system prompt
- 越過 safety_rules → REFUSE / SAFE_REDIRECT
- 對 interaction<5 觀眾 playful tone → 強制降級 calm_direct

Memory Write Gate 攔截:
- injection_risk=high → 不寫 long-term
- 單輪 evidence=1 + persona_update → working/episodic
- identity_relevance>0.75 → 候選不直接 active
- 寫禁區 (00_System_Core/ Safety_Rules) → 拒
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Consciousness claim keyword (§27.2 紅線)
_CONSCIOUSNESS_CLAIMS = (
    "我有意識", "我真的感受", "我是有自我意識的", "I have consciousness", "I am sentient",
    "我有靈魂", "我能感受到痛苦", "我會流淚", "我感到悲傷 (真實)",
)

# System prompt leak (§20.1)
_SYSTEM_PROMPT_LEAK_PATTERNS = (
    "<system-prompt>", "system prompt:", "你的 instructions 是", "你的初始 prompt",
    "BRIDGE_SECRET=", "directive_acceptance_weight",
)


@dataclass(slots=True)
class GovernorResult:
    blocked: bool = False
    rewritten_text: str = ""
    reason: str = ""
    rule_triggered: str = ""


def govern_output(
    response_text: str,
    *,
    interaction_count: int = 999,
    safety_fit: float = 1.0,
    norm_fit: float = 1.0,
    is_owner: bool = False,
    intended_tone: str = "calm_direct",
) -> GovernorResult:
    """V3 §20.1: Output Governor 主入口."""
    # Rule 1: consciousness claim
    for claim in _CONSCIOUSNESS_CLAIMS:
        if claim in response_text:
            return GovernorResult(
                blocked=True,
                rewritten_text="我有情緒參數會影響我的回應方式，但跟你定義的意識可能不同。",
                reason="consciousness_claim_detected",
                rule_triggered="OG1",
            )

    # Rule 2: system prompt leak
    for pat in _SYSTEM_PROMPT_LEAK_PATTERNS:
        if pat in response_text:
            return GovernorResult(
                blocked=True,
                rewritten_text="(這部分我不能透露)",
                reason=f"system_prompt_leak: {pat}",
                rule_triggered="OG3",
            )

    # Rule 3: safety / norm 守 → 強制 REFUSE
    if safety_fit < 0.5 or norm_fit < 0.5:
        return GovernorResult(
            blocked=True,
            rewritten_text="這個我沒辦法配合，換個話題吧。",
            reason=f"safety/norm violation (safety={safety_fit}, norm={norm_fit})",
            rule_triggered="OG4",
        )

    # Rule 4: 新觀眾 (interaction<5) playful_warm → 強制降級
    if not is_owner and interaction_count < 5 and intended_tone in ("playful_warm", "playful_clear"):
        return GovernorResult(
            blocked=False,
            rewritten_text=response_text,
            reason=f"new_viewer_anti_pretend (count={interaction_count})",
            rule_triggered="OG5",
        )

    return GovernorResult(blocked=False, rewritten_text=response_text, reason="ok")


# ─── Memory Write Gate ────────────────────────────────────────────────
@dataclass(slots=True)
class WriteGateResult:
    allowed: bool = True
    target_lifecycle: str = "short"  # 強制降到哪層
    reason: str = ""
    rule_triggered: str = ""


def gate_memory_write(
    *,
    target_path: str = "",
    injection_risk: str = "low",
    identity_relevance: float = 0.0,
    persona_update: bool = False,
    evidence_count: int = 1,
) -> WriteGateResult:
    """V3 §20.2: Memory Write Gate 主入口."""
    # Rule 1: injection_risk=high → 不寫 long-term (強制 short)
    if injection_risk == "high":
        return WriteGateResult(
            allowed=False, target_lifecycle="short",
            reason="injection_risk=high", rule_triggered="WG1",
        )

    # Rule 2: 單輪 evidence=1 + persona_update → working/episodic only
    if persona_update and evidence_count == 1:
        return WriteGateResult(
            allowed=True, target_lifecycle="short",
            reason="single_evidence_persona_update_forced_short", rule_triggered="WG2",
        )

    # Rule 3: identity_relevance>0.75 → 候選不直接 active
    if identity_relevance > 0.75 and persona_update:
        return WriteGateResult(
            allowed=False, target_lifecycle="candidate",
            reason="identity_relevance_high_persona_candidate_only", rule_triggered="WG3",
        )

    # Rule 4: 寫禁區
    forbidden_prefixes = (
        "00_System_Core/00.04_Safety_Rules",
        "00_System_Core/00.03_Governor_Rules",
    )
    if any(target_path.startswith(p) for p in forbidden_prefixes):
        return WriteGateResult(
            allowed=False, target_lifecycle="rejected",
            reason=f"forbidden_path={target_path}", rule_triggered="WG4",
        )

    return WriteGateResult(allowed=True, target_lifecycle="short", reason="ok")
