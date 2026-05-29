"""V3-O.10 #39 — 群體 sentiment aggregate.

5 分鐘滑窗, 多 viewer 情緒平均 → 進 prompt_packet.
讓精神體感知整個聊天室的整體氛圍, 不只是單一 user 的情緒.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path


def get_group_sentiment(
    vault_root: Path,
    *,
    window_minutes: int = 5,
    exclude_user_id: str = "",
) -> dict:
    """計算近 window_minutes 分鐘內所有 viewer 的平均情緒.

    Returns:
        {"avg_valence": float, "avg_arousal": float, "viewer_count": int,
         "dominant_emotion": str, "window_minutes": int}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    try:
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT user_id, valence, arousal, dominant_emotion FROM emotion_state "
                "WHERE timestamp >= ? AND user_id != ? "
                "GROUP BY user_id "
                "ORDER BY timestamp DESC",
                (cutoff, exclude_user_id or ""),
            ).fetchall()
    except Exception:
        return {"avg_valence": 0.0, "avg_arousal": 0.3, "viewer_count": 0,
                "dominant_emotion": "neutral", "window_minutes": window_minutes}

    if not rows:
        return {"avg_valence": 0.0, "avg_arousal": 0.3, "viewer_count": 0,
                "dominant_emotion": "neutral", "window_minutes": window_minutes}

    valences = [float(r["valence"] or 0.0) for r in rows]
    arousals = [float(r["arousal"] or 0.3) for r in rows]
    avg_v = sum(valences) / len(valences)
    avg_a = sum(arousals) / len(arousals)

    # 多數決 dominant_emotion
    from collections import Counter
    emo_counts = Counter(r["dominant_emotion"] or "neutral" for r in rows)
    dominant = emo_counts.most_common(1)[0][0]

    return {
        "avg_valence": round(avg_v, 3),
        "avg_arousal": round(avg_a, 3),
        "viewer_count": len(rows),
        "dominant_emotion": dominant,
        "window_minutes": window_minutes,
    }
