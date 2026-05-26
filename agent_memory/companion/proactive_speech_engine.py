"""V3 C12b Proactive Speech Engine — 4 Detector + 主動發言觸發.

對齊 V3 §16 主播感 + §22.3 主動發言 + §29.2 H2 + D-V3-18 KnowledgeGap 最優先.

4 個 Detector:
1. KnowledgeGapDetector ⭐ 最優先 (中之人多幫補知識核心循環)
2. AmbiguityDetector
3. NoveltyDetector
4. IncongruenceDetector (Phase 2 補)

Proactive Speech 觸發鏈:
  proactive_score = silence_intolerance × idle_norm
                  + topic_drive × 對話沉悶度
                  + curiosity_urge × (novel_entities + knowledge_gap_pending)
                  + engagement_seeking × viewer_decline_rate
                  + owner_present_bonus × is_owner_in_channel
                  - inhibition_level - recent_proactive_backoff

channel-aware threshold (D13-V3):
- public_stream: 0.4 (低門檻, 高觸發 30/場/5/觀眾)
- public_text_channel: 0.65 (中)
- dm 非 owner: 0.55 (3/天 / owner 不限)
- cli: 0.5
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# Channel-aware thresholds (D13-V3)
_CHANNEL_THRESHOLDS = {
    "public_stream": 0.4,
    "public_text_channel": 0.65,
    "dm": 0.55,
    "cli": 0.5,
    "normal": 0.5,
}

# Channel-aware 上限 (per session) — V3 §16.5
_CHANNEL_SESSION_LIMITS = {
    "public_stream": 30,
    "public_text_channel": 10,
    "dm": 3,
    "cli": 999,
    "normal": 20,
}


@dataclass(slots=True)
class DetectorResult:
    detector_name: str = ""
    triggered: bool = False
    score: float = 0.0
    payload: dict = field(default_factory=dict)


@dataclass(slots=True)
class ProactiveDecision:
    """主動發言評估結果."""

    should_speak: bool = False
    proactive_score: float = 0.0
    threshold_used: float = 0.5
    trigger_type: str = ""  # silence_fill / clarify / curiosity / callback / topic_shift / caring
    selected_action: str = ""  # PROACTIVE_TOPIC_SHIFT / CURIOUS_ASK_BACK / ...
    target_user_id: str = ""
    suggested_focus: str = ""  # KnowledgeGap entity / topic
    reason: str = ""


# ─── Detector 1: KnowledgeGapDetector (最優先) ──────────────────────────
def detect_knowledge_gap(
    message: str, *, certainty: float, known_entities: Optional[set[str]] = None,
) -> DetectorResult:
    """V3 §16.2 + §16.3: certainty<0.4 + message 含 entity → 寫 knowledge_gap_state."""
    if certainty >= 0.4:
        return DetectorResult(detector_name="KnowledgeGap", triggered=False, score=0.0)
    # tokenize CJK 2-3 + EN word
    tokens = set(re.findall(r"[一-鿿]{2,3}|[A-Za-z]{3,}", message))
    if not tokens:
        return DetectorResult(detector_name="KnowledgeGap", triggered=False)
    unknown = tokens - (known_entities or set())
    if not unknown:
        return DetectorResult(detector_name="KnowledgeGap", triggered=False)
    return DetectorResult(
        detector_name="KnowledgeGap", triggered=True,
        score=min(1.0, (1.0 - certainty) * len(unknown)),
        payload={"unknown_entities": sorted(unknown)[:5]},
    )


# ─── Detector 2: AmbiguityDetector ────────────────────────────────────
def detect_ambiguity(
    message: str, *, top_k_scores: Optional[list[float]] = None,
) -> DetectorResult:
    """V3 §16.2: avg < 0.4 OR top-k 分散 → ambiguity_score 高."""
    if not top_k_scores:
        # 沒 RAG 分數 → fallback: 短 query 含多個 entity 視為歧義
        tokens = re.findall(r"[一-鿿]{2,3}|[A-Za-z]{3,}", message)
        if len(tokens) >= 3 and len(message) < 30:
            return DetectorResult(detector_name="Ambiguity", triggered=True, score=0.6,
                                  payload={"reason": "short_multi_entity"})
        return DetectorResult(detector_name="Ambiguity", triggered=False)
    avg = sum(top_k_scores) / max(len(top_k_scores), 1)
    if avg < 0.4:
        return DetectorResult(detector_name="Ambiguity", triggered=True,
                              score=min(1.0, (0.4 - avg) * 2.5), payload={"avg": avg})
    return DetectorResult(detector_name="Ambiguity", triggered=False)


# ─── Detector 3: NoveltyDetector ──────────────────────────────────────
def detect_novelty(
    message: str, *, known_entities: Optional[set[str]] = None,
) -> DetectorResult:
    """V3 §16.2: entity_extract 找不到對應 chunk → 新概念."""
    tokens = set(re.findall(r"[一-鿿]{2,3}|[A-Za-z]{3,}", message))
    if not tokens:
        return DetectorResult(detector_name="Novelty", triggered=False)
    novel = tokens - (known_entities or set())
    novel_ratio = len(novel) / len(tokens)
    if novel_ratio >= 0.6:  # > 60% 是新 entity
        return DetectorResult(
            detector_name="Novelty", triggered=True, score=novel_ratio,
            payload={"novel_entities": sorted(novel)[:5]},
        )
    return DetectorResult(detector_name="Novelty", triggered=False)


# ─── Detector 4: IncongruenceDetector (Phase 2) ───────────────────────
def detect_incongruence(
    message: str, *, valence: float = 0.0,
) -> DetectorResult:
    """V3 §16.2: 字面情緒 vs VAD valence mismatch ≥ 0.5 → 主動關心.

    Phase 1 MVP keyword 偵測;Phase 2 用 LLM 校驗.
    """
    surface_pos = sum(1 for w in ("沒事", "還好", "OK", "ok", "okay", "fine") if w in message.lower())
    if surface_pos > 0 and valence < -0.5:
        return DetectorResult(
            detector_name="Incongruence", triggered=True, score=0.7,
            payload={"surface_says_ok_but_vad_negative": True},
        )
    return DetectorResult(detector_name="Incongruence", triggered=False)


# ─── KnowledgeGap state persistence ───────────────────────────────────
def record_knowledge_gap(
    vault_root: Path, user_id: str, entity: str, *,
    context_excerpt: str = "", certainty_score: float = 0.0,
) -> str:
    """V3 §16.3: 寫入 knowledge_gap_state. 既有同 entity 增 asked_count."""
    now = datetime.now(timezone.utc).isoformat()
    with open_companion_db(vault_root) as conn:
        existing = conn.execute(
            "SELECT gap_id, asked_count FROM knowledge_gap_state WHERE user_id=? AND entity=? AND resolved=0 LIMIT 1",
            (user_id, entity),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE knowledge_gap_state SET asked_count=?, last_seen_at=? WHERE gap_id=?",
                (existing["asked_count"] + 1, now, existing["gap_id"]),
            )
            conn.commit()
            return existing["gap_id"]
        gap_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO knowledge_gap_state (gap_id, user_id, entity, context_excerpt, certainty_score, asked_count, answered, resolved, last_seen_at, created_at) VALUES (?, ?, ?, ?, ?, 1, 0, 0, ?, ?)",
            (gap_id, user_id, entity, context_excerpt, certainty_score, now, now),
        )
        conn.commit()
        return gap_id


def mark_gap_answered(vault_root: Path, gap_id: str) -> None:
    """觀眾回答後標 answered=1 (供之後升 episodic)."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "UPDATE knowledge_gap_state SET answered=1, answered_at=? WHERE gap_id=?",
            (datetime.now(timezone.utc).isoformat(), gap_id),
        )
        conn.commit()


def mark_gap_resolved(vault_root: Path, gap_id: str, *, knowledge_path: str = "") -> None:
    """中之人補進 40_Knowledge_Base/ 後 watcher 標 resolved=1."""
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "UPDATE knowledge_gap_state SET resolved=1, knowledge_path=? WHERE gap_id=?",
            (knowledge_path, gap_id),
        )
        conn.commit()


def list_pending_gaps(vault_root: Path, limit: int = 10) -> list[dict]:
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT gap_id, user_id, entity, asked_count, answered, last_seen_at FROM knowledge_gap_state WHERE resolved=0 ORDER BY asked_count DESC, last_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Proactive Speech 主入口 ─────────────────────────────────────────
def evaluate_proactive_speech(
    vault_root: Path,
    *,
    session_id: str,
    channel_id: str,
    channel_type: str,
    silence_intolerance: float,
    curiosity_urge: float,
    topic_drive: float,
    engagement_seeking: float,
    inhibition_level: float = 0.5,
    idle_seconds: float = 0.0,
    novel_entities_count: int = 0,
    knowledge_gap_pending: int = 0,
    viewer_decline_rate: float = 0.0,
    owner_present_user_id: str = "",
    recent_ignored_count: int = 0,
) -> ProactiveDecision:
    """V3 §16.4 + §22.3.D: 算 proactive_score + 過 channel-aware threshold + dynamic backoff."""

    threshold = _CHANNEL_THRESHOLDS.get(channel_type, 0.5)
    idle_norm = min(1.0, idle_seconds / 60.0)
    sombre_ratio = 1.0 - silence_intolerance  # placeholder for chat sombre detection
    backoff_penalty = min(0.5, recent_ignored_count * 0.15) if channel_type == "public_stream" else min(0.6, recent_ignored_count * 0.3)
    owner_bonus = 0.2 if owner_present_user_id else 0.0

    proactive_score = (
        silence_intolerance * idle_norm
        + topic_drive * 0.3  # 簡化沉悶度 = topic_drive 自身指標
        + curiosity_urge * (novel_entities_count * 0.1 + knowledge_gap_pending * 0.05)
        + engagement_seeking * viewer_decline_rate
        + owner_bonus
        - inhibition_level * 0.3
        - backoff_penalty
    )
    proactive_score = max(0.0, min(1.0, proactive_score))

    # Phase 1 死循環防護 (D53: N=5)
    if recent_ignored_count >= 5 and channel_type != "dm":
        return ProactiveDecision(
            should_speak=False, proactive_score=proactive_score, threshold_used=threshold,
            reason="silence_intolerance_backoff_loop (D-V3-43)",
        )

    if proactive_score < threshold:
        return ProactiveDecision(
            should_speak=False, proactive_score=proactive_score, threshold_used=threshold,
            reason=f"below threshold ({proactive_score:.2f} < {threshold})",
        )

    # 選 trigger_type + action
    if knowledge_gap_pending > 0 and curiosity_urge > 0.4:
        trigger = "curiosity"
        action = "CURIOUS_ASK_BACK"
    elif novel_entities_count > 0:
        trigger = "curiosity"
        action = "PROACTIVE_CLARIFY"
    elif idle_norm > 0.6 and silence_intolerance > 0.5:
        trigger = "silence_fill"
        action = "PROACTIVE_TOPIC_SHIFT"
    elif viewer_decline_rate > 0.5:
        trigger = "engagement"
        action = "PROACTIVE_CALLBACK"
    else:
        trigger = "topic_shift"
        action = "PROACTIVE_TOPIC_SHIFT"

    return ProactiveDecision(
        should_speak=True, proactive_score=proactive_score, threshold_used=threshold,
        trigger_type=trigger, selected_action=action,
        reason=f"triggered ({proactive_score:.2f} >= {threshold})",
    )


def record_proactive_trigger(
    vault_root: Path,
    decision: ProactiveDecision,
    *, session_id: str, channel_id: str, channel_type: str,
    target_user_id: str = "", context_json: str = "{}",
    response_received_within_60s: bool = False,
) -> str:
    """V3 §22.3.E: 寫 proactive_triggers 表 (防重複 + audit)."""
    trigger_id = str(uuid.uuid4())
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT INTO proactive_triggers (trigger_id, session_id, channel_id, channel_type, target_user_id, trigger_type, trigger_score, threshold_used, context_json, action_taken, response_received_within_60s, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trigger_id, session_id, channel_id, channel_type, target_user_id, decision.trigger_type, decision.proactive_score, decision.threshold_used, context_json, decision.selected_action, int(response_received_within_60s), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return trigger_id
