"""V3 C9b Verbal Tics Engine — §29.7 H7 自發口頭禪.

對齊 V3 §29.7 + D-V3-30 (Phase 1 必上, 主播 vibe 核心) + D32-V3 (上限 0.7).

真人口頭禪不是規則式套用每句, 是「情緒/疲勞/興奮自然浮現」.
本模組: tic 池 + trigger_condition (affect/balance/emotion 連動) + probability 觸發.

SOUL.md verbal_tics 區塊範例:
  - tic: "ㄜㄜㄜ"
    trigger: balance.playfulness>0.5, joy>0.6
    base_probability: 0.3
    cooldown_turns: 5
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.affect_manager import AffectState
from agent_memory.companion.companion_db import open_companion_db
from agent_memory.companion.seven_emotions_balance import BalanceState, EmotionState


# 上限對齊 D32-V3 (0.7) → V3-E1 Bug 13 user 觀察「tic 太頻繁」, 改 0.3 (對齊真實聊天頻率)
_GLOBAL_PROBABILITY_CAP = 0.3


@dataclass(slots=True)
class TicDefinition:
    """從 SOUL.md verbal_tics 區塊載入 (Phase 1 hardcoded default + future load)."""

    tic: str
    base_probability: float = 0.3
    cooldown_turns: int = 5
    # trigger_condition (Phase 1 用簡單 keyword field)
    trigger_balance_playfulness_min: float = 0.0
    trigger_balance_whimsy_min: float = 0.0
    trigger_emotion_joy_min: float = 0.0
    trigger_emotion_sadness_min: float = 0.0
    trigger_affect_uncertainty_min: float = 0.0
    trigger_affect_arousal_min: float = 0.0


# Phase 1 default tics (對應「主播風」persona, SOUL.md 可 override)
_DEFAULT_TICS: tuple[TicDefinition, ...] = (
    TicDefinition(tic="ㄜㄜㄜ", base_probability=0.3, cooldown_turns=5,
                  trigger_balance_playfulness_min=0.5, trigger_emotion_joy_min=0.6),
    TicDefinition(tic="哦哦哦", base_probability=0.25, cooldown_turns=5,
                  trigger_emotion_joy_min=0.5, trigger_affect_arousal_min=0.5),
    TicDefinition(tic="欸欸我想想", base_probability=0.35, cooldown_turns=8,
                  trigger_affect_uncertainty_min=0.5),
    TicDefinition(tic="嗯哼", base_probability=0.2, cooldown_turns=3,
                  trigger_emotion_joy_min=0.5),
    TicDefinition(tic="啊啊啊不對", base_probability=0.4, cooldown_turns=8,
                  trigger_affect_uncertainty_min=0.5, trigger_affect_arousal_min=0.4),
)


@dataclass(slots=True)
class TicSelection:
    tic: Optional[str] = None
    probability_used: float = 0.0
    selected_definition_index: int = -1
    reason: str = ""


def _check_triggers(td: TicDefinition, affect: AffectState, emotion: EmotionState, balance: BalanceState) -> bool:
    """所有設定的 trigger 條件都滿足才算 trigger."""
    if balance.playfulness < td.trigger_balance_playfulness_min:
        return False
    if balance.whimsy < td.trigger_balance_whimsy_min:
        return False
    if emotion.joy < td.trigger_emotion_joy_min:
        return False
    if emotion.sadness < td.trigger_emotion_sadness_min:
        return False
    if affect.uncertainty < td.trigger_affect_uncertainty_min:
        return False
    if affect.arousal < td.trigger_affect_arousal_min:
        return False
    return True


def select_tic(
    affect: AffectState,
    emotion: EmotionState,
    balance: BalanceState,
    *,
    tics: Optional[tuple[TicDefinition, ...]] = None,
    policy_multiplier: float = 1.0,
    recent_tics_in_cooldown: Optional[set[str]] = None,
    rng: Optional[random.Random] = None,
) -> TicSelection:
    """V3 C9b: 從 tic 池選最高機率觸發的, 加 policy multiplier, 過 cooldown.

    Args:
        policy_multiplier: PolicyResult.verbal_tic_inject_probability_multiplier (1.0~1.3)
        recent_tics_in_cooldown: 最近 N turn 已用過的 tic strings (skip)
        rng: deterministic test 用

    Returns: TicSelection (tic=None 表沒觸發任何)
    """
    rng = rng or random.Random()
    tics = tics or _DEFAULT_TICS
    recent_tics_in_cooldown = recent_tics_in_cooldown or set()

    candidates: list[tuple[int, TicDefinition, float]] = []
    for i, td in enumerate(tics):
        if td.tic in recent_tics_in_cooldown:
            continue
        if not _check_triggers(td, affect, emotion, balance):
            continue
        # final probability = base * multiplier, capped to _GLOBAL_PROBABILITY_CAP (D32-V3)
        prob = min(_GLOBAL_PROBABILITY_CAP, td.base_probability * policy_multiplier)
        candidates.append((i, td, prob))

    if not candidates:
        return TicSelection(tic=None, reason="no candidate triggered")

    # 取機率最高的當主 candidate, sample roll
    candidates.sort(key=lambda c: c[2], reverse=True)
    idx, td, prob = candidates[0]
    roll = rng.random()
    if roll < prob:
        return TicSelection(
            tic=td.tic, probability_used=prob, selected_definition_index=idx,
            reason=f"triggered (prob={prob:.2f} roll={roll:.2f})",
        )
    return TicSelection(
        tic=None, probability_used=prob, selected_definition_index=idx,
        reason=f"probability miss (prob={prob:.2f} roll={roll:.2f})",
    )


def record_tic_usage(
    vault_root: Path,
    selection: TicSelection,
    *,
    session_id: str = "",
    user_id: str = "",
    trigger_condition: str = "",
) -> None:
    """V3 C9b: 寫 verbal_tics_history 表 (供 cooldown 計算 + audit)."""
    if not selection.tic:
        return
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT INTO verbal_tics_history (tic_event_id, session_id, user_id, tic, trigger_condition, actual_probability, used, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), session_id, user_id, selection.tic, trigger_condition, selection.probability_used, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_recent_tics_in_cooldown(
    vault_root: Path, session_id: str, *, last_n_turns: int = 5
) -> set[str]:
    """V3 C9b: 抓近 N turn 已用過的 tic (給 cooldown 用)."""
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT DISTINCT tic FROM verbal_tics_history WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, last_n_turns),
        ).fetchall()
    return {r["tic"] for r in rows if r["tic"]}


def maybe_inject_tic_into_response(response_text: str, tic: Optional[str]) -> str:
    """V3 C9b: 把 tic 注進 response 開頭 (簡單前綴)."""
    if not tic:
        return response_text
    return f"{tic} {response_text}"
