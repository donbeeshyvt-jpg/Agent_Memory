"""V3 C23 Skill Learning Loop — hermes Learning Loop 介面.

對齊 V3 §21.3 階段三 技能進化 + §4.5 Mode B + D-V3-... hermes 整合.

機制 (V3 §21.3):
- hermes 累積成功經驗 → 內建 Learning Loop 觸發
- 對話中 skill_suggestions (R7 C20b 共用) 提議「我學會了 X, 要不要存?」
- 中之人對話內回 yes → 寫 50_Skills_Tools/54_Taught_Skills/<skill-id>/SKILL.md
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
    emotional_origin: str = ""  # 對應某 emotional_event_id 或 candidate_id
    success_rate: float = 0.0
    source: str = "hermes_learning_loop"  # hermes_learning_loop / conversation_proposal / semantic_consolidation / teaching_detector
    # ⭐ V3-O.14 新增 (user 2026-06-05 拍板「該技能也要寫上是誰哪時候教的 + RAG 適合格式」)
    taught_by_user_id: str = ""
    taught_by_name: str = ""
    first_taught_at: str = ""
    last_reinforced_at: str = ""
    evidence_count: int = 0
    evidence_event_ids: list[str] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)  # RAG embed 用 + FTS5 keyword
    evidence_dialogues: list[dict] = field(default_factory=list)  # [{actor, content, at, event_id}]


def _safe_skill_id(name: str) -> str:
    """轉成檔名安全的 skill_id (kebab-case)."""
    cleaned = re.sub(r"[^a-zA-Z0-9一-鿿_-]+", "-", name.strip().lower())
    return cleaned.strip("-")[:80] or str(uuid.uuid4())


def register_skill(
    vault_root: Path,
    skill: SkillRegistration,
) -> dict:
    """V3 §21.3: 把 hermes/conversation 學到的 skill 寫進 54_Taught_Skills/.

    對齊 V3 規劃書 §A1.2 vault skeleton + R7 C20b skill_suggestions 同 pattern.
    """
    skill_id = _safe_skill_id(skill.skill_name) or skill.skill_id
    skill_dir = vault_root / "50_Skills_Tools" / "54_Taught_Skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    now_iso = datetime.now(timezone.utc).isoformat()

    steps_md = "\n".join(f"{i+1}. {s}" for i, s in enumerate(skill.procedure_steps))
    # ⭐ V3-N (user 2026-05-27): emotional_origin 加 wikilink body backlink
    origin_link = f"\n## 來源 (Origin)\n\n- [[{skill.emotional_origin}]] (對應 semantic_concept 或 episodic memory 或 skill_candidate)\n" if skill.emotional_origin else ""

    # ⭐ V3-O.14 → V3-O.15 schema_v12 對齊 knowledge_base.write_knowledge_v12
    tags_list = ["skill", "learned", skill.source, "schema_v12"]
    if skill.trigger_keywords:
        tags_list.extend([f"trigger:{k}" for k in skill.trigger_keywords[:5]])

    # V3-O.15: contributor_link — obsidian wikilink 連教學者朋友卡
    contributor_link = ""
    if skill.taught_by_user_id:
        safe_uid = skill.taught_by_user_id.replace("/", "_").replace("\\", "_")[:120]
        label = skill.taught_by_name or skill.taught_by_user_id[:18]
        contributor_link = f"[[20_Audience_Graph/22_Casual_Viewers/{safe_uid}|{label}]]"

    frontmatter_lines = [
        "---",
        "type: learned_skill",
        "schema_version: 12",  # V3-O.15: 11→12 對齊 knowledge_base
        f"skill_id: {skill_id}",
        f"skill_name: {skill.skill_name}",
        f"title: {skill.skill_name}",  # alias for KB-style query
        f"source: {skill.source}",
        f"emotional_origin: {skill.emotional_origin}",
        f"success_rate: {skill.success_rate}",
        f"created_at: {now_iso}",
        "lifecycle_state: long",
        "pinned: true",
        f"tags: {tags_list}",
    ]
    # V3-O.14 教學追溯 metadata + V3-O.15 contributor wikilink
    if skill.taught_by_user_id:
        frontmatter_lines.append(f"taught_by_user_id: \"{skill.taught_by_user_id}\"")
        frontmatter_lines.append(f"contributor_user_id: \"{skill.taught_by_user_id}\"")  # alias for KB-style query
    if skill.taught_by_name:
        frontmatter_lines.append(f"taught_by_name: \"{skill.taught_by_name}\"")
        frontmatter_lines.append(f"contributor_name: \"{skill.taught_by_name}\"")
    if contributor_link:
        frontmatter_lines.append(f"contributor_link: \"{contributor_link}\"")
    if skill.first_taught_at:
        frontmatter_lines.append(f"first_taught_at: {skill.first_taught_at}")
        frontmatter_lines.append(f"first_contributed_at: {skill.first_taught_at}")
    if skill.last_reinforced_at:
        frontmatter_lines.append(f"last_reinforced_at: {skill.last_reinforced_at}")
    if skill.evidence_count:
        frontmatter_lines.append(f"evidence_count: {skill.evidence_count}")
    if skill.evidence_event_ids:
        frontmatter_lines.append(f"evidence_event_ids: {skill.evidence_event_ids}")
    if skill.trigger_keywords:
        frontmatter_lines.append(f"trigger_keywords: {skill.trigger_keywords}")
        frontmatter_lines.append(f"related_concept_ids: []")  # placeholder for V3-O.15 雙關聯 (skill_merge_curator 會回填)
    frontmatter_lines.append("---")
    frontmatter_lines.append("")

    body_lines = [
        f"# {skill.skill_name}",
        "",
        "## 觸發情境",  # ⭐ RAG embed 主要靠這段
        skill.trigger_situation or "(未填)",
        "",
        "## 描述",
        skill.description or "(未填)",
        "",
        "## 步驟摘要",
        steps_md or "(無明確步驟)",
        "",
    ]
    # V3-O.14 範例對話 (RAG 撈時帶上下文)
    if skill.evidence_dialogues:
        body_lines.append("## 範例對話")
        body_lines.append("")
        for d in skill.evidence_dialogues[:3]:
            at = (d.get("at") or "")[:19]
            actor = d.get("actor", "?")
            content = (d.get("content") or "").strip()
            body_lines.append(f"- [{at}] **{actor}**: {content[:200]}")
        body_lines.append("")
    # V3-O.14 教學追溯 + V3-O.15 contributor wikilink
    if skill.taught_by_name or skill.evidence_count:
        body_lines.append("## 教學追溯")
        body_lines.append("")
        if contributor_link:
            body_lines.append(f"- 教導者: {contributor_link}")
        elif skill.taught_by_name:
            body_lines.append(f"- 教導者: {skill.taught_by_name} (`{skill.taught_by_user_id[:18]}`)")
        if skill.first_taught_at:
            body_lines.append(f"- 第一次教: {skill.first_taught_at[:19]}")
        if skill.last_reinforced_at:
            body_lines.append(f"- 最後強化: {skill.last_reinforced_at[:19]}")
        if skill.evidence_count:
            body_lines.append(f"- 重複次數: {skill.evidence_count}")
        body_lines.append("")
    # V3-O.14 標籤 (給 FTS5 撈 + 視覺索引)
    if skill.trigger_keywords:
        body_lines.append("## 標籤")
        body_lines.append("")
        body_lines.append(" ".join(f"#{k}" for k in skill.trigger_keywords[:8]))
        body_lines.append("")

    content = "\n".join(frontmatter_lines + body_lines) + origin_link
    atomic_write(skill_path, content)
    return {
        "registered": True,
        "skill_id": skill_id,
        "path": str(skill_path.relative_to(vault_root)),
    }


def list_learned_skills(vault_root: Path) -> list[str]:
    """V3 §21.3: 列已學技能."""
    skills_root = vault_root / "50_Skills_Tools" / "54_Taught_Skills"
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
    skills_root = vault_root / "50_Skills_Tools" / "54_Taught_Skills"
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
    寫 50_Skills_Tools/54_Taught_Skills/<skill_id>/SKILL.md

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
