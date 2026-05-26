"""V3 C17b Metacognition — §29.10 H10 對話中即時反思.

對齊 V3 §29.10 + chat pipeline Step 16.5 加 self_consistency_check.

偵測: 當前 response 跟近 N turn 的 raw_events / claims 矛盾 → 主動修正.
範例: 上 turn 說「我喜歡咖啡」, 這 turn 要說「我從不喝咖啡」→ trigger.

Phase 2 MVP: rule-based + simple substring opposite (Phase 3 升 LLM 校驗).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# 簡單矛盾 keyword 配對 (Phase 2 MVP — Phase 3 用 LLM 比對)
_CONTRADICTION_PAIRS = (
    ("喜歡", "討厭"),
    ("喜歡", "不喜歡"),
    ("好喜歡", "好討厭"),
    ("我會", "我不會"),
    ("可以", "不可以"),
    ("一定", "絕對不會"),
    ("我覺得 OK", "我覺得糟"),
)


@dataclass(slots=True)
class MetacognitionResult:
    contradiction_detected: bool = False
    contradicting_excerpt: str = ""
    suggested_correction: str = ""
    reason: str = ""


def _find_contradictions(current: str, previous: str) -> Optional[tuple[str, str]]:
    """V3 §29.10: 兩段文字找互斥 keyword pair."""
    for pos, neg in _CONTRADICTION_PAIRS:
        if pos in current and neg in previous:
            return (neg, pos)
        if neg in current and pos in previous:
            return (pos, neg)
    return None


def check_self_consistency(
    vault_root: Path,
    *,
    candidate_response: str,
    session_id: str,
    look_back_turns: int = 5,
) -> MetacognitionResult:
    """V3 §29.10: 跟近 N turn agent response 比對矛盾."""
    # V3-G3: 兼容 'agent' (Phase 1 早期) 和 'bot' (V3-E1 Bug 12 後對齊)
    # raw_events 實際寫入用 'bot' (chat_runtime Step 17), 但留 'agent' 不破 V3 stress section T mock
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT content FROM raw_events WHERE session_id=? AND actor IN ('agent','bot') ORDER BY created_at DESC LIMIT ?",
            (session_id, look_back_turns),
        ).fetchall()
    if not rows:
        return MetacognitionResult(contradiction_detected=False, reason="no prior agent turns")

    for r in rows:
        prev = r["content"] or ""
        cont = _find_contradictions(candidate_response, prev)
        if cont:
            old_keyword, new_keyword = cont
            return MetacognitionResult(
                contradiction_detected=True,
                contradicting_excerpt=prev[:120],
                suggested_correction=f"等等我剛才講「{old_keyword}」但現在說「{new_keyword}」, 讓我修一下...",
                reason=f"contradiction pair: {old_keyword} vs {new_keyword}",
            )

    return MetacognitionResult(contradiction_detected=False, reason="no contradiction")


def maybe_prefix_correction(response_text: str, result: MetacognitionResult) -> str:
    """V3 §29.10: 偵測到矛盾 → 主動加修正前綴."""
    if not result.contradiction_detected:
        return response_text
    return f"{result.suggested_correction} {response_text}"
