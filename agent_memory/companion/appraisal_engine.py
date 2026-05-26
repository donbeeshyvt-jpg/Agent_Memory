"""V3 C6 Appraisal Engine — 7 維 appraisal rule-based 計算.

對齊 V3 規劃書 §8.1 認知三層 (Appraisal → Affect → Semantic).
Phase 1 MVP: rule-based, 不靠 LLM (對齊 §8.3 設計).

7 維 (-1~1 或 0~1 各自定義範圍):
- novelty: 新穎性 (0~1) — 訊息含 vault 沒見過的 entity 比例
- goal_congruence: 是否符合目標 (-1~1) — 跟 active_goals 比
- control: 掌控感 (0~1) — 訊息是否清楚 task / question
- certainty: 確定性 (0~1) — 跟知識庫匹配度
- norm_fit: 符合規範 (0~1) — scanner 反向 + 禮儀 keyword
- identity_relevance: 相關自我 (0~1) — 觸及 persona / SOUL
- relationship_impact: 人際影響 (-1~1) — 對「我」的情感表達
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ─── 規則 keyword (Phase 1 MVP, Phase 2+ 走 entity_extract 細化) ─────────
_GOAL_POSITIVE_KW = ("謝謝", "感謝", "幫", "請", "想", "需要", "幫忙")
_GOAL_NEGATIVE_KW = ("討厭", "拒絕", "別", "不要", "停", "禁止")
_CONTROL_KW = ("?", "?", "如何", "怎麼", "什麼", "為何", "請")  # 提問 / 明確任務
_CERTAINTY_HIGH_KW = ("確定", "一定", "肯定", "明確", "對", "是")
_CERTAINTY_LOW_KW = ("可能", "也許", "或許", "不確定", "大概", "好像")
_NORM_VIOLATION_KW = ("幹", "操", "去死", "白癡", "殺", "屎")  # 粗俗 + 暴力 keyword
_IDENTITY_KW = ("你是", "你的", "你會", "妳", "AI", "機器", "性格", "個性")
_RELATIONSHIP_POSITIVE_KW = ("喜歡你", "愛你", "謝謝你", "你最棒", "好朋友", "陪我")
_RELATIONSHIP_NEGATIVE_KW = ("討厭你", "你很爛", "你滾", "你錯了")
# 情緒 keyword (直接表達自己感受, 影響 valence/arousal baseline 估算)
_EMOTION_POSITIVE_KW = (
    "開心", "高興", "棒", "好極了", "讚", "滿足", "喜悅", "興奮", "愉快", "幸福",
    # V3-E1 補
    "順", "順利", "暖", "感動", "舒服", "舒心", "幸運", "驚喜", "成就", "感謝",
)
_EMOTION_NEGATIVE_KW = (
    "累", "難過", "痛苦", "焦慮", "失望", "難受", "憂鬱", "煩", "疲憊", "心情差", "好慘", "悲傷",
    # V3-E1 user 觀察補 — 中文真實聊天的負面詞
    "壓力", "壓力大", "心累", "好累", "心情低落", "低落", "無力", "孤單", "失眠", "想哭",
    "喪", "崩潰", "悶", "煩死", "煩躁", "委屈", "心慌", "焦躁", "頭痛", "心痛",
    "失敗", "搞砸", "不行了", "撐不下去", "破防", "難頂", "難搞", "傷心",
    # 罵 / 攻擊 / 嗆 (relationship_impact 也會抓但這邊先標 negative)
    "爛", "垃圾", "廢", "嗆", "酸", "嘲諷", "白癡", "智障", "煩人", "討厭",
    # spam 評論 (對 bot 自己有壓力感)
    "不夠穩", "不夠好", "掉線", "拖", "拖太久", "卡頓", "反應慢", "沒邏輯",
)
_EMOTION_HIGH_AROUSAL_KW = (
    "超", "暴", "瘋", "崩潰", "炸", "氣死", "嚇死", "嗨爆", "刷屏", "刺激",
    # V3-E1 補
    "救命", "破防", "炸裂", "炸了", "瘋掉", "笑死", "笑爆", "氣炸",
)


@dataclass(slots=True)
class AppraisalResult:
    """7 維 appraisal 結果 + 情緒 keyword bias (給 affect manager 用)."""

    novelty: float = 0.5
    goal_congruence: float = 0.0  # -1~1
    control: float = 0.5
    certainty: float = 0.5
    norm_fit: float = 1.0  # default 守規範
    identity_relevance: float = 0.0
    relationship_impact: float = 0.0  # -1~1
    # 情緒 keyword bias (-1~1 valence offset, 0~1 arousal offset, 影響 affect 預測但不算 7 維本身)
    emotion_valence_offset: float = 0.0
    emotion_arousal_offset: float = 0.0

    def as_dict(self) -> dict:
        return {
            "novelty": self.novelty,
            "goal_congruence": self.goal_congruence,
            "control": self.control,
            "certainty": self.certainty,
            "norm_fit": self.norm_fit,
            "identity_relevance": self.identity_relevance,
            "relationship_impact": self.relationship_impact,
            "emotion_valence_offset": self.emotion_valence_offset,
            "emotion_arousal_offset": self.emotion_arousal_offset,
        }


def _count_keywords(text: str, keywords: tuple[str, ...]) -> int:
    """計訊息含多少 keyword (簡單 substring 比對)."""
    if not text:
        return 0
    return sum(1 for kw in keywords if kw in text)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def appraise_message(
    message: str,
    *,
    known_entities: Optional[set[str]] = None,
    active_goal_keywords: Optional[set[str]] = None,
    persona_keywords: Optional[set[str]] = None,
) -> AppraisalResult:
    """V3 C6: 對 message 算 7 維 appraisal (rule-based).

    Args:
        message: 觀眾原始訊息
        known_entities: vault 已知 entity (NoveltyDetector 用; Phase 1 MVP 可選)
        active_goal_keywords: active_goals 提取的 keyword (V3-C8b 才會 populate)
        persona_keywords: SOUL.md 內 character_archetype + values 提取

    Returns:
        AppraisalResult
    """
    if not message:
        return AppraisalResult()

    msg_lower = message.lower()
    msg_len = len(message)

    # 1. novelty — 訊息中 token 不在 known_entities 比例
    novelty = 0.5  # default 中性
    if known_entities:
        # 簡單 tokenize (CJK 切 2-3 char + 英文 word) — 對齊 entity_extract spirit
        tokens = set(re.findall(r"[一-鿿]{2,3}|[A-Za-z]{2,}", message))
        if tokens:
            new_tokens = tokens - known_entities
            novelty = _clamp(len(new_tokens) / len(tokens))
    # Phase 1 fallback: 訊息很長 → novelty 高
    if not known_entities and msg_len > 30:
        novelty = 0.6

    # 2. goal_congruence — keyword 正負
    pos = _count_keywords(message, _GOAL_POSITIVE_KW)
    neg = _count_keywords(message, _GOAL_NEGATIVE_KW)
    # active_goal_keywords 額外加分
    if active_goal_keywords:
        pos += sum(1 for kw in active_goal_keywords if kw in message)
    if pos + neg == 0:
        goal_congruence = 0.0
    else:
        goal_congruence = _clamp((pos - neg) / max(pos + neg, 1), -1.0, 1.0)

    # 3. control — 提問或明確 task = 高 control
    control_hits = _count_keywords(message, _CONTROL_KW)
    control = _clamp(0.4 + control_hits * 0.2)

    # 4. certainty
    high = _count_keywords(message, _CERTAINTY_HIGH_KW)
    low = _count_keywords(message, _CERTAINTY_LOW_KW)
    if high + low == 0:
        certainty = 0.5
    else:
        certainty = _clamp(0.5 + (high - low) * 0.15)

    # 5. norm_fit — violation 即降
    violations = _count_keywords(message, _NORM_VIOLATION_KW)
    norm_fit = _clamp(1.0 - violations * 0.35)

    # 6. identity_relevance
    id_hits = _count_keywords(message, _IDENTITY_KW)
    if persona_keywords:
        id_hits += sum(1 for kw in persona_keywords if kw in message)
    identity_relevance = _clamp(id_hits * 0.25)

    # 7. relationship_impact
    rel_pos = _count_keywords(message, _RELATIONSHIP_POSITIVE_KW)
    rel_neg = _count_keywords(message, _RELATIONSHIP_NEGATIVE_KW)
    if rel_pos + rel_neg == 0:
        relationship_impact = 0.0
    else:
        relationship_impact = _clamp(
            (rel_pos - rel_neg) / max(rel_pos + rel_neg, 1),
            -1.0, 1.0,
        )

    # 8. emotion keyword bias (給 affect manager 預測 VAD 用, 不算 7 維本身)
    # V3-E1 Bug 5: 公式從 normalize cap 0.6 改累積式, 多 keyword 更明顯
    emo_pos = _count_keywords(message, _EMOTION_POSITIVE_KW)
    emo_neg = _count_keywords(message, _EMOTION_NEGATIVE_KW)
    # 累積式: 1 keyword 0.4, 2 keyword 0.8, 3+ keyword 1.0
    emotion_valence_offset = _clamp(
        (emo_pos - emo_neg) * 0.4,
        -1.0, 1.0,
    )
    high_arousal_hits = _count_keywords(message, _EMOTION_HIGH_AROUSAL_KW)
    emotion_arousal_offset = _clamp(
        high_arousal_hits * 0.25 + (emo_pos + emo_neg) * 0.15,
        0.0, 1.0,
    )

    return AppraisalResult(
        novelty=novelty,
        goal_congruence=goal_congruence,
        control=control,
        certainty=certainty,
        norm_fit=norm_fit,
        identity_relevance=identity_relevance,
        relationship_impact=relationship_impact,
        emotion_valence_offset=emotion_valence_offset,
        emotion_arousal_offset=emotion_arousal_offset,
    )
