# -*- coding: utf-8 -*-
"""V3-O.14 C1: Teaching Intent Detector — owner 教冬比新概念 → 累積 evidence → 升 skill.

對齊:
- user 2026-06-05 設計討論:
  「覺得正在被教的時候 判定一下 → 連續對這個概念教了 3 次以上 → 升級成技能」
  「技能要寫上是誰哪時候教的 + 適合 RAG 檢索格式」
- V3-K4 skill_learning_loop.py 原本只走 semantic→skill, 但 episodic semantic 升格被 val>0.3 卡死.
  此 detector 是並行管道: owner 直接教 → 不靠 valence, 走 evidence_count 累積.

流程:
  step 17.6 (chat pipeline) → detect_teaching_intent(message, recent_dialogue, llm) →
    {is_teaching, concept_id, concept_name, summary, why_skill_candidate}
  ↓ 是 teaching
  accumulate_evidence(concept_id, teacher_id, event_id) → skill_candidates 表 +1
  ↓ evidence_count >= 3
  promote_candidate_to_skill(candidate_id, llm) → register_skill (寫 SKILL.md)

設計關鍵:
- LLM 判斷 (不靠 keyword) — 通用任何 topic, 不只菜單.
- concept_id 用 canonicalized form (低 ascii / 中文 normalize), 防止「菜單」「店裡的菜單」算不同 concept.
- evidence_count >= 3 是預設, 可改 config (`teaching.promotion_threshold`).
- 只 owner 對話算 (路人教不算, 避免 prompt injection 注入假 skill).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─── DB schema (skill_candidates) ────────────────────────────────────────
SKILL_CANDIDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_candidates (
    candidate_id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,           -- canonical, e.g. "menu_management"
    concept_name TEXT NOT NULL,         -- 自然語 e.g. "菜單管理"
    teacher_user_id TEXT NOT NULL,
    teacher_display_name TEXT,
    summary TEXT,                       -- 1-3 句精煉, LLM 給
    evidence_count INTEGER DEFAULT 1,
    evidence_event_ids TEXT,            -- JSON list
    first_seen_at TEXT NOT NULL,
    last_reinforced_at TEXT NOT NULL,
    status TEXT DEFAULT 'working',      -- working / promoting / promoted / rejected
    promotion_threshold INTEGER DEFAULT 3,
    promoted_skill_id TEXT,             -- 升格後填 skill_id
    promoted_at TEXT,
    notes TEXT,
    UNIQUE (concept_id, teacher_user_id)
)
"""


def ensure_skill_candidates_schema(conn) -> None:
    """確保 skill_candidates 表存在. 由 companion_db.ensure_companion_db 或本檔內呼叫."""
    conn.execute(SKILL_CANDIDATES_SCHEMA)


# ─── concept canonicalization ────────────────────────────────────────────
def canonicalize_concept(name: str) -> str:
    """概念名 → canonical id (kebab-case, 限長).

    「菜單管理」 → "菜單管理"
    「Menu Management」 → "menu-management"
    「店裡的菜單系統」 → "店裡菜單系統"
    """
    if not name:
        return ""
    cleaned = name.strip().lower()
    # 中文虛字 (字內也算, 中文沒 word boundary)
    for w in ["的", "了", "在", "是"]:
        cleaned = cleaned.replace(w, "")
    # 英文虛字 (word boundary, 避免把字母從詞中間挖掉, 例 "management" 不該變 "mngement")
    cleaned = re.sub(r"\b(to|the|a|an|of|in|on|for)\b", "", cleaned)
    cleaned = re.sub(r"[^\w一-鿿_-]+", "-", cleaned)
    cleaned = cleaned.strip("-")[:60]
    return cleaned or "untitled"


# ─── LLM teaching intent detection ───────────────────────────────────────
def detect_teaching_intent(
    *, user_message: str, recent_dialogue_excerpt: str,
    is_owner: bool, llm_client,
    timeout_seconds: float = 60.0,
) -> Optional[dict]:
    """V3-O.14: LLM 判斷「owner 是否正在教 bot 新概念」.

    Args:
        user_message: 當前 owner 訊息
        recent_dialogue_excerpt: 近 3-5 turn 對話 (供 context)
        is_owner: 必須 True 才偵測 (viewer 不算)
        llm_client: sub_task LLM client (V4 Flash via OPENROUTER_API_KEY_SUBTASK)
        timeout_seconds: 60s

    Returns: None (非 teaching) 或 {
        "is_teaching": True,
        "concept_id": "menu_management",
        "concept_name": "菜單管理",
        "summary": "owner 在教 bot 如何維護一份遞增的菜單清單, 每加一道菜要驗證數量並重新整列",
        "confidence": 0.85,
    }
    """
    if not is_owner:
        return None
    if not user_message.strip():
        return None

    try:
        from agent_memory.llm_text_helpers import call_llm_for_text
    except Exception:
        return None

    prompt = (
        "你是夥伴大腦的 teaching-intent 偵測 sub_task.\n"
        "判斷『主人是否正在教夥伴一個可以重複套用的新概念/技能/流程』.\n"
        "教學的特徵: 主人解釋規則 / 給範例步驟 / 要夥伴記住一套做法 / 對既有做法做修正/擴充.\n"
        "非教學: 一般聊天 / 點餐 / 情緒交流 / 命令做單次動作.\n\n"
        f"近期對話:\n{recent_dialogue_excerpt[:1200]}\n\n"
        f"主人這一句:\n「{user_message[:500]}」\n\n"
        "輸出 (純 JSON, 不要 ```code fence```):\n"
        '{"is_teaching": true/false,'
        ' "concept_name": "<≤20字 中文/英文, 不是這次教什麼具體東西, 而是這個技能的「類別名稱」, 例 \\"菜單管理\\" / \\"客人檔案系統\\" / \\"應對挑釁話術\\">",'
        ' "summary": "<≤80字 摘要這個技能的核心>",'
        ' "confidence": 0.0~1.0}\n\n'
        "若 is_teaching=false 仍要給 concept_name=\"\", summary=\"\", confidence=0.0."
    )

    try:
        result = call_llm_for_text(
            llm_client, prompt,
            persona_id="companion",
            max_tokens=400,
            auxiliary="teaching_intent_detect",
        )
        text = (result.text or "").strip()
    except Exception:
        return None

    # 去掉 markdown code fence (LLM 偶爾無視 instruction)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except Exception:
        # LLM 沒守 JSON → 嘗試 regex 撈
        m_is = re.search(r'"is_teaching"\s*:\s*(true|false)', text)
        m_name = re.search(r'"concept_name"\s*:\s*"([^"]+)"', text)
        m_sum = re.search(r'"summary"\s*:\s*"([^"]+)"', text)
        m_conf = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
        if not (m_is and m_is.group(1) == "true" and m_name):
            return None
        data = {
            "is_teaching": True,
            "concept_name": m_name.group(1),
            "summary": m_sum.group(1) if m_sum else "",
            "confidence": float(m_conf.group(1)) if m_conf else 0.5,
        }

    if not data.get("is_teaching"):
        return None
    concept_name = (data.get("concept_name") or "").strip()
    if not concept_name:
        return None
    return {
        "is_teaching": True,
        "concept_id": canonicalize_concept(concept_name),
        "concept_name": concept_name,
        "summary": (data.get("summary") or "").strip()[:200],
        "confidence": float(data.get("confidence", 0.5)),
    }


# ─── evidence accumulation ───────────────────────────────────────────────
def accumulate_teaching_evidence(
    vault_root: Path,
    *,
    concept_id: str, concept_name: str,
    teacher_user_id: str, teacher_display_name: str,
    event_id: str, summary: str,
) -> dict:
    """V3-O.14: 累積 evidence 到 skill_candidates 表.

    Returns: {
        "candidate_id": ...,
        "evidence_count": N,
        "threshold": 3,
        "ready_to_promote": bool,
        "status": "working" | "promoting" | "promoted",
    }
    """
    from agent_memory.companion.companion_db import open_companion_db
    now_iso = datetime.now(timezone.utc).isoformat()

    with open_companion_db(vault_root) as conn:
        ensure_skill_candidates_schema(conn)
        # 查既有 candidate (concept_id + teacher_user_id 是 UNIQUE key)
        row = conn.execute(
            "SELECT candidate_id, evidence_count, evidence_event_ids, status, promotion_threshold "
            "FROM skill_candidates WHERE concept_id=? AND teacher_user_id=?",
            (concept_id, teacher_user_id),
        ).fetchone()

        if row is None:
            candidate_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO skill_candidates "
                "(candidate_id, concept_id, concept_name, teacher_user_id, teacher_display_name, "
                "summary, evidence_count, evidence_event_ids, first_seen_at, last_reinforced_at, "
                "status, promotion_threshold) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'working', 3)",
                (candidate_id, concept_id, concept_name, teacher_user_id, teacher_display_name,
                 summary, json.dumps([event_id]), now_iso, now_iso),
            )
            conn.commit()
            return {
                "candidate_id": candidate_id,
                "evidence_count": 1, "threshold": 3,
                "ready_to_promote": False, "status": "working",
            }
        # update 既有
        candidate_id = row["candidate_id"]
        new_count = int(row["evidence_count"] or 0) + 1
        threshold = int(row["promotion_threshold"] or 3)
        try:
            evt_ids = json.loads(row["evidence_event_ids"] or "[]")
            if not isinstance(evt_ids, list):
                evt_ids = []
        except Exception:
            evt_ids = []
        if event_id not in evt_ids:
            evt_ids.append(event_id)
        evt_ids = evt_ids[-10:]  # 限 10 條
        ready = new_count >= threshold and row["status"] == "working"
        new_status = "promoting" if ready else row["status"]
        conn.execute(
            "UPDATE skill_candidates SET evidence_count=?, evidence_event_ids=?, "
            "last_reinforced_at=?, summary=?, status=? WHERE candidate_id=?",
            (new_count, json.dumps(evt_ids), now_iso, summary or row[1], new_status,
             candidate_id),
        )
        conn.commit()
        return {
            "candidate_id": candidate_id,
            "evidence_count": new_count, "threshold": threshold,
            "ready_to_promote": ready, "status": new_status,
        }


def list_promotable_candidates(vault_root: Path) -> list[dict]:
    """列 status='promoting' 的 candidate, 給 promoter 用."""
    from agent_memory.companion.companion_db import open_companion_db
    with open_companion_db(vault_root) as conn:
        ensure_skill_candidates_schema(conn)
        rows = conn.execute(
            "SELECT candidate_id, concept_id, concept_name, teacher_user_id, teacher_display_name, "
            "summary, evidence_count, evidence_event_ids, first_seen_at, last_reinforced_at "
            "FROM skill_candidates WHERE status='promoting' ORDER BY last_reinforced_at DESC LIMIT 20"
        ).fetchall()
    out = []
    for r in rows:
        try:
            evt_ids = json.loads(r["evidence_event_ids"] or "[]")
        except Exception:
            evt_ids = []
        out.append({
            "candidate_id": r["candidate_id"],
            "concept_id": r["concept_id"],
            "concept_name": r["concept_name"],
            "teacher_user_id": r["teacher_user_id"],
            "teacher_display_name": r["teacher_display_name"],
            "summary": r["summary"] or "",
            "evidence_count": r["evidence_count"],
            "evidence_event_ids": evt_ids,
            "first_seen_at": r["first_seen_at"],
            "last_reinforced_at": r["last_reinforced_at"],
        })
    return out


def mark_candidate_promoted(vault_root: Path, candidate_id: str, skill_id: str) -> None:
    """記錄 promoted_skill_id, 防止重複 promote."""
    from agent_memory.companion.companion_db import open_companion_db
    now_iso = datetime.now(timezone.utc).isoformat()
    with open_companion_db(vault_root) as conn:
        ensure_skill_candidates_schema(conn)
        conn.execute(
            "UPDATE skill_candidates SET status='promoted', promoted_skill_id=?, promoted_at=? "
            "WHERE candidate_id=?",
            (skill_id, now_iso, candidate_id),
        )
        conn.commit()


# ─── promote candidate → SKILL.md ────────────────────────────────────────
def promote_candidate_to_skill(
    vault_root: Path,
    *,
    candidate: dict,
    llm_client,
) -> Optional[str]:
    """V3-O.14 C2: 升 candidate → 寫 50_Skills_Tools/<concept_id>/SKILL.md.

    Args:
        candidate: list_promotable_candidates 回的 dict
        llm_client: sub_task LLM

    Returns: skill_id (寫成功) 或 None (失敗).
    """
    from agent_memory.companion.skill_learning_loop import SkillRegistration, register_skill
    from agent_memory.companion.companion_db import open_companion_db

    # 撈 evidence event 全文
    evidence_texts = []
    if candidate.get("evidence_event_ids"):
        with open_companion_db(vault_root) as conn:
            placeholders = ",".join(["?"] * len(candidate["evidence_event_ids"]))
            rows = conn.execute(
                f"SELECT event_id, actor, content, created_at FROM raw_events "
                f"WHERE event_id IN ({placeholders}) ORDER BY created_at ASC",
                tuple(candidate["evidence_event_ids"]),
            ).fetchall()
        for r in rows:
            evidence_texts.append({
                "event_id": r["event_id"],
                "actor": r["actor"],
                "content": (r["content"] or "")[:300],
                "at": r["created_at"],
            })

    # LLM 提煉 procedure + trigger_keywords (給 RAG 撈)
    try:
        from agent_memory.llm_text_helpers import call_llm_for_text
        evidence_summary = "\n".join(
            f"[{e['at'][:19]}] {e['actor']}: {e['content']}"
            for e in evidence_texts[:5]
        )
        prompt = (
            "你是夥伴大腦的 skill consolidation curator.\n"
            "主人最近反覆教夥伴一個概念, 整理成正式技能寫進大腦.\n\n"
            f"概念名稱: {candidate['concept_name']}\n"
            f"摘要: {candidate.get('summary', '')}\n\n"
            f"原始對話 evidence:\n{evidence_summary[:2000]}\n\n"
            "輸出純 JSON (no code fence):\n"
            '{"trigger_situation": "<≤80字, 什麼情境下會用到, 給 RAG 撈時 embed 這段>",\n'
            ' "description": "<≤120字, 核心做法>",\n'
            ' "procedure_steps": ["<step1, ≤40字>", "<step2>", "<step3>"],\n'
            ' "trigger_keywords": ["<keyword1>", "<keyword2>", "..."]}\n'
        )
        result = call_llm_for_text(
            llm_client, prompt,
            persona_id="companion", max_tokens=600,
            auxiliary="skill_promotion",
        )
        text = (result.text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
    except Exception:
        # LLM 失敗 → 用 candidate.summary 當 fallback
        data = {
            "trigger_situation": candidate.get("summary", "")[:80],
            "description": candidate.get("summary", "")[:120],
            "procedure_steps": [],
            "trigger_keywords": [],
        }

    skill = SkillRegistration(
        skill_name=candidate["concept_name"],
        description=data.get("description", "")[:200],
        trigger_situation=data.get("trigger_situation", "")[:120],
        procedure_steps=[s for s in (data.get("procedure_steps") or []) if s][:5],
        emotional_origin=candidate.get("candidate_id", ""),
        success_rate=0.0,
        source="teaching_detector",
        # ⭐ V3-O.14 新增 metadata
        taught_by_user_id=candidate.get("teacher_user_id", ""),
        taught_by_name=candidate.get("teacher_display_name", ""),
        first_taught_at=candidate.get("first_seen_at", ""),
        last_reinforced_at=candidate.get("last_reinforced_at", ""),
        evidence_count=candidate.get("evidence_count", 0),
        evidence_event_ids=candidate.get("evidence_event_ids", []),
        trigger_keywords=[k for k in (data.get("trigger_keywords") or []) if k][:8],
        evidence_dialogues=evidence_texts[:3],
    )
    try:
        result = register_skill(vault_root, skill)
        skill_id = result.get("skill_id")
        if skill_id:
            mark_candidate_promoted(vault_root, candidate["candidate_id"], skill_id)
            return skill_id
    except Exception:
        pass
    return None
