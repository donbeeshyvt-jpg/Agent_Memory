# -*- coding: utf-8 -*-
"""V3-O.15 (2026-06-05 user 拍板): 每日 merge 類似技能.

對齊:
- user「定期把類似技能整合(一天整合一次)」
- 由 companion_curator.run_layer3_24h_medium 每日呼叫一次
- 用 trigger_keywords 重疊度 + LLM judge 決定 merge

流程:
  ① 掃 50_Skills_Tools/51_Hermes_Learned/* SKILL.md
  ② 解 frontmatter trigger_keywords + skill_name
  ③ 用 jaccard similarity 找 cluster (>=0.4 視為候選)
  ④ 每 cluster 送 LLM (sub_task) 判斷該不該 merge
  ⑤ merge 結果 → 寫新 SKILL.md (含 absorbed_skill_ids + 合併 evidence_count + 多教導者)
  ⑥ 老的 archive 到 51_Hermes_Learned/_merged/<original_id>/
"""
from __future__ import annotations

import json
import re
import shutil
import yaml
from datetime import datetime, timezone
from pathlib import Path


SIMILARITY_THRESHOLD = 0.4  # jaccard ≥ 0.4 視為 cluster 候選
MIN_CLUSTER_SIZE = 2


# ─── 主入口 ─────────────────────────────────────────────────
def consolidate_similar_skills(vault_root: Path) -> dict:
    """V3-O.15: 每日 Layer3 觸發一次. 掃 + cluster + LLM judge + merge.

    Returns: {scanned: N, clusters: M, merged: K, archived: L}
    """
    out = {"scanned": 0, "clusters": 0, "merged": 0, "archived": 0}
    skills_root = vault_root / "50_Skills_Tools" / "51_Hermes_Learned"
    if not skills_root.exists():
        return out

    # 1. 掃所有 SKILL.md (跳 _merged/)
    skills = []
    for skill_dir in skills_root.iterdir():
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            continue
        meta = _parse_skill_md(skill_path)
        if meta:
            meta["_dir"] = skill_dir
            meta["_path"] = skill_path
            skills.append(meta)
    out["scanned"] = len(skills)
    if len(skills) < 2:
        return out

    # 2. cluster by trigger_keywords jaccard
    clusters = _cluster_by_keywords(skills, threshold=SIMILARITY_THRESHOLD)
    out["clusters"] = sum(1 for c in clusters if len(c) >= MIN_CLUSTER_SIZE)
    if out["clusters"] == 0:
        return out

    # 3. LLM judge per cluster
    try:
        from agent_memory.llm_client import LLMClient
        from agent_memory.llm_text_helpers import call_llm_for_text
        client = LLMClient(vault_root)
    except Exception:
        return out

    for cluster in clusters:
        if len(cluster) < MIN_CLUSTER_SIZE:
            continue
        try:
            judge_result = _llm_judge_merge(client, cluster)
        except Exception:
            continue
        if not judge_result or not judge_result.get("should_merge"):
            continue

        merged = judge_result.get("merged_skill", {})
        if not merged.get("skill_name"):
            continue

        # 4. 寫新 merged SKILL.md
        try:
            new_path = _write_merged_skill(vault_root, cluster, merged)
            if new_path:
                out["merged"] += 1
                # 5. archive 老的
                for old in cluster:
                    try:
                        archived = _archive_skill_dir(old["_dir"])
                        if archived:
                            out["archived"] += 1
                    except Exception:
                        pass
        except Exception:
            pass

    return out


# ─── helpers ────────────────────────────────────────────────
def _parse_skill_md(path: Path) -> dict:
    """解 frontmatter, 抽 skill_name + trigger_keywords + taught_by 等."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    fm_text = text[4:end]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        return {}
    return {
        "skill_id": fm.get("skill_id", ""),
        "skill_name": fm.get("skill_name", ""),
        "trigger_keywords": [k for k in (fm.get("trigger_keywords") or []) if k],
        "taught_by_name": fm.get("taught_by_name", ""),
        "taught_by_user_id": fm.get("taught_by_user_id", ""),
        "evidence_count": int(fm.get("evidence_count", 1) or 1),
        "evidence_event_ids": fm.get("evidence_event_ids") or [],
        "first_contributed_at": fm.get("first_taught_at") or fm.get("first_contributed_at", ""),
        "last_reinforced_at": fm.get("last_reinforced_at", ""),
        "_full_text": text,
    }


def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _cluster_by_keywords(skills: list, *, threshold: float) -> list[list]:
    """Greedy cluster: 對每對算 jaccard, ≥ threshold 同 cluster."""
    n = len(skills)
    parent = list(range(n))  # union-find

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            sim = _jaccard(skills[i]["trigger_keywords"], skills[j]["trigger_keywords"])
            if sim >= threshold:
                union(i, j)
    clusters_map: dict[int, list] = {}
    for i in range(n):
        r = find(i)
        clusters_map.setdefault(r, []).append(skills[i])
    return list(clusters_map.values())


def _llm_judge_merge(llm_client, cluster: list) -> dict:
    """LLM 判斷該不該 merge cluster 內的 N 個 skill."""
    from agent_memory.llm_text_helpers import call_llm_for_text

    cluster_summary_lines = []
    for i, s in enumerate(cluster, 1):
        cluster_summary_lines.append(
            f"Skill {i}:\n"
            f"  name: {s['skill_name']}\n"
            f"  keywords: {s['trigger_keywords']}\n"
            f"  taught_by: {s['taught_by_name']}\n"
            f"  evidence_count: {s['evidence_count']}"
        )
    prompt = (
        "你是夥伴大腦的 skill consolidation curator (每日 merge).\n"
        "夥伴最近學到 N 個技能, 可能是同一件事的不同說法, 請判斷該不該合併.\n\n"
        f"候選技能 (已用 trigger_keywords jaccard>0.4 篩出):\n"
        + "\n\n".join(cluster_summary_lines)
        + "\n\n"
        "判斷標準:\n"
        "- 是否在描述同一個概念 (不是字面相同)\n"
        "- 觸發情境是否重疊\n"
        "- 教導者語意是否一致\n\n"
        "請輸出純 JSON (不要 code fence):\n"
        "{\n"
        '  "should_merge": true/false,\n'
        '  "reason": "<≤80 字判斷依據>",\n'
        '  "merged_skill": {\n'
        '    "skill_name": "<新合併後的技能名稱, ≤30字>",\n'
        '    "trigger_situation": "<merged 觸發情境, ≤120字>",\n'
        '    "description": "<merged 描述, ≤200字>",\n'
        '    "trigger_keywords": ["<merged keywords, dedup, ≤15>"],\n'
        '    "procedure_steps": ["<step1>", "<step2>", "<step3>"]\n'
        "  }\n"
        "}\n\n"
        "若 should_merge=false, merged_skill 可填空 {}."
    )
    try:
        result = call_llm_for_text(
            llm_client, prompt,
            persona_id="companion",
            max_tokens=2000,
            auxiliary="skill_consolidation",
        )
        text = (result.text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return {}


def _write_merged_skill(vault_root: Path, cluster: list, merged: dict) -> Path:
    """寫新 merged SKILL.md, 含 absorbed_skill_ids + 合併教導者列表."""
    from agent_memory.companion.skill_learning_loop import SkillRegistration, register_skill

    # 合併 evidence + 教導者
    total_evidence = sum(s["evidence_count"] for s in cluster)
    all_evt_ids = []
    for s in cluster:
        all_evt_ids.extend(s["evidence_event_ids"])
    all_evt_ids = list(dict.fromkeys(all_evt_ids))[-15:]  # dedup, 最多 15 條

    # 主教導者 = evidence 最多的
    primary = max(cluster, key=lambda s: s["evidence_count"])
    teachers_list = list(dict.fromkeys(
        (s["taught_by_name"] for s in cluster if s["taught_by_name"])
    ))

    skill = SkillRegistration(
        skill_name=merged.get("skill_name", "")[:30],
        description=merged.get("description", "")[:300] +
                    (f"\n\n(合併自: {', '.join(teachers_list)} 共 {len(cluster)} 個相似技能)" if teachers_list else ""),
        trigger_situation=merged.get("trigger_situation", "")[:200],
        procedure_steps=[s for s in (merged.get("procedure_steps") or []) if s][:5],
        emotional_origin=";".join(s["skill_id"] for s in cluster),
        source="skill_merge_curator",
        taught_by_user_id=primary["taught_by_user_id"],
        taught_by_name=primary["taught_by_name"],
        first_taught_at=min(s["first_contributed_at"] for s in cluster if s["first_contributed_at"]) or "",
        last_reinforced_at=datetime.now(timezone.utc).isoformat(),
        evidence_count=total_evidence,
        evidence_event_ids=all_evt_ids,
        trigger_keywords=[k for k in (merged.get("trigger_keywords") or []) if k][:15],
        evidence_dialogues=[],
    )
    result = register_skill(vault_root, skill)
    if result.get("registered"):
        # 補寫 absorbed_skill_ids 到 frontmatter (作 audit)
        try:
            new_md = vault_root / result["path"]
            text = new_md.read_text(encoding="utf-8")
            absorbed_line = f"absorbed_skill_ids: {[s['skill_id'] for s in cluster]}\n"
            text = text.replace("---\n\n# ", f"{absorbed_line}---\n\n# ", 1)
            new_md.write_text(text, encoding="utf-8")
            return new_md
        except Exception:
            pass
    return None


def _archive_skill_dir(skill_dir: Path) -> Path:
    """搬老的 skill 目錄到 _merged/."""
    parent = skill_dir.parent
    merged_root = parent / "_merged"
    merged_root.mkdir(parents=True, exist_ok=True)
    dest = merged_root / skill_dir.name
    if dest.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        dest = merged_root / f"{skill_dir.name}_{ts}"
    shutil.move(str(skill_dir), str(dest))
    return dest
