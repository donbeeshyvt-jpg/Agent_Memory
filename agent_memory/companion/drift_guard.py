"""V3 C22 Drift Guard — Persona Candidate 嚴審.

對齊 V3 §22 + D-V3-12 + D-V3-15 (Owner 不蓋過 safety).

機制:
- trait_evolution candidate → drift_guard 算 drift_score
- drift_score < 0.5 → 拒 (太溫和或太激烈)
- drift_score >= 0.5 + identity_relevance < 0.75 → 寫 73_Candidates/ 待中之人確認
- 必須**人工** active (Phase 3 MVP: 自動寫 candidate, 不自動 active)

backup 對應 hermes curator.backup.keep=5.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db
from agent_memory.security.atomic import atomic_write


_DRIFT_MIN = 0.5
_DRIFT_MAX = 1.2


@dataclass(slots=True)
class DriftAuditResult:
    user_id: str
    trait_name: str
    drift_score: float
    passed: bool
    reason: str
    candidate_path: str = ""  # 73_Candidates/ 內檔案路徑


def compute_drift_score(
    *, current_value: float, proposed_value: float, evidence_count: int,
) -> float:
    """V3 §22: 簡單 drift = abs(delta) × (evidence / target_evidence).

    range 0~1+ (大於 1 表強 drift, 需嚴審).
    """
    delta = abs(proposed_value - current_value)
    evidence_factor = min(1.0, evidence_count / 10.0)
    return delta * evidence_factor


def audit_candidate(
    vault_root: Path,
    user_id: str,
    trait_name: str,
) -> DriftAuditResult:
    """V3 §22 + Persona Candidate 必須人工確認.

    動作:
    1. 算 drift_score
    2. 不過閾值 → reject
    3. 過閾值 → 寫 70_Persona_Versions/73_Candidates/<candidate_id>.md (人工待審)
    """
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT evidence_count, current_value, proposed_value, events_json FROM trait_evolution "
            "WHERE user_id=? AND trait_name=?",
            (user_id, trait_name),
        ).fetchone()
    if row is None:
        return DriftAuditResult(user_id=user_id, trait_name=trait_name, drift_score=0.0, passed=False, reason="not_found")

    drift = compute_drift_score(
        current_value=row["current_value"] or 0.0,
        proposed_value=row["proposed_value"] or 0.0,
        evidence_count=row["evidence_count"],
    )

    if drift < _DRIFT_MIN:
        return DriftAuditResult(
            user_id=user_id, trait_name=trait_name, drift_score=drift,
            passed=False, reason=f"drift_too_low ({drift:.2f} < {_DRIFT_MIN})",
        )

    if drift > _DRIFT_MAX:
        return DriftAuditResult(
            user_id=user_id, trait_name=trait_name, drift_score=drift,
            passed=False, reason=f"drift_too_extreme ({drift:.2f} > {_DRIFT_MAX} — 防社工)",
        )

    # 寫 73_Candidates/
    candidate_id = str(uuid.uuid4())
    candidate_path = vault_root / "70_Persona_Versions" / "73_Candidates" / f"{candidate_id}.md"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\n"
        f"type: persona_candidate\nschema_version: 10\n"
        f"user_id: {user_id}\ntrait_name: {trait_name}\n"
        f"evidence_count: {row['evidence_count']}\ndrift_score: {drift:.3f}\n"
        f"proposed_value: {row['proposed_value']}\ncurrent_value: {row['current_value']}\n"
        f"awaiting_human_confirm: true\n"
        f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"---\n"
        f"# Persona Candidate\n\n"
        f"Trait: **{trait_name}** for user {user_id}\n\n"
        f"- evidence_count: {row['evidence_count']}\n"
        f"- proposed: {row['proposed_value']:.3f}\n"
        f"- current: {row['current_value']:.3f}\n"
        f"- drift_score: {drift:.3f}\n\n"
        f"⚠ 此候選必須中之人手動確認才會 active. drift_guard 不自動套用.\n"
    )
    atomic_write(candidate_path, content)

    return DriftAuditResult(
        user_id=user_id, trait_name=trait_name, drift_score=drift, passed=True,
        reason="written_to_candidates_awaiting_human",
        candidate_path=str(candidate_path.relative_to(vault_root)),
    )
