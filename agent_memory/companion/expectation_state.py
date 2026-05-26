"""V3 C24b Expectation State — §29.12 H12 期待 / 失望循環.

對齊 V3 §29.12 + Phase 3 (進階, 對齊 D-V3-30).

每場直播開始時設 baseline (concurrent_viewers / chat_velocity / owner_present 期待):
- 過程中對比實際 → delta
- delta > 0.3 (超預期) → arousal +0.2 / joy +0.15
- delta < -0.3 (沒達標) → valence -0.1 / sadness +0.1
- curator 7d deep 校準 long-term expectation (避免長期失望)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class ExpectationItem:
    expectation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    metric: str = "viewers"  # viewers / chat_velocity / owner_present / ...
    expected_value: float = 0.0
    actual_value: float = 0.0
    delta: float = 0.0
    affect_impact_json: str = ""


def set_baseline(
    vault_root: Path, session_id: str, metric: str, expected_value: float,
) -> str:
    """V3 §29.12: 直播開始 set baseline."""
    eid = str(uuid.uuid4())
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT INTO expectation_state (expectation_id, session_id, metric, expected_value, actual_value, delta, affect_impact_json, timestamp) VALUES (?, ?, ?, ?, 0.0, 0.0, ?, ?)",
            (eid, session_id, metric, expected_value, "{}", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return eid


def update_actual(
    vault_root: Path, expectation_id: str, actual_value: float,
) -> dict:
    """V3 §29.12: 更新 actual → 算 delta → affect impact.

    Returns: {delta, affect_impact}
    - delta > 0.3 (超預期): {valence_offset: +0.05, joy_offset: +0.15, arousal_offset: +0.2}
    - delta < -0.3 (沒達標): {valence_offset: -0.1, sadness_offset: +0.1}
    """
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT expected_value FROM expectation_state WHERE expectation_id=?",
            (expectation_id,),
        ).fetchone()
        if row is None:
            return {"error": "expectation_not_found"}
        expected = row["expected_value"] or 0.0
        # 用 normalized delta (除以 expected 避免 magnitude 偏差)
        delta = (actual_value - expected) / max(abs(expected), 1.0)

        affect_impact = {}
        if delta > 0.3:
            affect_impact = {"valence_offset": 0.05, "joy_offset": 0.15, "arousal_offset": 0.2}
        elif delta < -0.3:
            affect_impact = {"valence_offset": -0.1, "sadness_offset": 0.1}

        conn.execute(
            "UPDATE expectation_state SET actual_value=?, delta=?, affect_impact_json=? WHERE expectation_id=?",
            (actual_value, delta, json.dumps(affect_impact), expectation_id),
        )
        conn.commit()
    return {"delta": delta, "affect_impact": affect_impact}


def list_session_expectations(vault_root: Path, session_id: str) -> list[dict]:
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT * FROM expectation_state WHERE session_id=? ORDER BY timestamp ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]
