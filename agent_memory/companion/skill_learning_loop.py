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
    # ⭐ V3-N (user 2026-05-27): emotional_origin 加 wikilink body backlink
    origin_link = f"\n## 來源 (Origin)\n\n- [[{skill.emotional_origin}]] (對應 semantic_concept 或 episodic memory)\n" if skill.emotional_origin else ""
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
        f"{origin_link}"
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


def list_recent_skills_summary(vault_root: Path, max_count: int = 3) -> list[dict]:
    """V3-K4 (user 2026-05-27 「升格技能」): 撈最近 skill 給 Memory Router L3 用.

    Returns: [{skill_name, trigger_situation, ...}, ...]
    """
    skills_root = vault_root / "50_Skills_Tools" / "51_Hermes_Learned"
    if not skills_root.exists():
        return []
    skills = []
    for skill_dir in skills_root.iterdir():
        if not (skill_dir.is_dir() and (skill_dir / "SKILL.md").exists()):
            continue
        try:
            content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        except Exception:
            continue
        # 簡單 parse skill_name + trigger_situation
        import re as _re
        name_match = _re.search(r"skill_name:\s*(.+)", content)
        trigger_match = _re.search(r"## 適用情境\s*\n(.+?)\n\n", content, _re.DOTALL)
        skills.append({
            "skill_name": name_match.group(1).strip() if name_match else skill_dir.name,
            "trigger_situation": trigger_match.group(1).strip()[:80] if trigger_match else "",
            "path": str((skill_dir / "SKILL.md").relative_to(vault_root)),
        })
    return skills[:max_count]


def consolidate_skills_via_llm(vault_root: Path) -> int:
    """V3-K4 (user 2026-05-27 「升格技能」): curator L4 7d 從 semantic_memories 升格 skill.

    對齊 V3 §21.3 + user「自然記憶升格技能」設計理念.

    路徑: semantic_memories (我發現 X 有效) → LLM 提煉 → skill (適用情境/描述/步驟)
    寫 50_Skills_Tools/51_Hermes_Learned/<skill_id>/SKILL.md

    Returns: 新升 skill 數.
    """
    try:
        from agent_memory.llm_client import LLMClient
        from agent_memory.llm_text_helpers import call_llm_for_text
        from agent_memory.companion.companion_db import open_companion_db
    except Exception:
        return 0

    from datetime import datetime, timezone, timedelta
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            # 撈近 7d high-conf semantic concepts
            rows = conn.execute(
                "SELECT memory_id, claim, confidence FROM semantic_memories "
                "WHERE created_at > ? AND confidence >= 0.6 AND status='semantic' "
                "ORDER BY confidence DESC, evidence_count DESC LIMIT 5",
                (cutoff_7d,),
            ).fetchall()
    except Exception:
        return 0

    if not rows:
        return 0

    try:
        client = LLMClient(vault_root)
    except Exception:
        return 0

    # 已升格的 skill (避免重複)
    existing_skills = set(list_learned_skills(vault_root))

    written = 0
    for r in rows:
        claim = (r["claim"] or "").strip()
        if not claim:
            continue
        # LLM 把 claim 升成 skill (適用情境 + 步驟)
        prompt = (
            "你是夥伴大腦的 skill consolidation curator.\n"
            "我學到一個概念 (semantic memory):\n"
            f"  「{claim}」\n\n"
            "請把它整理成可重用的 skill (技能), 格式 4 行:\n"
            "NAME: <skill 名稱, kebab-case 英文, ≤30 字>\n"
            "TRIGGER: <什麼情境下用, ≤60 字>\n"
            "DESCRIPTION: <如何運用, 中文 ≤80 字>\n"
            "STEPS: <step1>;<step2>;<step3> (≤3 steps, 分號分隔)\n\n"
            "輸出 (僅 4 行, 無解釋):"
        )
        try:
            result = call_llm_for_text(client, prompt, persona_id="companion", max_tokens=400, auxiliary="skill_consolidation")
            text = (result.text or "").strip()
        except Exception:
            continue

        # Parse
        skill_name = ""
        trigger = ""
        description = ""
        steps_str = ""
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("NAME:"):
                skill_name = line.split(":", 1)[1].strip()[:30]
            elif line.upper().startswith("TRIGGER:"):
                trigger = line.split(":", 1)[1].strip()[:80]
            elif line.upper().startswith("DESCRIPTION:"):
                description = line.split(":", 1)[1].strip()[:100]
            elif line.upper().startswith("STEPS:"):
                steps_str = line.split(":", 1)[1].strip()

        if not skill_name or not trigger:
            continue

        # 去重: 已存在 skill 跳過
        skill_id = _safe_skill_id(skill_name)
        if skill_id in existing_skills:
            continue

        steps_list = [s.strip() for s in steps_str.split(";") if s.strip()][:3]

        skill = SkillRegistration(
            skill_name=skill_name,
            description=description,
            trigger_situation=trigger,
            procedure_steps=steps_list,
            emotional_origin=r["memory_id"],
            success_rate=0.0,
            source="semantic_consolidation",
        )
        result_dict = register_skill(vault_root, skill)
        if result_dict.get("registered"):
            written += 1
            existing_skills.add(skill_id)
    return written
