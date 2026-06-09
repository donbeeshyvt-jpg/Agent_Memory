"""V3 C11c Memory Router — 4-layer + emotion_modulated_recall.

對齊 V3 §13 Mood-Congruent Memory Recall + Memory Router 4-Layer.

Layer 1 短期 hot cache (0.40 weight) — raw_events 近 10 min + working_memory
Layer 2 中期 episodic (0.35) — hybrid search + emotion_modulated_recall
Layer 3 長期 self/owner/narrative (0.20) — 00.07/00.08/active_goals/narrative
Layer 4 動態 (0.05) — Inside Jokes + knowledge_gap + GraphRAG 一跳

emotion_modulated_recall 公式 (V3 §13.1 D-V3-25):
  emotion_recall_score =
       0.30 × RAG_score
     + 0.25 × VAD_similarity
     + 0.15 × emotion_match_bonus
     + 0.10 × same_user_bonus
     + 0.10 × lifecycle_stage_weight
     + 0.05 × salience
     + 0.05 × inside_joke_bonus
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# Layer token budget (D-V3-23 對齊 R12 prompt cap 3000 char)
# V3-O.14 (2026-06-05): L3 600→1800 因 audit 補洞 — skill RAG 完整內容 + emotion/journal/preference md
LAYER1_BUDGET = 1200
LAYER2_BUDGET = 1050
LAYER3_BUDGET = 1800
LAYER4_BUDGET = 150


@dataclass(slots=True)
class MemoryHit:
    """memory_modulated_recall 結果 hit."""

    path: str = ""
    summary: str = ""
    base_rag_score: float = 0.0
    emotion_recall_score: float = 0.0
    lifecycle_state: str = "short"
    valence: float = 0.0
    arousal: float = 0.3
    dominance: float = 0.5
    dominant_emotion: str = "neutral"
    salience: float = 0.5
    user_id: str = ""
    tags: tuple[str, ...] = ()
    is_inside_joke: bool = False


@dataclass(slots=True)
class MemoryContext:
    """Memory Router 4 層融合輸出."""

    layer1_short: list[str] = field(default_factory=list)  # raw_events excerpts
    layer2_mid: list[MemoryHit] = field(default_factory=list)  # episodic recalls
    layer3_long: list[str] = field(default_factory=list)  # MEMORY / Owner_Profile / goals
    layer4_dynamic: list[MemoryHit] = field(default_factory=list)  # inside jokes + gap + graph
    total_char_estimate: int = 0
    rendered_memory_context: str = ""


def _cosine_3d(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """3D cosine similarity (對 VAD)."""
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    na = math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)
    nb = math.sqrt(b[0] ** 2 + b[1] ** 2 + b[2] ** 2)
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, dot / (na * nb))  # negative cosine → 0 (不要倒插)


def compute_emotion_recall_score(
    hit: MemoryHit,
    *,
    current_valence: float, current_arousal: float, current_dominance: float,
    current_dominant_emotion: str,
    user_id: str,
    balance_playfulness: float = 0.0,
) -> float:
    """V3 §13.1: 7 因子加權 emotion_recall_score."""
    # (a) VAD 相似度
    vad_sim = _cosine_3d(
        (current_valence, current_arousal, current_dominance),
        (hit.valence, hit.arousal, hit.dominance),
    )

    # (b) 七情匹配
    emotion_match_bonus = 0.3 if hit.dominant_emotion == current_dominant_emotion else 0.0

    # (c) same_user
    same_user_bonus = 0.2 if hit.user_id and hit.user_id == user_id else 0.0

    # (d) lifecycle stage weight
    stage_weight_map = {"short": 1.0, "mid": 0.8, "long": 0.6}
    stage_weight = stage_weight_map.get(hit.lifecycle_state, 0.5)
    # 極端情緒長期事件 boost
    if hit.lifecycle_state == "long" and abs(hit.valence) > 0.7:
        stage_weight += 0.3

    # (e) salience
    salience_w = hit.salience

    # (f) inside joke bonus (balance.playfulness > 0.5)
    inside_joke_bonus = 0.15 if (balance_playfulness > 0.5 and hit.is_inside_joke) else 0.0

    score = (
        0.30 * hit.base_rag_score
        + 0.25 * vad_sim
        + 0.15 * emotion_match_bonus
        + 0.10 * same_user_bonus
        + 0.10 * stage_weight
        + 0.05 * salience_w
        + 0.05 * inside_joke_bonus
    )
    return score


def emotion_modulated_recall(
    base_hits: list[MemoryHit],
    *,
    current_valence: float, current_arousal: float, current_dominance: float,
    current_dominant_emotion: str, user_id: str,
    balance_playfulness: float = 0.0,
    top_k: int = 5,
) -> list[MemoryHit]:
    """V3 §13.1: 對 base hits 重排, 取 top-K."""
    for h in base_hits:
        h.emotion_recall_score = compute_emotion_recall_score(
            h,
            current_valence=current_valence, current_arousal=current_arousal,
            current_dominance=current_dominance, current_dominant_emotion=current_dominant_emotion,
            user_id=user_id, balance_playfulness=balance_playfulness,
        )
    return sorted(base_hits, key=lambda x: x.emotion_recall_score, reverse=True)[:top_k]


# ─── Layer 1: 短期 hot cache (raw_events 近 10min) ─────────────────────────
def fetch_layer1_short(
    vault_root: Path, session_id: str, *, window_minutes: int = 10, max_items: int = 20,
) -> list[str]:
    """V3 §13.2 Layer 1: 從 raw_events 抓 session 內近 10min.
    V3-O.11+ (user 2026-06-01): max_items 8→20 — 直播統一場景，頻道最近 N 句(不分 user)。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT actor, content FROM raw_events WHERE session_id=? AND created_at>=? ORDER BY created_at DESC LIMIT ?",
            (session_id, cutoff, max_items),
        ).fetchall()
    return [f"[{r['actor']}] {r['content']}" for r in reversed(rows)]


# ─── Layer 2: 中期 episodic recall (emotion_modulated) ──────────────────
def fetch_layer2_episodic(
    vault_root: Path, user_id: str,
    *,
    current_valence: float = 0.0, current_arousal: float = 0.3, current_dominance: float = 0.5,
    current_dominant_emotion: str = "neutral",
    balance_playfulness: float = 0.0,
    top_k: int = 5,
) -> list[MemoryHit]:
    """V3 §13.2 Layer 2: 從 episodic_memories 抓相關 + emotion_modulated 重排."""
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT memory_id, summary, valence, arousal, dominance, salience, "
            "emotional_salience, lifecycle_state, user_id FROM episodic_memories "
            "WHERE (user_id=? OR user_id IS NULL) ORDER BY emotional_salience DESC, created_at DESC LIMIT 30",
            (user_id,),
        ).fetchall()
    base_hits: list[MemoryHit] = []
    for r in rows:
        h = MemoryHit(
            path=r["memory_id"],
            summary=r["summary"] or "",
            base_rag_score=(r["emotional_salience"] or 0.5),  # MVP: 用 emotional_salience 當 RAG proxy
            lifecycle_state=r["lifecycle_state"] or "short",
            valence=r["valence"] or 0.0,
            arousal=r["arousal"] or 0.3,
            dominance=r["dominance"] or 0.5,
            salience=r["salience"] or 0.5,
            user_id=r["user_id"] or "",
        )
        base_hits.append(h)
    return emotion_modulated_recall(
        base_hits,
        current_valence=current_valence, current_arousal=current_arousal,
        current_dominance=current_dominance, current_dominant_emotion=current_dominant_emotion,
        user_id=user_id, balance_playfulness=balance_playfulness,
        top_k=top_k,
    )


# ─── Layer 3: 長期 self/owner/narrative ─────────────────────────────────
def fetch_layer3_long(
    vault_root: Path, *,
    user_id: str = "", is_owner: bool = False,
    current_message: str = "",
    current_valence: float = 0.0,
) -> list[str]:
    """V3 §13.2 Layer 3: 抓 00.07/00.08/active_goals.

    V3-O.14 (2026-06-05): 補洞 — 加 skill RAG / 30_Emotional / 90_Daily_Journal / 60_Pref_md.
        current_message: 給 skill RAG hybrid_search 撈相關 skill 用 (空字串 fallback 最近 N 個).
        current_valence: 給情緒 md 決定要不要載 (|v|>0.4 才載, 避免太雜訊).
    """
    items: list[str] = []
    # 00.07 Companion MEMORY (always)
    p = vault_root / "00_System_Core" / "00.07_Companion_MEMORY.md"
    if p.exists():
        items.append(_excerpt_md(p, max_chars=200))
    # 00.08 Owner Profile (only if owner 對話)
    if is_owner:
        p2 = vault_root / "00_System_Core" / "00.08_Owner_Profile.md"
        if p2.exists():
            items.append(_excerpt_md(p2, max_chars=200))
    # active_goals
    from agent_memory.companion.active_goals import list_active_goals
    goals = list_active_goals(vault_root, target_audience=user_id)
    for g in goals[:3]:  # top 3
        items.append(f"goal: {g.description} (imp={g.importance})")
    # ⭐ V3-K2 (user 2026-05-27 「自我成長小孩」): semantic 自我提煉概念
    try:
        from agent_memory.companion.semantic_writer import list_recent_semantic_concepts
        concepts = list_recent_semantic_concepts(vault_root, user_id=user_id, max_count=3)
        for c in concepts:
            items.append(f"self_concept: {c['claim'][:120]} (conf={c['confidence']:.2f})")
    except Exception:
        pass
    # ⭐ V3-K3 (user 2026-05-27 「自我成長小孩」): narrative 自我敘事弧
    try:
        from agent_memory.companion.narrative_writer import list_recent_narratives
        narratives = list_recent_narratives(vault_root, user_id=user_id, max_count=2)
        for n in narratives:
            items.append(f"narrative: {n['theme']} — {n['relationship_arc'][:80]}")
    except Exception:
        pass
    # ⭐ V3-K4 + V3-O.14 C3 (user 2026-06-05): learned skills 完整內容 by RAG
    # 原本只回 50 字 metadata, 現在走 hybrid_search 給完整 trigger_situation + 步驟
    try:
        from agent_memory.companion.vault_md_search import retrieve_skills
        from agent_memory.companion.skill_learning_loop import list_recent_skills_summary
        skill_hits = []
        if current_message.strip():
            # ⭐ V3-O.15.33 (2026-06-09 user 拍板, 多次重申): RAG 撈到後「整張技能卡注入」, 不截.
            # 設計 = 前綴關鍵字+RAG 摘要約 5000 字 (撈用), 命中後整張 SKILL.md (上限 25000 字)
            # 全塞進 prompt 給 main_chat. 不傳 max_chars → 走 retrieve_skills 預設 25000.
            skill_hits = retrieve_skills(
                vault_root, current_message, top_k=3,
            )
        if not skill_hits:
            # fallback: 最近 N 個 metadata (relevancy 不足或無 query)
            for s in list_recent_skills_summary(vault_root, max_count=3):
                items.append(
                    f"skill: {s['skill_name']} (trigger: {s['trigger_situation'][:80]})"
                )
        else:
            for h in skill_hits:
                items.append(f"[skill {h['path']}]\n{h['content']}")
    except Exception:
        pass
    # ⭐ V3-O.14 audit 補洞: 30_Emotional_State md (val 高才載)
    if abs(current_valence) > 0.4:
        try:
            from agent_memory.companion.vault_md_search import retrieve_emotional_state
            for h in retrieve_emotional_state(vault_root, top_k=2, max_chars=200):
                items.append(f"[emo {h['path']}]\n{h['content']}")
        except Exception:
            pass
    # ⭐ V3-O.14 audit 補洞: 90_Daily_Journal 最近反思
    try:
        from agent_memory.companion.vault_md_search import retrieve_daily_journal
        for h in retrieve_daily_journal(vault_root, current_message, top_k=2, max_chars=300):
            items.append(f"[journal {h['path']}]\n{h['content']}")
    except Exception:
        pass
    # ⭐ V3-O.14 audit 補洞: 60_Preference_Memory md (語義偏好, 非 DB working preference)
    if current_message.strip():
        try:
            from agent_memory.companion.vault_md_search import retrieve_preferences_md
            for h in retrieve_preferences_md(vault_root, current_message, top_k=2, max_chars=250):
                items.append(f"[pref_md {h['path']}]\n{h['content']}")
        except Exception:
            pass
    return items


def _excerpt_md(path: Path, *, max_chars: int = 200) -> str:
    """從 .md 抓 body excerpt (strip frontmatter)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    # strip frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            text = text[end + 5 :]
    return text[:max_chars]


# ─── Layer 4: 動態元素 (Inside Jokes + knowledge_gap + GraphRAG) ─────────
def fetch_layer4_dynamic(
    vault_root: Path, user_id: str,
    *, balance_playfulness: float = 0.0, balance_curiosity_urge: float = 0.0,
    intimacy_score: float = 0.0, mode: str = "reactive",
) -> list[MemoryHit]:
    """V3 §13.2 Layer 4: 動態元素 (intimacy ≥ 0.4 + playfulness > 0.5 才開 Inside Jokes)."""
    items: list[MemoryHit] = []
    # Inside Jokes (intimacy>=0.4 + playfulness>0.5)
    if intimacy_score >= 0.4 and balance_playfulness > 0.5:
        # vault 20_Audience_Graph/23_Inside_Jokes/*.md (簡單 list)
        jokes_dir = vault_root / "20_Audience_Graph" / "23_Inside_Jokes"
        if jokes_dir.exists():
            for joke_file in list(jokes_dir.glob("*.md"))[:3]:
                items.append(MemoryHit(
                    path=str(joke_file.relative_to(vault_root)),
                    summary=_excerpt_md(joke_file, max_chars=120),
                    is_inside_joke=True, lifecycle_state="long",
                    user_id=user_id,
                ))
    # knowledge_gap pending (proactive mode 提權)
    if mode == "proactive" or balance_curiosity_urge > 0.5:
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT gap_id, entity, context_excerpt FROM knowledge_gap_state "
                "WHERE resolved=0 ORDER BY asked_count ASC, created_at DESC LIMIT 3",
            ).fetchall()
        for r in rows:
            items.append(MemoryHit(
                path=f"knowledge_gap:{r['gap_id']}",
                summary=f"unresolved: {r['entity']} ({r['context_excerpt'] or ''})",
                lifecycle_state="mid", salience=0.7,
            ))
    return items


def _truncate_to_budget(items: list[str], budget: int) -> tuple[list[str], int]:
    """V3 §13.2 token budget: truncate items 到 budget char."""
    out: list[str] = []
    used = 0
    for it in items:
        if used + len(it) > budget:
            remaining = budget - used
            if remaining > 50:
                out.append(it[:remaining] + "...")
            break
        out.append(it)
        used += len(it)
    return out, used


def build_memory_context(
    vault_root: Path,
    *, session_id: str = "", user_id: str = "",
    current_valence: float = 0.0, current_arousal: float = 0.3, current_dominance: float = 0.5,
    current_dominant_emotion: str = "neutral",
    balance_playfulness: float = 0.0, balance_curiosity_urge: float = 0.0,
    intimacy_score: float = 0.0, is_owner: bool = False,
    mode: str = "reactive",
    current_message: str = "",  # ⭐ V3-O.14: 給 L3 skill RAG hybrid_search 用
) -> MemoryContext:
    """V3 §13.2 主入口: 4 層融合, 套 token budget."""
    ctx = MemoryContext()

    # Layer 1
    l1 = fetch_layer1_short(vault_root, session_id) if session_id else []
    l1, l1_used = _truncate_to_budget(l1, LAYER1_BUDGET)
    ctx.layer1_short = l1

    # Layer 2
    l2 = fetch_layer2_episodic(
        vault_root, user_id,
        current_valence=current_valence, current_arousal=current_arousal,
        current_dominance=current_dominance, current_dominant_emotion=current_dominant_emotion,
        balance_playfulness=balance_playfulness, top_k=5,
    ) if user_id else []
    l2_strs = [f"[recall {h.path}] {h.summary[:120]}" for h in l2]
    l2_strs, l2_used = _truncate_to_budget(l2_strs, LAYER2_BUDGET)
    ctx.layer2_mid = l2

    # Layer 3 — V3-O.14 加 current_message + current_valence (skill RAG + emo md gate)
    l3 = fetch_layer3_long(
        vault_root, user_id=user_id, is_owner=is_owner,
        current_message=current_message, current_valence=current_valence,
    )
    l3, l3_used = _truncate_to_budget(l3, LAYER3_BUDGET)
    ctx.layer3_long = l3

    # Layer 4
    l4 = fetch_layer4_dynamic(
        vault_root, user_id,
        balance_playfulness=balance_playfulness, balance_curiosity_urge=balance_curiosity_urge,
        intimacy_score=intimacy_score, mode=mode,
    )
    l4_strs = [f"[dyn {h.path}] {h.summary[:80]}" for h in l4]
    l4_strs, l4_used = _truncate_to_budget(l4_strs, LAYER4_BUDGET)
    ctx.layer4_dynamic = l4

    ctx.total_char_estimate = l1_used + l2_used + l3_used + l4_used
    ctx.rendered_memory_context = _render(ctx, l2_strs, l4_strs)
    return ctx


def _render(ctx: MemoryContext, l2_strs: list[str], l4_strs: list[str]) -> str:
    """V3 §13.2: 渲染成 <memory-context> fence.

    V3-O.15 (2026-06-05 user 拍板): 40+50 區段 **永遠 emit** (即使無 hit),
    確保 LLM 永遠知道「我有外部知識 + 學過的技能可以調用, 只是這 turn 沒撈到」.
    """
    parts = ["<memory-context>"]
    if ctx.layer1_short:
        parts.append("# recent (短期)")
        parts.extend(ctx.layer1_short)
    if l2_strs:
        parts.append("# episodic recall (中期, mood-congruent)")
        parts.extend(l2_strs)
    if ctx.layer3_long:
        parts.append("# self/owner/goals (長期)")
        parts.extend(ctx.layer3_long)
    if l4_strs:
        parts.append("# dynamic")
        parts.extend(l4_strs)
    parts.append("</memory-context>")
    return "\n".join(parts)


def _build_friend_roster(vault_root) -> str:
    """V3-O.15.15 (2026-06-06 user 拍板): 輕量「朋友名冊」— 全體 metadata, 無相處紀錄內文.

    解 V3-O.15.6 朋友卡 RAG 只撈 top-3 的盲點: 被問「認識多少朋友 / 列出所有人 /
    最近跟誰最好」這類「全體 / 數量 / 排序」問題時, top-3 RAG 撈不全也數不出來.
    名冊查 intimacy_states ⨝ users (排除 owner + banned), 依親密度排, 極輕量
    (~數百字 vs 全卡 ~10 萬字). 內文仍由 retrieved_friend_cards 的 top-3 提供.
    """
    try:
        import sqlite3
        from agent_memory.companion.companion_db import get_companion_db_path
        db = get_companion_db_path(vault_root)
        if not db.exists():
            return ""
        owner_uid = ""
        try:
            from agent_memory.companion.companion_config import get_owner_user_id_for_transport
            owner_uid = get_owner_user_id_for_transport(vault_root, "discord") or ""
        except Exception:
            owner_uid = ""
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT i.user_id, COALESCE(u.display_name, i.user_id) AS name, "
                "i.interaction_count, i.intimacy_stage, i.intimacy_score, i.last_interaction_at "
                "FROM intimacy_states i LEFT JOIN users u ON u.user_id = i.user_id "
                "WHERE COALESCE(u.is_banned, 0) = 0 "
                "ORDER BY i.intimacy_score DESC, i.interaction_count DESC"
            ).fetchall()
        finally:
            conn.close()
        friends = [r for r in rows if str(r[0]) != str(owner_uid)]
        if not friends:
            return ""
        lines = [f"你目前認識 {len(friends)} 位朋友 (以下為完整名冊, 依親密度排序; 只含 metadata, 不含相處紀錄內文):"]
        for _uid, name, n, stage, score, last in friends:
            last10 = (last or "")[:10]
            try:
                score_s = round(float(score or 0), 2)
            except Exception:
                score_s = score
            lines.append(f"  - {name}｜互動 {n} 次｜親密度 {stage or '?'}（{score_s}）｜最近 {last10}")
        return "\n".join(lines)
    except Exception:
        return ""


def render_skills_and_knowledge_sections(
    vault_root, current_message: str = "",
) -> dict:
    """V3-O.15 + V3-O.15.6: 給 step15 prompt assembly 用的 40+50+20 section 內容.

    永遠 emit 結構 — 即使 0 hit 也回 stub.
    V3-O.15.6 加 retrieved_friend_cards (RAG 撈相關 viewer 朋友卡, 標明「查回來的」).

    Returns: {
        "learned_skills_relevant": str,
        "knowledge_base_relevant_hits": str,
        "retrieved_friend_cards": str,  # V3-O.15.6
    }
    """
    out = {
        "learned_skills_relevant": "(本輪未撈到相關技能, 但若情境符合可主動 callback 學過的東西)",
        "knowledge_base_relevant_hits": "(本輪未撈到相關外部知識, 若需要可主動表示需要查資料)",
        "retrieved_friend_cards": "(本輪未撈到相關朋友卡, 若你「記得」某人但內容模糊, 可能是因為他不在最近對話)",
        "friend_roster": "(本輪名冊未讀到)",
    }
    if not current_message or not vault_root:
        return out
    # 50 — learned skills (完整內容 RAG)
    try:
        from agent_memory.companion.vault_md_search import retrieve_skills
        hits = retrieve_skills(vault_root, current_message, top_k=3, max_chars=4000)  # V3-O.15.19: 2000→4000 讓 rich 段落更完整進 prompt
        if hits:
            lines = []
            for h in hits:
                lines.append(f"### {h['path']}\n{h['content']}")
            out["learned_skills_relevant"] = "\n\n".join(lines)
    except Exception:
        pass
    # 40 — external knowledge (完整 schema v12 內文 RAG)
    try:
        from agent_memory.companion.vault_md_search import retrieve_external_knowledge
        hits = retrieve_external_knowledge(vault_root, current_message, top_k=3, max_chars=2000)
        if hits:
            lines = []
            for h in hits:
                lines.append(f"### {h['path']}\n{h['content']}")
            out["knowledge_base_relevant_hits"] = "\n\n".join(lines)
    except Exception:
        pass
    # ⭐ V3-O.15.6: 20 — friend cards RAG (整張卡, 標明「查回來的」)
    try:
        from agent_memory.companion.vault_md_search import retrieve_friend_cards
        hits = retrieve_friend_cards(vault_root, current_message, top_k=3, max_chars=5000)
        if hits:
            lines = []
            for h in hits:
                lines.append(f"### {h['path']}\n{h['content']}")
            out["retrieved_friend_cards"] = "\n\n".join(lines)
    except Exception:
        pass
    # ⭐ V3-O.15.15: 20 — 朋友名冊 (全體 metadata, 無內文) — 答「幾個朋友/列全部/誰最熟」
    try:
        roster = _build_friend_roster(vault_root)
        if roster:
            out["friend_roster"] = roster
    except Exception:
        pass
    return out
