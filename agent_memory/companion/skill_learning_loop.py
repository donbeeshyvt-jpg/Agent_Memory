"""V3 C23 Skill Learning Loop — hermes Learning Loop 介面.

對齊 V3 §21.3 階段三 技能進化 + §4.5 Mode B + D-V3-... hermes 整合.

機制 (V3 §21.3):
- hermes 累積成功經驗 → 內建 Learning Loop 觸發
- 對話中 skill_suggestions (R7 C20b 共用) 提議「我學會了 X, 要不要存?」
- 中之人對話內回 yes → 寫 50_Skills_Tools/51_Hermes_Learned/<skill-id>/SKILL.md
- 下次遇到類似情境 → Memory Router 抓到該 SKILL → 直接調用

Phase 3 MVP: 介面 + skill 寫入. Phase 4 接 Memory Router retrieve.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.security.atomic import atomic_write


@dataclass(slots=True)
class SkillRegistration:
    skill_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    skill_name: str = ""
    description: str = ""
    trigger_situation: str = ""
    procedure_steps: list[str] = field(default_factory=list)
    emotional_origin: str = ""  # 對應某 emotional_event_id
    success_rate: float = 0.0
    source: str = "hermes_learning_loop"  # 或 conversation_proposal


def _safe_skill_id(name: str) -> str:
    """轉成檔名安全的 skill_id (kebab-case)."""
    cleaned = re.sub(r"[^a-zA-Z0-9一-鿿_-]+", "-", name.strip().lower())
    return cleaned.strip("-")[:80] or str(uuid.uuid4())


def register_skill(
    vault_root: Path,
    skill: SkillRegistration,
) -> dict:
    """V3 §21.3: 把 hermes/conversation 學到的 skill 寫進 51_Hermes_Learned/.

    對齊 V3 規劃書 §A1.2 vault skeleton + R7 C20b skill_suggestions 同 pattern.
    """
    skill_id = _safe_skill_id(skill.skill_name) or skill.skill_id
    skill_dir = vault_root / "50_Skills_Tools" / "51_Hermes_Learned" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"

    steps_md = "\n".join(f"{i+1}. {s}" for i, s in enumerate(skill.procedure_steps))
    content = (
        f"---\n"
        f"type: learned_skill\nschema_version: 10\n"
        f"skill_id: {skill_id}\nskill_name: {skill.skill_name}\n"
        f"source: {skill.source}\n"
        f"emotional_origin: {skill.emotional_origin}\n"
        f"success_rate: {skill.success_rate}\n"
        f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"lifecycle_state: long\npinned: true\n"
        f"tags: [skill, learned, hermes_loop]\n"
        f"---\n\n"
        f"# {skill.skill_name}\n\n"
        f"## 適用情境\n{skill.trigger_situation}\n\n"
        f"## 描述\n{skill.description}\n\n"
        f"## 步驟\n{steps_md or '(無)'}\n"
    )
    atomic_write(skill_path, content)
    return {
        "registered": True,
        "skill_id": skill_id,
        "path": str(skill_path.relative_to(vault_root)),
    }


def list_learned_skills(vault_root: Path) -> list[str]:
    """V3 §21.3: 列已學技能."""
    skills_root = vault_root / "50_Skills_Tools" / "51_Hermes_Learned"
    if not skills_root.exists():
        return []
    out = []
    for skill_dir in skills_root.iterdir():
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            out.append(skill_dir.name)
    return out
