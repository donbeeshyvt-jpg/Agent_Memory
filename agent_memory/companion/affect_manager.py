"""V3 C6 Affect Manager — VAD + uncertainty 指數平滑.

對齊 V3 規劃書 §8.1 + §8.2 認知三層.
Phase 1 MVP: VAD 由 Appraisal 推算 → 指數平滑更新.

VAD (-1~1 / 0~1 / 0~1 / 0~1):
- Valence (-1~1): 情感極性, +1 開心 / -1 痛苦
- Arousal (0~1): 激活程度, 0 平靜 / 1 暴怒
- Dominance (0~1): 掌控感, 0 無力 / 1 主導
- uncertainty (0~1): 不確定性, 0 確定 / 1 茫然

Phase 1 不寫 emotion_state 表 (那由 seven_emotions_balance 寫),
本模組只算 VAD + 提供更新邏輯給 chat_runtime hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agent_memory.companion.appraisal_engine import AppraisalResult


@dataclass(slots=True)
class AffectState:
    """VAD + uncertainty 當前狀態. 預設中性 baseline."""

    valence: float = 0.0
    arousal: float = 0.3
    dominance: float = 0.5
    uncertainty: float = 0.3

    def as_dict(self) -> dict:
        return {
            "valence": self.valence,
            "arousal": self.arousal,
            "dominance": self.dominance,
            "uncertainty": self.uncertainty,
        }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def predict_vad_from_appraisal(appraisal: AppraisalResult) -> AffectState:
    """V3 C6: appraisal 7 維 → VAD 4 維推算 (rule-based mapping).

    對齊 V3 §8.2 七情工程映射 + §8.1 三層情緒模型:
    - 正向 goal_congruence + positive relationship_impact → valence ↑
    - 高 control + 高 certainty → dominance ↑, uncertainty ↓
    - 高 novelty / 低 norm_fit → arousal ↑
    - 低 certainty + 低 control → uncertainty ↑
    """
    # Valence (-1~1): 正負情感 (含情緒 keyword bias)
    valence = _clamp(
        appraisal.goal_congruence * 0.35
        + appraisal.relationship_impact * 0.25
        + (appraisal.norm_fit - 0.5) * 0.3  # norm_fit < 0.5 → valence 負
        + appraisal.emotion_valence_offset * 0.5,  # 情緒 keyword 主導
        -1.0, 1.0,
    )

    # Arousal (0~1): 激活
    arousal = _clamp(
        0.3  # baseline
        + appraisal.novelty * 0.2
        + (1.0 - appraisal.norm_fit) * 0.3  # 違規 → 高 arousal
        + abs(appraisal.relationship_impact) * 0.2  # 強人際感受 → 高 arousal
        + appraisal.emotion_arousal_offset * 0.4,  # 情緒激活詞
        0.0, 1.0,
    )

    # Dominance (0~1): 掌控
    dominance = _clamp(
        0.4  # baseline
        + appraisal.control * 0.3
        + appraisal.certainty * 0.3,
        0.0, 1.0,
    )

    # uncertainty (0~1): 不確定
    uncertainty = _clamp(
        0.3  # baseline
        + (1.0 - appraisal.certainty) * 0.4
        + (1.0 - appraisal.control) * 0.3,
        0.0, 1.0,
    )

    return AffectState(
        valence=valence,
        arousal=arousal,
        dominance=dominance,
        uncertainty=uncertainty,
    )


def update_affect_smoothed(
    current: AffectState,
    predicted: AffectState,
    *,
    alpha: float = 0.4,
) -> AffectState:
    """V3 C6: 指數平滑更新 affect — 避免單一訊息劇烈震盪.

    對齊 V3 §8.1 「指數平滑 (α=0.3-0.5)」+ §29.5 emotional aftermath.

    new = α × predicted + (1 - α) × current
    """
    alpha = _clamp(alpha, 0.0, 1.0)
    return AffectState(
        valence=alpha * predicted.valence + (1 - alpha) * current.valence,
        arousal=alpha * predicted.arousal + (1 - alpha) * current.arousal,
        dominance=alpha * predicted.dominance + (1 - alpha) * current.dominance,
        uncertainty=alpha * predicted.uncertainty + (1 - alpha) * current.uncertainty,
    )


def appraise_and_update_affect(
    message: str,
    current_affect: Optional[AffectState] = None,
    *,
    alpha: float = 0.4,
    known_entities: Optional[set[str]] = None,
) -> tuple[AppraisalResult, AffectState]:
    """V3 C6: 一站式 — appraisal + VAD 預測 + 平滑更新.

    Args:
        message: 觀眾訊息
        current_affect: 上一輪 affect 狀態 (None = baseline)
        alpha: 指數平滑係數 (0.3-0.5 默認 0.4)
        known_entities: 已知 entities (給 novelty 算)

    Returns:
        (appraisal, new_affect): 給 chat pipeline Step 4+5 用
    """
    from agent_memory.companion.appraisal_engine import appraise_message

    appraisal = appraise_message(message, known_entities=known_entities)
    predicted = predict_vad_from_appraisal(appraisal)
    if current_affect is None:
        current_affect = AffectState()  # baseline
    new_affect = update_affect_smoothed(current_affect, predicted, alpha=alpha)
    return appraisal, new_affect
