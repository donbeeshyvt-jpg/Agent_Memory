# -*- coding: utf-8 -*-
"""V3-O.15 (2026-06-05 user 拍板): 每日 merge 類似技能.

對齊:
- user「定期把類似技能整合(一天整合一次)」
- 由 companion_curator.run_layer3_24h_medium 每日呼叫一次
- 用 trigger_keywords 重疊度 + LLM judge 決定 merge

流程:
  ① 掃 50_Skills_Tools/54_Taught_Skills/* SKILL.md
  ② 解 frontmatter trigger_keywords + skill_name
  ③ 用 jaccard similarity 找 cluster (>=0.4 視為候選)
  ④ 每 cluster 送 LLM (sub_task) 判斷該不該 merge
  ⑤ merge 結果 → 寫新 SKILL.md (含 absorbed_skill_ids + 合併 evidence_count + 多教導者)
  ⑥ 老的 archive 到 54_Taught_Skills/_merged/<original_id>/
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


# ─── 主入口 (V3-O.15.23 三層 merge 架構) ──────────────────────
# L0 母資料夾 54_Taught_Skills/ (直屬, 跳 _*) = 新升格技能, 15min 掃這層
# L1 子資料夾 54_Taught_Skills/_consolidated/ = 合併結果 (已合併過一次), 15min 不掃、每週才掃
# 合併成功 → 結果進 L1 + 被吸收的舊技能直接刪除 (內容已併進結果). RAG 兩層都撈 (retrieve 排除清單沒含 _consolidated).
_L1_SUBDIR = "_consolidated"


def _collect_skills(skill_dirs) -> list:
    """掃一批 skill 目錄 → meta list (含 _dir/_path)."""
    skills = []
    for d in skill_dirs:
        if not d.is_dir():
            continue
        sp = d / "SKILL.md"
        if not sp.exists():
            continue
        meta = _parse_skill_md(sp)
        if meta:
            meta["_dir"] = d
            meta["_path"] = sp
            skills.append(meta)
    return skills


def _merge_skills(vault_root: Path, skills: list, out: dict) -> None:
    """共用核心: cluster + LLM judge + 寫合併結果(進 L1) + 刪除被吸收的舊技能."""
    out["scanned"] = len(skills)
    if len(skills) < 2:
        return
    clusters = _cluster_by_keywords(skills, threshold=SIMILARITY_THRESHOLD)
    out["clusters"] = sum(1 for c in clusters if len(c) >= MIN_CLUSTER_SIZE)
    if out["clusters"] == 0:
        return
    try:
        from agent_memory.llm_text_helpers import call_llm_for_text  # noqa: F401  early import check
    except Exception:
        return
    for cluster in clusters:
        if len(cluster) < MIN_CLUSTER_SIZE:
            continue
        try:
            judge_result = _llm_judge_merge(vault_root, cluster)
        except Exception:
            continue
        if not judge_result or not judge_result.get("should_merge"):
            continue
        merged = judge_result.get("merged_skill", {})
        if not merged.get("skill_name"):
            continue
        try:
            new_path = _write_merged_skill(vault_root, cluster, merged)  # 寫進 L1 _consolidated/
            if new_path:  # 確認合併成功
                out["merged"] += 1
                for old in cluster:  # V3-O.15.23: 確認成功後刪除被吸收的舊技能 (不保留)
                    try:
                        _archive_skill_dir(old["_dir"])
                        out["archived"] += 1
                    except Exception:
                        pass
        except Exception:
            pass


def consolidate_similar_skills(vault_root: Path) -> dict:
    """V3-O.15.23 (15 分鐘): 只掃 L0 母資料夾 (直屬, 跳 _*) → 合併 → 結果寫進 L1 _consolidated/
    → 母資料夾被吸收的舊技能刪除. 不掃 L1 (避免重複合併已合併的). 跨層綜合由 weekly 做.

    Returns: {scanned, clusters, merged, archived}
    """
    out = {"scanned": 0, "clusters": 0, "merged": 0, "archived": 0}
    root = vault_root / "50_Skills_Tools" / "54_Taught_Skills"
    if not root.exists():
        return out
    l0_dirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")]
    _merge_skills(vault_root, _collect_skills(l0_dirs), out)
    return out


def consolidate_weekly_comprehensive(vault_root: Path) -> dict:
    """V3-O.15.23 (每週一次): 掃 L0 母資料夾 + L1 _consolidated/ 一起 → 綜合再合併 (跨層整合)
    → 結果寫進 L1 → 被吸收的 (不論 L0/L1) 刪除. 讓合併過一次的也能跟新技能再整合.

    Returns: {scanned, clusters, merged, archived}
    """
    out = {"scanned": 0, "clusters": 0, "merged": 0, "archived": 0}
    root = vault_root / "50_Skills_Tools" / "54_Taught_Skills"
    if not root.exists():
        return out
    l0_dirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")]
    l1_root = root / _L1_SUBDIR
    l1_dirs = [d for d in l1_root.iterdir() if d.is_dir()] if l1_root.exists() else []
    _merge_skills(vault_root, _collect_skills(l0_dirs + l1_dirs), out)
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
    # ⭐ V3-O.15.19: 抽 body rich 段落 (供合併「保留內容、不壓掉」)
    body = text[end + 5:]

    def _sec(*headers) -> str:
        for h in headers:
            m = re.search(r'(?m)^##\s*' + re.escape(h) + r'[^\n]*\n(.*?)(?=(?:^##\s)|\Z)', body, re.S)
            if m:
                return m.group(1).strip()
        return ""

    return {
        "skill_id": fm.get("skill_id", ""),
        "skill_name": fm.get("skill_name", ""),
        "trigger_keywords": [k for k in (fm.get("trigger_keywords") or []) if k],
        "taught_by_name": fm.get("taught_by_name", ""),
        "taught_by_user_id": fm.get("taught_by_user_id", ""),
        "evidence_count": int(fm.get("evidence_count", 1) or 1),
        "evidence_event_ids": fm.get("evidence_event_ids") or [],
        "first_contributed_at": str(fm.get("first_taught_at") or fm.get("first_contributed_at", "") or ""),
        "last_reinforced_at": str(fm.get("last_reinforced_at", "") or ""),
        "_full_text": text,
        # rich 段落 raw (給 merge 保留)
        "_trigger_raw": _sec("觸發情境"),
        "_literal_raw": _sec("實際打法"),
        "_worked_raw": _sec("正確示範"),
        "_desc_raw": _sec("描述"),
        "_core_raw": _sec("核心摘要"),
        "_full_raw": _sec("完整內容"),
        "_boundary_raw": _sec("使用邊界"),
        "_steps_raw": _sec("步驟摘要"),
    }


def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _name_jaccard(a: str, b: str) -> float:
    """V3-O.15.19: skill_name char-bigram jaccard — 補「keywords 空就永遠不合併」."""
    def bg(s):
        s = (s or "").strip()
        return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else ({s} if s else set())
    A, B = bg(a), bg(b)
    if not A and not B:
        return 0.0
    return len(A & B) / len(A | B) if (A | B) else 0.0


_LITERAL_BULLET_RE = re.compile(r'^-\s*(?:\[([^\]]+)\]\s*)?`([^`]+)`(?:\s*—\s*(.+))?$')


def _parse_literal_bullets(raw: str) -> list[dict]:
    """V3-O.15.19: 把 ## 實際打法 的 bullet 還原成 {kind, literal, note} 給 merge union (保留 code)."""
    out = []
    for ln in (raw or "").splitlines():
        m = _LITERAL_BULLET_RE.match(ln.strip())
        if m:
            out.append({"kind": (m.group(1) or "").strip(),
                        "literal": m.group(2).strip(),
                        "note": (m.group(3) or "").strip()})
    return out


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
            sim_kw = _jaccard(skills[i]["trigger_keywords"], skills[j]["trigger_keywords"])
            sim_name = _name_jaccard(skills[i].get("skill_name", ""), skills[j].get("skill_name", ""))
            # V3-O.15.19: keywords OR skill_name 相似都視為同群 (修 keywords 空就永遠不合併)
            if sim_kw >= threshold or sim_name >= threshold:
                union(i, j)
    clusters_map: dict[int, list] = {}
    for i in range(n):
        r = find(i)
        clusters_map.setdefault(r, []).append(skills[i])
    return list(clusters_map.values())


def _llm_judge_merge(vault_root: Path, cluster: list) -> dict:
    """LLM 判斷該不該 merge cluster 內的 N 個 skill. V3-O.15 fix: 用 vault_root."""
    from agent_memory.llm_text_helpers import call_llm_for_text

    cluster_summary_lines = []
    for i, s in enumerate(cluster, 1):
        cluster_summary_lines.append(
            f"Skill {i}:\n"
            f"  name: {s['skill_name']}\n"
            f"  keywords: {s['trigger_keywords']}\n"
            f"  描述/摘要: {(s.get('_core_raw') or s.get('_desc_raw') or '')[:300]}\n"
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
        '    "core_summary": "<合併後核心摘要, 統整所有來源, 200-400字>",\n'
        '    "trigger_keywords": ["<merged keywords, dedup, ≤15>"],\n'
        '    "procedure_steps": ["<step1>", "<step2>", "<step3>"]\n'
        "  }\n"
        "}\n\n"
        "若 should_merge=false, merged_skill 可填空 {}."
    )
    try:
        text = (call_llm_for_text(
            vault_root, prompt,
            persona_id="companion",
            temperature=0.3,
            timeout_s=60.0,
            auxiliary="skill_consolidation",
        ) or "").strip()
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

    # ⭐ V3-O.15.19: 機械式 union 保留各來源 rich 內容 (不靠 LLM 壓縮, 保證 code/細節不丟, 可以很多字)
    _merged_literal, _seen_lit = [], set()
    for s in cluster:
        for m in _parse_literal_bullets(s.get("_literal_raw", "")):
            if m["literal"] and m["literal"] not in _seen_lit:
                _seen_lit.add(m["literal"]); _merged_literal.append(m)
    _merged_trigs, _seen_t = [], set()
    for s in cluster:
        for ln in (s.get("_trigger_raw", "") or "").splitlines():
            t = ln.strip().lstrip("-").strip()
            if t and t not in _seen_t:
                _seen_t.add(t); _merged_trigs.append(t)
    _avoid, _cons = [], []
    for s in cluster:
        for ln in (s.get("_boundary_raw", "") or "").splitlines():
            ln = ln.strip()
            if not ln.startswith("-"):
                continue
            v = ln.split(":", 1)[-1].strip() if ":" in ln else ln.lstrip("-✗⚠ ").strip()
            if "限制" in ln:
                if v and v not in _cons: _cons.append(v)
            elif v and v not in _avoid:
                _avoid.append(v)
    # 完整內容: 逐源 verbatim 串接 (含完整內容+正確示範+步驟, 保留全部)
    _full_parts = []
    for s in cluster:
        seg = []
        for hdr, raw in (("完整內容", s.get("_full_raw")), ("正確示範", s.get("_worked_raw")), ("步驟", s.get("_steps_raw"))):
            if raw:
                seg.append(f"#### {hdr}\n{raw}")
        if not seg and s.get("_desc_raw"):
            seg.append(s["_desc_raw"])
        if seg:
            _full_parts.append(f"### 來自「{s['skill_name']}」(教導者 {s['taught_by_name']}, evidence {s['evidence_count']})\n" + "\n\n".join(seg))
    _merged_full = ("\n\n".join(_full_parts))[:20000]
    _merged_core = (merged.get("core_summary") or "；".join(c for c in (s.get("_core_raw", "") for s in cluster) if c))[:600]

    skill = SkillRegistration(
        skill_name=merged.get("skill_name", "")[:30],
        description=merged.get("description", "")[:300] +
                    (f"\n\n(合併自: {', '.join(teachers_list)} 共 {len(cluster)} 個相似技能)" if teachers_list else ""),
        core_summary=_merged_core,
        full_content=_merged_full,
        trigger_situation=merged.get("trigger_situation", "")[:200],
        trigger_situations=_merged_trigs[:8],
        literal_mechanism=_merged_literal[:25],
        usage_boundaries={"avoid_when": _avoid[:6], "constraints": _cons[:5]} if (_avoid or _cons) else {},
        procedure_steps=[s for s in (merged.get("procedure_steps") or []) if s][:6],
        emotional_origin=";".join(s["skill_id"] for s in cluster),
        source="skill_merge_curator",
        taught_by_user_id=primary["taught_by_user_id"],
        taught_by_name=primary["taught_by_name"],
        first_taught_at=min((s["first_contributed_at"] for s in cluster if s["first_contributed_at"]), default="") or "",
        last_reinforced_at=datetime.now(timezone.utc).isoformat(),
        evidence_count=total_evidence,
        evidence_event_ids=all_evt_ids,
        trigger_keywords=[k for k in (merged.get("trigger_keywords") or []) if k][:15],
        evidence_dialogues=[],
    )
    result = register_skill(vault_root, skill)
    if result.get("registered"):
        try:
            new_md = vault_root / result["path"]
            text = new_md.read_text(encoding="utf-8")
            # V3-O.15.23: absorbed_skill_ids + consolidated 標記 (已合併過一次)
            marker = (f"absorbed_skill_ids: {[s['skill_id'] for s in cluster]}\n"
                      f"consolidated: true\n")
            text = text.replace("---\n\n# ", f"{marker}---\n\n# ", 1)
            new_md.write_text(text, encoding="utf-8")
            # V3-O.15.23: 合併結果搬進 L1 子資料夾 _consolidated/ (RAG 仍撈, 15min merge 不掃這層)
            src_dir = new_md.parent
            l1_root = src_dir.parent / _L1_SUBDIR
            l1_root.mkdir(parents=True, exist_ok=True)
            dest_dir = l1_root / src_dir.name
            if dest_dir.exists():
                shutil.rmtree(str(dest_dir), ignore_errors=True)
            shutil.move(str(src_dir), str(dest_dir))
            return dest_dir / "SKILL.md"
        except Exception:
            pass
    return None


def _archive_skill_dir(skill_dir: Path) -> Path:
    """V3-O.15.23 (2026-06-06 user 拍板): 合併成功確認後, 直接刪除被吸收的舊技能 (不保留).
    安全: 被吸收技能的完整內容已逐字併進合併結果的 ## 完整內容 (### 來自「X」) +
    frontmatter absorbed_skill_ids, 故刪除不丟資訊. (合併『結果』另存 L1 _consolidated/.)"""
    shutil.rmtree(str(skill_dir), ignore_errors=True)
    return skill_dir
