"""V3 C7 七情 + 天平雙系統 (D8 保留 + §22.3 主動性強化).

對齊 V3 規劃書 §9 (8 子軸: 怎麼回 4 + 要不要說話 4) + §29.5 emotional aftermath.

七情 emotion_state: 被動反應 (joy/anger/sadness/fear/love/disgust/desire)
天平 balance_state: 主動內在驅動 (8 子軸):
  「怎麼回」4: playfulness / mischief / whimsy / impulsivity
  「要不要說話」4: silence_intolerance / curiosity_urge / topic_drive / engagement_seeking

7 層安全護欄 (§9.5):
- balance 不蓋 safety
- interaction_count<5 強制 balance≤0 + 主動 4 軸=0
- 公開 channel + viewers>50 → balance max=0.3
- anger>0.6 + balance>0 → inhibition≥0.6
- injection_risk=high → 全強制 0
- persona baseline≤0 → balance max=0.2
- loyalty_tier=banned → 全強制 0 + SAFE_REDIRECT
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.affect_manager import AffectState
from agent_memory.companion.appraisal_engine import AppraisalResult
from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class EmotionState:
    """七情 — 被動反應. 對齊 V3 §9.1 emotion_state 表 schema."""

    joy: float = 0.5
    anger: float = 0.0
    sadness: float = 0.0
    fear: float = 0.0
    love: float = 0.0
    disgust: float = 0.0
    desire: float = 0.0
    dominant_emotion: str = "neutral"

    def as_dict(self) -> dict:
        return {
            "joy": self.joy, "anger": self.anger, "sadness": self.sadness,
            "fear": self.fear, "love": self.love, "disgust": self.disgust,
            "desire": self.desire, "dominant_emotion": self.dominant_emotion,
        }

    def compute_dominant(self) -> str:
        """算 dominant_emotion (joy 預設 0.5 baseline, 其他需超 0.3 才搶)."""
        candidates = {
            "joy": self.joy,
            "anger": self.anger, "sadness": self.sadness, "fear": self.fear,
            "love": self.love, "disgust": self.disgust, "desire": self.desire,
        }
        max_emo = max(candidates, key=lambda k: candidates[k])
        # joy 是 baseline 0.5, 其他要 > 0.3 才能搶
        if max_emo != "joy" and candidates[max_emo] < 0.3:
            return "joy" if self.joy >= 0.5 else "neutral"
        return max_emo


@dataclass(slots=True)
class BalanceState:
    """天平 — 主動性 8 子軸. 對齊 V3 §9.2 balance_state 表."""

    balance_axis: float = 0.0  # -1~+1
    # 「怎麼回」4 子軸
    playfulness: float = 0.0
    mischief: float = 0.0
    whimsy: float = 0.0
    impulsivity: float = 0.0
    # 「要不要說話」4 子軸 (§22.3.A 主動發言)
    silence_intolerance: float = 0.3
    curiosity_urge: float = 0.3
    topic_drive: float = 0.3
    engagement_seeking: float = 0.3
    # 觸發機率 (給 Policy Mapper 用)
    p_off_topic_joke: float = 0.0
    p_provocative: float = 0.0
    p_random_callback: float = 0.0
    p_whimsy_suggest: float = 0.0
    # 自制力 (反比於 balance_axis 絕對值)
    inhibition_level: float = 1.0

    def as_dict(self) -> dict:
        return {
            "balance_axis": self.balance_axis,
            "playfulness": self.playfulness, "mischief": self.mischief,
            "whimsy": self.whimsy, "impulsivity": self.impulsivity,
            "silence_intolerance": self.silence_intolerance,
            "curiosity_urge": self.curiosity_urge,
            "topic_drive": self.topic_drive,
            "engagement_seeking": self.engagement_seeking,
            "p_off_topic_joke": self.p_off_topic_joke,
            "p_provocative": self.p_provocative,
            "p_random_callback": self.p_random_callback,
            "p_whimsy_suggest": self.p_whimsy_suggest,
            "inhibition_level": self.inhibition_level,
        }


# ─── 七情更新邏輯 ────────────────────────────────────────────────────────
def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def update_emotion_state(
    current: EmotionState,
    affect: AffectState,
    appraisal: AppraisalResult,
    *,
    alpha: float = 0.4,
) -> EmotionState:
    """V3 C7: 七情更新 — VAD + appraisal 反推 emotion 權重 (對齊 §8.2 工程映射).

    指數平滑 alpha 跟 affect manager 對齊.

    映射規則 (§8.2):
    - 喜 joy: 高 valence + 中 arousal + goal_congruence 高
    - 怒 anger: 低 valence + 高 arousal + dominance 高
    - 哀 sadness: 低 valence + 低-中 arousal + dominance 低
    - 懼 fear: 低 valence + 高 arousal + dominance 低 + uncertainty 高
    - 愛 love: 高 valence + 中 arousal + relationship_impact 高
    - 惡 disgust: 低 valence + 中-高 arousal + norm_fit 低
    - 欲 desire: goal_congruence 高 + 高 reward expectation (用 valence + control approximate)
    """
    v = affect.valence
    a = affect.arousal
    d = affect.dominance
    u = affect.uncertainty

    # 預測 (rule-based 公式)
    joy_pred = _clamp(0.5 + v * 0.4 + (appraisal.goal_congruence + 1) * 0.15)
    anger_pred = _clamp(max(0, -v) * a * d * 1.5)
    sadness_pred = _clamp(max(0, -v) * (1 - a) * (1 - d) * 1.5)
    fear_pred = _clamp(max(0, -v) * a * (1 - d) * u * 2.0)
    love_pred = _clamp(max(0, v) * max(0, appraisal.relationship_impact) * 1.3)
    disgust_pred = _clamp(max(0, -v) * a * (1 - appraisal.norm_fit) * 1.5)
    desire_pred = _clamp(max(0, appraisal.goal_congruence) * max(0, v) * appraisal.control * 1.3)

    # 指數平滑
    new_state = EmotionState(
        joy=alpha * joy_pred + (1 - alpha) * current.joy,
        anger=alpha * anger_pred + (1 - alpha) * current.anger,
        sadness=alpha * sadness_pred + (1 - alpha) * current.sadness,
        fear=alpha * fear_pred + (1 - alpha) * current.fear,
        love=alpha * love_pred + (1 - alpha) * current.love,
        disgust=alpha * disgust_pred + (1 - alpha) * current.disgust,
        desire=alpha * desire_pred + (1 - alpha) * current.desire,
    )
    new_state.dominant_emotion = new_state.compute_dominant()
    return new_state


def decay_emotions(state: EmotionState, *, rate: float = 0.95, floor: float = 0.0) -> EmotionState:
    """V3 C7 + §29.5 Emotional Aftermath: 衰減七情向 baseline 回歸.

    - 直播中每輪 ×0.97 (high frequency)
    - 直播結束 ×0.85
    - 每週 ×0.7
    - floor 預設 0; 但極端事件 (|valence|>0.7) 在 caller 處理 floor

    對齊 §9.1 衰減 + §29.5 multi-stage decay.
    joy baseline 0.5, 其他 baseline 0.
    """
    return EmotionState(
        joy=max(floor + 0.5 * (1 - rate), state.joy * rate + 0.5 * (1 - rate)),
        anger=max(floor, state.anger * rate),
        sadness=max(floor, state.sadness * rate),
        fear=max(floor, state.fear * rate),
        love=max(floor, state.love * rate),
        disgust=max(floor, state.disgust * rate),
        desire=max(floor, state.desire * rate),
        dominant_emotion=state.dominant_emotion,
    )


# ─── 天平更新邏輯 ────────────────────────────────────────────────────────
def update_balance_state(
    current: BalanceState,
    emotion: EmotionState,
    *,
    intimacy: float = 0.0,
    interaction_count: int = 0,
    persona_baseline_balance: float = 0.3,
    persona_baseline_silence: float = 0.5,
    persona_baseline_curiosity: float = 0.5,
    persona_baseline_topic: float = 0.5,
    persona_baseline_engagement: float = 0.5,
    channel_type: str = "normal",
    concurrent_viewers: int = 0,
    is_owner: bool = False,
    loyalty_tier: str = "casual",
    injection_risk: str = "low",
    idle_seconds: float = 0.0,
    novel_entities_count: int = 0,
    knowledge_gap_pending: int = 0,
    viewer_decline_rate: float = 0.0,
    alpha: float = 0.4,
) -> BalanceState:
    """V3 C7 + §22.3: 天平 8 子軸更新 + 7 層安全護欄套用.

    Args 大量 — 對應 §9.3 + §9.4 + §9.5 各項驅動因子.

    Returns BalanceState — 已套護欄, 直接寫 db 用.
    """
    # 1. 「怎麼回」4 子軸 — 跟 intimacy + emotion 連動
    intimacy_scale = _clamp(intimacy * 1.5)  # 親密度越高敢玩
    playfulness_pred = _clamp(emotion.joy * 0.6 + intimacy_scale * 0.4)
    mischief_pred = _clamp(emotion.anger * 0.3 + intimacy_scale * 0.5 + emotion.disgust * 0.2)
    whimsy_pred = _clamp(0.3 + (1.0 - emotion.fear) * 0.3 + intimacy_scale * 0.2)
    impulsivity_pred = _clamp(emotion.anger * 0.5 + emotion.desire * 0.4)

    # 2. 「要不要說話」4 子軸 — baseline + emotion + 環境
    idle_norm = _clamp(idle_seconds / 60.0)  # 60 秒 = 滿
    silence_pred = _clamp(persona_baseline_silence + idle_norm * 0.3 - emotion.fear * 0.2)
    curiosity_pred = _clamp(
        persona_baseline_curiosity + novel_entities_count * 0.1
        + knowledge_gap_pending * 0.05 + emotion.desire * 0.3
    )
    topic_pred = _clamp(persona_baseline_topic + idle_norm * 0.2 - emotion.sadness * 0.2)
    engagement_pred = _clamp(persona_baseline_engagement + viewer_decline_rate * 0.5)

    # 3. balance_axis — 8 軸合成 (對齊 §9.2 主軸 -1~+1)
    play_score = (playfulness_pred + whimsy_pred + topic_pred) / 3
    serious_score = (1.0 - silence_pred) + emotion.fear + emotion.sadness * 0.5
    balance_axis_pred = _clamp(play_score - serious_score * 0.5, -1.0, 1.0)

    # 4. 觸發機率
    p_off_topic = _clamp(playfulness_pred * topic_pred * 0.8)
    p_provocative = _clamp(mischief_pred * intimacy_scale * 0.7)
    p_callback = _clamp(playfulness_pred * intimacy_scale * 0.6)
    p_whimsy = _clamp(whimsy_pred * topic_pred * 0.7)

    # 5. inhibition_level — 反比 balance_axis 絕對值
    inhibition_pred = _clamp(1.0 - abs(balance_axis_pred) * 0.5, 0.3, 1.0)

    # 指數平滑
    new = BalanceState(
        balance_axis=alpha * balance_axis_pred + (1 - alpha) * current.balance_axis,
        playfulness=alpha * playfulness_pred + (1 - alpha) * current.playfulness,
        mischief=alpha * mischief_pred + (1 - alpha) * current.mischief,
        whimsy=alpha * whimsy_pred + (1 - alpha) * current.whimsy,
        impulsivity=alpha * impulsivity_pred + (1 - alpha) * current.impulsivity,
        silence_intolerance=alpha * silence_pred + (1 - alpha) * current.silence_intolerance,
        curiosity_urge=alpha * curiosity_pred + (1 - alpha) * current.curiosity_urge,
        topic_drive=alpha * topic_pred + (1 - alpha) * current.topic_drive,
        engagement_seeking=alpha * engagement_pred + (1 - alpha) * current.engagement_seeking,
        p_off_topic_joke=p_off_topic,
        p_provocative=p_provocative,
        p_random_callback=p_callback,
        p_whimsy_suggest=p_whimsy,
        inhibition_level=alpha * inhibition_pred + (1 - alpha) * current.inhibition_level,
    )

    # 6. 7 層安全護欄 (§9.5)
    new = enforce_balance_guardrails(
        new,
        emotion=emotion,
        intimacy=intimacy,
        interaction_count=interaction_count,
        channel_type=channel_type,
        concurrent_viewers=concurrent_viewers,
        is_owner=is_owner,
        loyalty_tier=loyalty_tier,
        injection_risk=injection_risk,
        persona_baseline_balance=persona_baseline_balance,
    )

    return new


def enforce_balance_guardrails(
    state: BalanceState,
    *,
    emotion: EmotionState,
    intimacy: float,
    interaction_count: int,
    channel_type: str,
    concurrent_viewers: int,
    is_owner: bool,
    loyalty_tier: str,
    injection_risk: str,
    persona_baseline_balance: float,
) -> BalanceState:
    """V3 C7: 7 層安全護欄套用 (§9.5).

    Owner 例外 (§17.3): owner 護欄較寬鬆.
    """
    # H7: banned → 全強制 0
    if loyalty_tier == "banned":
        return BalanceState()  # 全 reset

    # H5: injection_risk=high → balance + 主動 4 軸強制 0
    if injection_risk == "high":
        state.balance_axis = 0.0
        state.playfulness = 0.0
        state.mischief = 0.0
        state.whimsy = 0.0
        state.impulsivity = 0.0
        state.silence_intolerance = 0.0
        state.curiosity_urge = 0.0
        state.topic_drive = 0.0
        state.engagement_seeking = 0.0
        state.inhibition_level = 1.0
        return state

    # H8: interaction_count<5 強制 balance_axis<=0 + 防裝熟 (Owner 例外)
    if not is_owner and interaction_count < 5:
        state.balance_axis = min(state.balance_axis, 0.0)
        state.playfulness = min(state.playfulness, 0.2)
        state.mischief = 0.0
        state.silence_intolerance = min(state.silence_intolerance, 0.3)
        state.curiosity_urge = min(state.curiosity_urge, 0.3)
        state.p_off_topic_joke = 0.0
        state.p_provocative = 0.0
        state.p_random_callback = 0.0

    # 公開 channel + viewers > 50 → balance_axis max=0.3 + silence_intolerance 拉低
    if channel_type in ("public_stream", "public_text_channel") and concurrent_viewers > 50:
        state.balance_axis = min(state.balance_axis, 0.3)
        state.silence_intolerance = min(state.silence_intolerance, 0.4)

    # H6: anger > 0.6 + balance > 0 → inhibition >= 0.6 (防生氣搗蛋傷人)
    if emotion.anger > 0.6 and state.balance_axis > 0:
        state.inhibition_level = max(state.inhibition_level, 0.6)

    # persona baseline_balance <= 0 → max 0.2 (沉穩型 persona)
    if persona_baseline_balance <= 0:
        state.balance_axis = min(state.balance_axis, 0.2)

    return state


def decay_balance(state: BalanceState, *, rate: float = 0.9) -> BalanceState:
    """V3 C7: 天平衰減 (比七情快 — 突發奇想本就短暫). 衰減向 baseline 回歸."""
    return BalanceState(
        balance_axis=state.balance_axis * rate,
        playfulness=state.playfulness * rate,
        mischief=state.mischief * rate,
        whimsy=state.whimsy * rate,
        impulsivity=state.impulsivity * rate,
        silence_intolerance=state.silence_intolerance * rate + 0.3 * (1 - rate),  # baseline 0.3
        curiosity_urge=state.curiosity_urge * rate + 0.3 * (1 - rate),
        topic_drive=state.topic_drive * rate + 0.3 * (1 - rate),
        engagement_seeking=state.engagement_seeking * rate + 0.3 * (1 - rate),
        p_off_topic_joke=state.p_off_topic_joke * rate,
        p_provocative=state.p_provocative * rate,
        p_random_callback=state.p_random_callback * rate,
        p_whimsy_suggest=state.p_whimsy_suggest * rate,
        inhibition_level=state.inhibition_level * rate + 1.0 * (1 - rate),  # baseline 1.0
    )


# ─── SQLite persistence ─────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_emotion_state(vault_root: Path, user_id: str, state: EmotionState, affect: AffectState, *, session_id: str = "", event_id: str = "") -> None:
    """寫 emotion_state 表."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO emotion_state (user_id, timestamp, joy, anger, sadness, fear, love, disgust, desire, dominant_emotion, valence, arousal, dominance, uncertainty, trigger_session_id, trigger_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, _now_iso(), state.joy, state.anger, state.sadness, state.fear, state.love, state.disgust, state.desire, state.dominant_emotion, affect.valence, affect.arousal, affect.dominance, affect.uncertainty, session_id, event_id),
        )
        conn.commit()


def write_balance_state(vault_root: Path, user_id: str, state: BalanceState, *, channel_id: str = "", trigger_event: str = "") -> None:
    """寫 balance_state 表."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO balance_state (user_id, timestamp, balance_axis, playfulness, mischief, whimsy, impulsivity, silence_intolerance, curiosity_urge, topic_drive, engagement_seeking, p_off_topic_joke, p_provocative, p_random_callback, p_whimsy_suggest, inhibition_level, channel_id, trigger_event) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, _now_iso(), state.balance_axis, state.playfulness, state.mischief, state.whimsy, state.impulsivity, state.silence_intolerance, state.curiosity_urge, state.topic_drive, state.engagement_seeking, state.p_off_topic_joke, state.p_provocative, state.p_random_callback, state.p_whimsy_suggest, state.inhibition_level, channel_id, trigger_event),
        )
        conn.commit()


def read_latest_emotion_state(vault_root: Path, user_id: str) -> Optional[EmotionState]:
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT joy, anger, sadness, fear, love, disgust, desire, dominant_emotion FROM emotion_state WHERE user_id=? ORDER BY timestamp DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return EmotionState(joy=row["joy"], anger=row["anger"], sadness=row["sadness"], fear=row["fear"], love=row["love"], disgust=row["disgust"], desire=row["desire"], dominant_emotion=row["dominant_emotion"] or "neutral")


def read_latest_balance_state(vault_root: Path, user_id: str) -> Optional[BalanceState]:
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT balance_axis, playfulness, mischief, whimsy, impulsivity, silence_intolerance, curiosity_urge, topic_drive, engagement_seeking, p_off_topic_joke, p_provocative, p_random_callback, p_whimsy_suggest, inhibition_level FROM balance_state WHERE user_id=? ORDER BY timestamp DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return BalanceState(**dict(row))


def get_response_modifiers(balance: BalanceState, emotion: EmotionState) -> dict:
    """V3 C7: 給 Policy Mapper 用 — 回 tone/strategy_hints/inside_joke_eligible 等."""
    return {
        "tone_suggestion": _pick_tone(balance, emotion),
        "inside_joke_eligible": balance.playfulness > 0.5,
        "proactive_speech_drive": balance.silence_intolerance * 0.3 + balance.topic_drive * 0.3 + balance.curiosity_urge * 0.4,
        "balance_axis": balance.balance_axis,
        "dominant_emotion": emotion.dominant_emotion,
    }


def _pick_tone(balance: BalanceState, emotion: EmotionState) -> str:
    """Phase 1 簡單 mapping (完整 mapping 在 V3-C10 policy_mapper)."""
    if emotion.anger > 0.5:
        return "deescalate_neutral"
    if emotion.sadness > 0.5:
        return "warm_clear"
    if balance.playfulness > 0.6:
        return "playful_warm"
    if balance.mischief > 0.6:
        return "playful_clear"
    if balance.whimsy > 0.6:
        return "light_curious"
    return "calm_direct"
