"""V3 C7d Inner Monologue — §29 H1.

對齊 V3 §29.1 + D-V3-30: Phase 1 必上, 沒它夥伴像 chatbot 不像人.

dialog pipeline Step 14.5: LLM 出 response 前先生成短內心獨白 (Chain-of-Thought 風格).
部分隱藏 (reasoning), 部分公開 (對話思考過程外顯).

風格 by affect:
- calm + clarify needed → 結構化 ("等等讓我想想...")
- anxious → 跳躍 ("欸我有點亂...")
- playful → 戲謔 ("哦這讓我想到...")

balance.whimsy 高時 pre-utterance leak (隨機在 reply 內加思考片段).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from agent_memory.companion.affect_manager import AffectState
from agent_memory.companion.seven_emotions_balance import BalanceState, EmotionState


# V3-O.12 #G5 (2026-06-03): hardcoded template list 廢棄.
# user 觀察「固定句子很怪」+ 「哦這讓我想到」每 turn 重複→ phrase 死板.
# 改由 _render_final_generation_instruction 內加 instruction, 讓 LLM 根據當前
# affect/balance/emotion state 在 reply 內自然體現思考過渡語感, 取代 hardcoded list 抽取.
# 保留 _MONOLOGUE_TEMPLATES key 結構供 _pick_style 仍可 return 合法 style label,
# 但內容全空 → generate_inner_monologue 不再抽出 phrase, monologue_text/leak 永遠空.
_MONOLOGUE_TEMPLATES: dict[str, list[str]] = {
    "structured": [],
    "anxious": [],
    "playful": [],
    "curious": [],
    "warm": [],
}


@dataclass(slots=True)
class InnerMonologueResult:
    """V3 C7d Inner Monologue output."""

    monologue_text: str = ""  # 給 LLM 當 reasoning context (隱藏)
    pre_utterance_leak: str = ""  # 可選 — 對話開頭外顯 (None 時不外顯)
    style: str = "structured"
    used_template: bool = True  # Phase 1 用 template, Phase 3 才改 LLM


def _pick_style(
    affect: AffectState, emotion: EmotionState, balance: BalanceState, policy_strategy: str
) -> str:
    """Phase 1 規則: affect/emotion/balance/policy → monologue style."""
    if policy_strategy in ("clarify_before_answer", "task_decomposition"):
        return "structured"
    if affect.uncertainty > 0.6 or emotion.fear > 0.4:
        return "anxious"
    if balance.playfulness > 0.5 or balance.mischief > 0.4:
        return "playful"
    if balance.curiosity_urge > 0.5 or policy_strategy in ("curious_ask_back", "proactive_clarify"):
        return "curious"
    if emotion.sadness > 0.4 or emotion.love > 0.4 or affect.valence > 0.3:
        return "warm"
    return "structured"


def generate_inner_monologue(
    affect: AffectState,
    emotion: EmotionState,
    balance: BalanceState,
    *,
    policy_strategy: str = "calm_clear",
    policy_inner_monologue_visible: bool = False,
    rng: Optional[random.Random] = None,
) -> InnerMonologueResult:
    """V3 C7d: 生成內心獨白 (Phase 1 template-based, Phase 3 可換 LLM 生成).

    Args:
        affect / emotion / balance: 當前狀態
        policy_strategy: Policy Mapper 算出的 strategy
        policy_inner_monologue_visible: Policy 是否標明思考過程外顯
        rng: 注 deterministic 用 (e2e test)

    Returns InnerMonologueResult.
    """
    rng = rng or random.Random()
    style = _pick_style(affect, emotion, balance, policy_strategy)
    # V3-O.12 #G5: hardcoded template 廢棄, phrase 改由 main LLM 在 final_generation_instruction
    # 規範下自然生成. 此處只保留 style 算出 (給 trace / audit), monologue_text/leak 永遠空.
    template_pool = _MONOLOGUE_TEMPLATES.get(style, [])
    monologue = rng.choice(template_pool) if template_pool else ""
    leak = ""  # G5: 永不 inject hardcoded prefix

    return InnerMonologueResult(
        monologue_text=monologue,
        pre_utterance_leak=leak,
        style=style,
        used_template=bool(template_pool),
    )


def maybe_inject_into_response(
    response_text: str,
    monologue: InnerMonologueResult,
    *,
    inject: bool = False,
) -> str:
    """V3 C7d: 把 inner monologue pre-utterance leak 注進 response 開頭."""
    if not inject or not monologue.pre_utterance_leak:
        return response_text
    return f"{monologue.pre_utterance_leak} {response_text}"
