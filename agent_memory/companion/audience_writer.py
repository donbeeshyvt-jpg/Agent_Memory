# -*- coding: utf-8 -*-
"""V3-F1 (V3-E8 2026-05-27): 觀眾 profile markdown 自動寫入.

對齊:
- V3 §5 vault skeleton 雙寫設計 (DB + Markdown)
- V3 §10.5 / §10.6 VIP/Casual/Banned 分層
- V3 §13 Memory Router L3 self/owner→viewer 擴展
- V3-F_待做清單 F1
- user 2026-05-27 第 3 輪深度觀察 Q2+Q3 (觀眾應該有個別記憶塊)

每次 non-owner turn 在 chat_runtime Step 17.5 觸發:
- 寫 / 更新 20_Audience_Graph/{22_Casual_Viewers,21_VIP_Viewers,23_Banned_Viewers}/<user_id>.md
- frontmatter: loyalty_tier / intimacy_score / intimacy_stage / interaction_count / emotional_resonance_density / first_seen / updated_at
- body: 偏好觀察 (preference_memories) / 對話 highlight (近 5 turn raw_events) / 下次策略提示

owner 不寫 (已有 00.08_Owner_Profile.md, 對齊 V3-E5 動態讀).
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import threading
import queue as _queue_mod
import sys as _sys

from agent_memory.companion.companion_db import open_companion_db
from agent_memory.security.atomic import atomic_write


# ─── V3-O.11+ user 2026-06-03 修法 E: 朋友卡寫入 background serial queue ───
# 設計: 一個 worker thread FIFO 處理 (max_workers=1 等效), 避免 N viewer 並發
# 觸發 N × 2 個 LLM call (viewer_reflection + friend_card_consolidation) 同時打 LLM
# → lock contention / rate limit / 卡住主 chat flow.
# 改成: chat flow 立刻 enqueue + return, worker 後台一個一個 sequential 處理.
_AUDIENCE_QUEUE: _queue_mod.Queue = _queue_mod.Queue()
_AUDIENCE_WORKER_STARTED = False
_AUDIENCE_WORKER_LOCK = threading.Lock()


def _audience_worker_loop() -> None:
    """Background worker: FIFO drain queue, 一次處理一張卡 (serial)."""
    while True:
        try:
            item = _AUDIENCE_QUEUE.get()
            if item is None:  # poison pill (graceful shutdown, not used currently)
                break
            vault_root, user_id, display_name = item
            try:
                write_viewer_profile(vault_root, user_id, display_name=display_name)
            except Exception as exc:
                try:
                    print(f"[audience worker FAIL] uid={user_id[:18]} {type(exc).__name__}: {str(exc)[:200]}", file=_sys.stderr)
                except Exception:
                    pass
            finally:
                try:
                    _AUDIENCE_QUEUE.task_done()
                except Exception:
                    pass
        except Exception:
            pass  # 永不破 worker (continue loop)


def _ensure_audience_worker_started() -> None:
    """Singleton ensure: 第一次 enqueue 時起 daemon worker thread."""
    global _AUDIENCE_WORKER_STARTED
    with _AUDIENCE_WORKER_LOCK:
        if not _AUDIENCE_WORKER_STARTED:
            t = threading.Thread(
                target=_audience_worker_loop,
                daemon=True,
                name="audience-writer-worker",
            )
            t.start()
            _AUDIENCE_WORKER_STARTED = True


def enqueue_viewer_profile_write(vault_root: Path, user_id: str, display_name: str = "") -> int:
    """V3-O.11+ user 2026-06-03 修法 E: 朋友卡寫入進 background serial queue.

    Non-blocking — 立刻 return queue 內 pending 數量 (給 telemetry 看堆積).
    """
    _ensure_audience_worker_started()
    _AUDIENCE_QUEUE.put((vault_root, user_id, display_name))
    return _AUDIENCE_QUEUE.qsize()


CASUAL_DIR = "20_Audience_Graph/22_Casual_Viewers"
VIP_DIR = "20_Audience_Graph/21_VIP_Viewers"
BANNED_DIR = "20_Audience_Graph/23_Banned_Viewers"

MAX_HIGHLIGHTS = 8  # V3-O.15 (2026-06-05 user): 對話 highlight 5→8 pair (user+bot)
MAX_PREF_OBS = 10  # 偏好觀察 keep 10
MAX_DISPLAY_NAME_LEN = 80
MAX_CONTENT_PREVIEW = 80

# V3-O.11 階段3 朋友卡記憶層
# V3-O.15 (2026-06-05 user): 近期對話彙整 / 反思 字量 ×N — 拉到 5000 / 3000 字以
# 涵蓋整段豐富互動, 不再限於 2-3 句口頭式摘要.
MAX_CONSOLIDATION_TURNS = 20   # 彙整餵 LLM 的對話句數上限 (10→20)
MAX_REFLECTION_EVENTS = 8      # 反思餵 LLM 的高分事件數上限
MAX_CONSOLIDATION_OUTPUT_CHARS = 5000  # V3-O.15: 彙整段 md 上限字數
MAX_REFLECTION_OUTPUT_CHARS = 3000     # V3-O.15: 反思段 md 上限字數


def _llm_viewer_reflection(vault_root: Path, display_name: str, raw_block: str) -> str:
    """V3-O.11 階段3-1: 記憶模型對該 viewer 近期互動生成 2-3 句「我理解這個人是怎樣」反思.

    declarative facts 風格 (「這位觀眾傾向…」而非指令). 記憶模型 = local_gemma.
    LLM 不可用拋 Exception → caller try/except 跳過該段.
    """
    from agent_memory.llm_text_helpers import call_llm_for_text

    prompt = (
        "你是 精神體, 正在更新對某位『觀眾朋友』的理解 (寫進你私人的朋友卡, 觀眾看不到).\n"
        "請依據以下近期互動, 用『陳述事實 (declarative)』寫下『我理解這個人是怎樣』.\n"
        "風格要求: 用「這位觀眾傾向…」「他似乎…」「跟他相處時…」這種描述句, "
        "不要寫成對自己的指令 (不要『我應該…』『記得要…』).\n"
        "可以涵蓋: 性格 / 互動風格 / 偏好 / 雷點 / 跟我關係動態 / 觀察到的成長變化 / "
        "他對什麼話題會熱絡 / 跟其他觀眾的對比觀察 / 我對他的長期感受.\n"
        "第三人稱描述觀眾, 具體不流水帳, 不要前後說明.\n"
        "V3-O.15 (2026-06-05 user 拍板): 字數最多 3000 字, 寫深入豐富的多段反思, 不再限 2-3 句.\n\n"
        f"觀眾: {display_name}\n"
        f"近期互動:\n{raw_block}\n\n"
        "請直接輸出反思 (純文字, 可分段, 無 bullet 無標題, ≤3000 字):"
    )
    return call_llm_for_text(
        vault_root, prompt, persona_id="companion",
        temperature=0.4, timeout_s=60.0,  # V3-O.15: 60s 對齊統一 sub_task timeout
        auxiliary="viewer_reflection",
    )


def _llm_friend_card_consolidation(vault_root: Path, display_name: str, raw_block: str) -> str:
    """V3-O.11 階段3-2: 記憶模型把近 10 句對話壓縮成 2-3 句摘要 (取代逐句冗長).

    記憶模型 = local_gemma. LLM 不可用拋 Exception → caller try/except 跳過該段.
    """
    from agent_memory.llm_text_helpers import call_llm_for_text

    prompt = (
        "你是 精神體, 正在整理跟某位觀眾朋友的近期對話 (寫進你私人的朋友卡).\n"
        "請把以下對話彙整成詳細摘要, 涵蓋: 聊了什麼主題 / 各主題的具體內容 / "
        "觀眾的狀態或在意的事 / 互動氛圍 / 觀眾提出的問題或建議 / 我的回應重點 / "
        "對話走向脈絡 / 中間轉折點 / 任何值得日後記得的細節.\n"
        "可以分段, 保留具體例子, 不要逐句複述, 不要前後說明.\n"
        "V3-O.15 (2026-06-05 user 拍板): 字數最多 5000 字, 寫深入豐富的多段彙整, "
        "不再限 2-3 句 — 細節是用來下次對話時 callback 的, 越完整越好.\n\n"
        f"觀眾: {display_name}\n"
        f"近期對話:\n{raw_block}\n\n"
        "請直接輸出彙整 (純文字, 可分段, 無 bullet 無標題, ≤5000 字):"
    )
    return call_llm_for_text(
        vault_root, prompt, persona_id="companion",
        temperature=0.3, timeout_s=60.0,
        auxiliary="friend_card_consolidation",
    )


def _intimacy_stage(intimacy_score: float) -> str:
    """親密度階段 — V3-O.11 P4: 對齊 intimacy_state._STAGES 中文名 (與 DB 一致, score-only fallback)."""
    if intimacy_score >= 0.8:
        return "深度理解"
    elif intimacy_score >= 0.6:
        return "親密"
    elif intimacy_score >= 0.4:
        return "信任"
    elif intimacy_score >= 0.2:
        return "熟悉"
    return "初識"


def get_viewer_profile_path(vault_root: Path, user_id: str, loyalty_tier: str) -> Path:
    """根據 loyalty_tier 算出 markdown 路徑."""
    safe_uid = user_id.replace("/", "_").replace("\\", "_")[:120]
    if loyalty_tier == "vip":
        return vault_root / VIP_DIR / f"{safe_uid}.md"
    elif loyalty_tier == "banned":
        return vault_root / BANNED_DIR / f"{safe_uid}.md"
    return vault_root / CASUAL_DIR / f"{safe_uid}.md"


def write_viewer_profile(
    vault_root: Path,
    user_id: str,
    *,
    display_name: str = "",
) -> Optional[Path]:
    """V3-F1: 寫 / 更新 viewer profile markdown.

    撈 DB users + intimacy_states + preference_memories + raw_events + emotion_state,
    組裝 markdown + atomic write 到 20_Audience_Graph/<tier>/<user_id>.md.

    Returns: 寫入的檔案路徑 (失敗回 None, non-critical 不阻塞 chat).
    """
    if not user_id or user_id == "anonymous":
        return None

    try:
        with open_companion_db(vault_root) as conn:
            user_row = conn.execute(
                "SELECT user_id, display_name, role, loyalty_tier, is_banned, first_seen_at, last_seen_at FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if not user_row:
                return None  # user 不存在於 DB, 跳過

            intim_row = conn.execute(
                "SELECT interaction_count, intimacy_score, intimacy_stage, last_interaction_at FROM intimacy_states WHERE user_id=?",
                (user_id,),
            ).fetchone()

            # 撈最近 raw_events (該 user 自己的, 含 bot reply)
            highlights = conn.execute(
                "SELECT actor, content, created_at FROM raw_events "
                "WHERE user_id=? AND actor IN ('user','bot') "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, MAX_HIGHLIGHTS * 2),
            ).fetchall()

            # 撈 preference_memories (status active/proposed/verified)
            try:
                prefs = conn.execute(
                    "SELECT preference_type AS topic, claim, strength, status, last_seen_at AS updated_at FROM preference_memories "
                    "WHERE user_id=? AND status NOT IN ('rejected','expired') "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (user_id, MAX_PREF_OBS),
                ).fetchall()
            except Exception:
                prefs = []

            # 算 emotional_resonance_density (近 30 turn 強情緒 / 總 turn)
            try:
                total_30 = conn.execute(
                    "SELECT COUNT(*) AS c FROM raw_events WHERE user_id=? AND actor='user'",
                    (user_id,),
                ).fetchone()["c"] or 0
                strong_30 = conn.execute(
                    "SELECT COUNT(*) AS c FROM emotion_state "
                    "WHERE user_id=? AND (sadness > 0.5 OR anger > 0.5 OR fear > 0.5 OR love > 0.5 OR joy > 0.75)",
                    (user_id,),
                ).fetchone()["c"] or 0
                emo_density = (strong_30 / total_30) if total_30 > 0 else 0.0
            except Exception:
                emo_density = 0.0

            # V3-O.11 階段3-3 重要性加權: 撈高 emotional_salience 事件 (episodic_memories),
            # 用 intimacy_score 排序 helper. 優先保留高分項避免無限長 (取代純時間序的冗長).
            try:
                weighted_events = conn.execute(
                    "SELECT summary, valence, arousal, emotional_salience, created_at "
                    "FROM episodic_memories WHERE user_id=? AND summary IS NOT NULL AND summary != '' "
                    "ORDER BY emotional_salience DESC, ABS(valence) DESC, created_at DESC LIMIT ?",
                    (user_id, MAX_REFLECTION_EVENTS),
                ).fetchall()
            except Exception:
                weighted_events = []

            # 近 N 句對話 (user+bot 原文, 時間序), 給彙整段壓縮用
            try:
                recent_turns = conn.execute(
                    "SELECT actor, content, created_at FROM raw_events "
                    "WHERE user_id=? AND actor IN ('user','bot') AND content IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (user_id, MAX_CONSOLIDATION_TURNS),
                ).fetchall()
            except Exception:
                recent_turns = []

    except Exception:
        return None

    loyalty_tier = user_row["loyalty_tier"] or "casual"
    interaction_count = (intim_row["interaction_count"] if intim_row else 0) or 0
    intimacy_score = (intim_row["intimacy_score"] if intim_row else 0.0) or 0.0
    last_interaction = (intim_row["last_interaction_at"] if intim_row else None) or user_row["last_seen_at"]
    # V3-O.11 P4: 優先用 DB 已存的 intimacy_stage (intimacy_state.py 權威中文值), 與 DB 一致
    intim_stage = (intim_row["intimacy_stage"] if intim_row else "") or _intimacy_stage(intimacy_score)

    now_iso = datetime.now(timezone.utc).isoformat()
    final_name = (display_name or user_row["display_name"] or user_id)[:MAX_DISPLAY_NAME_LEN]

    profile_path = get_viewer_profile_path(vault_root, user_id, loyalty_tier)
    profile_path.parent.mkdir(parents=True, exist_ok=True)

    # V3-O.15.6 (2026-06-06 user 拍板): schema v10 → v12 對齊 SKILL/KB RAG 友善格式.
    # 加 trigger_keywords (display_name + alias 變化 + user_id) 給 FTS5 + substring 撈得到.
    # 朋友卡寫入 = overwrite, 自動把舊 v10 朋友卡升 v12 新格式.

    # 抽 trigger_keywords: display_name + alias 變化 + user_id + intimacy_stage
    trigger_keywords = []
    if final_name:
        trigger_keywords.append(final_name)
        # 拆字 (中文每 2-3 字一個 token 給 RAG 撈)
        if len(final_name) >= 2:
            trigger_keywords.append(final_name[:2])
    if user_id:
        trigger_keywords.append(user_id)
        # Discord mention 格式
        trigger_keywords.append(f"<@{user_id}>")
    # alias / nickname history (from users table)
    try:
        import json as _json_alias
        nh = user_row["nickname_history"] if "nickname_history" in user_row.keys() else None
        if nh:
            alias_list = _json_alias.loads(nh)
            for a in alias_list[-5:]:  # 近 5 個 alias
                _n = (a.get("name") or "").strip()
                if _n and _n not in trigger_keywords:
                    trigger_keywords.append(_n)
    except Exception:
        pass
    trigger_keywords = trigger_keywords[:10]

    # ─── 組裝 markdown ───
    lines = []
    lines.append("---")
    lines.append("type: friend_card")  # V3-O.15.6: viewer_profile → friend_card
    lines.append("schema_version: 12")  # V3-O.15.6: 10 → 12
    lines.append(f"user_id: {user_id}")
    lines.append(f"display_name: {final_name}")
    lines.append(f"title: {final_name}")  # alias for KB-style RAG query
    lines.append(f"contributor_user_id: \"{user_id}\"")
    lines.append(f"contributor_name: \"{final_name}\"")
    # Obsidian wikilink 回連自己 (給 backlink graph 用)
    lines.append(f"contributor_link: \"[[{user_id}|{final_name}]]\"")
    lines.append(f"loyalty_tier: {loyalty_tier}")
    lines.append(f"intimacy_score: {intimacy_score:.4f}")
    lines.append(f"intimacy_stage: {intim_stage}")
    lines.append(f"interaction_count: {interaction_count}")
    lines.append(f"emotional_resonance_density: {emo_density:.4f}")
    lines.append(f"last_interaction_at: {last_interaction}")
    lines.append(f"first_seen_at: {user_row['first_seen_at']}")
    lines.append(f"updated_at: {now_iso}")
    lines.append(f"role: {user_row['role']}")
    lines.append(f"trigger_keywords: {trigger_keywords}")  # V3-O.15.6: RAG 撈用
    lines.append("---")
    lines.append("")
    lines.append(f"# Viewer Profile — {final_name}")
    lines.append("")
    lines.append("## 我對這個觀眾的觀察 (auto)")
    lines.append("")
    lines.append(f"- loyalty: **{loyalty_tier}** / 親密度: **{intim_stage}** ({intimacy_score:.2f})")
    lines.append(f"- 互動次數: {interaction_count} turn")
    lines.append(f"- 情緒共鳴密度: {emo_density:.2%} (強情緒 turn 占比)")
    lines.append(f"- 首次見面: {user_row['first_seen_at']}")
    lines.append(f"- 最近互動: {last_interaction}")
    lines.append("")

    if prefs:
        lines.append("## 偏好觀察 (我學到的)")
        lines.append("")
        for p in prefs[:MAX_PREF_OBS]:
            strength = float(p["strength"] or 0.0)
            stars = "★" * max(1, int(round(strength * 5)))
            topic = (p["topic"] or "")[:30]
            claim = (p["claim"] or "")[:120].replace("\n", " ")
            status = p["status"] or "?"
            lines.append(f"- **{topic}**: {claim} ({stars} strength={strength:.2f}, status={status})")
        lines.append("")
    else:
        lines.append("## 偏好觀察 (我學到的)")
        lines.append("")
        lines.append("- (還沒累積到夠多 preference_memories)")
        lines.append("")

    if highlights:
        lines.append("## 對話 highlight (近 5 pair)")
        lines.append("")
        for h in highlights[:MAX_HIGHLIGHTS * 2]:
            time_short = (h["created_at"] or "")[:19]
            actor_label = "你" if h["actor"] == "user" else "我"
            content = (h["content"] or "")[:MAX_CONTENT_PREVIEW].replace("\n", " ")
            lines.append(f"- [{time_short}] {actor_label}: {content}")
        lines.append("")
    else:
        lines.append("## 對話 highlight (近 5 pair)")
        lines.append("")
        lines.append("- (還沒有對話紀錄)")
        lines.append("")

    # V3-O.10 #38: 情緒貢獻軌跡段 (per-viewer emotion impact tracking)
    try:
        from agent_memory.companion.companion_db import open_companion_db as _odb
        with _odb(vault_root) as _ec:
            _emo_rows = _ec.execute(
                "SELECT valence, arousal, dominant_emotion, timestamp FROM emotion_state "
                "WHERE user_id=? ORDER BY timestamp DESC LIMIT 5",
                (user_id,),
            ).fetchall()
        if _emo_rows:
            lines.append("## 情緒貢獻軌跡 (近 5 turn)")
            lines.append("")
            for _er in _emo_rows:
                _ts = (_er["timestamp"] or "")[:16]
                _v = float(_er["valence"] or 0.0)
                _a = float(_er["arousal"] or 0.0)
                _emo = _er["dominant_emotion"] or "neutral"
                _sign = "+" if _v >= 0 else ""
                lines.append(f"- [{_ts}] val={_sign}{_v:.2f} aro={_a:.2f} emo={_emo}")
            lines.append("")
    except Exception:
        pass

    # ─── V3-O.11 階段3-2: 近期對話彙整 (記憶模型壓縮近 10 句, 取代逐句冗長) ───
    # raw_events 時間序倒撈 → reverse 成正序餵 LLM. LLM 不可用 graceful 跳過該段.
    if recent_turns:
        turns_ordered = list(reversed(recent_turns))
        consolidation_block = "\n".join(
            f"  [{(t['actor'] == 'user') and '觀眾' or '我'}] "
            f"{(t['content'] or '')[:160].strip()}"
            for t in turns_ordered
            if (t["content"] or "").strip()
        )
        if consolidation_block.strip():
            try:
                summary_text = _llm_friend_card_consolidation(
                    vault_root, final_name, consolidation_block,
                ).strip()
            except Exception:
                summary_text = ""
            if summary_text:
                lines.append("## 近期對話彙整")
                lines.append("")
                lines.append(summary_text[:600])
                lines.append("")

    # ─── V3-O.11 階段3-1: 我對這位的理解 (反思) ───
    # 用高 emotional_salience 事件 (重要性加權) 為主, 不足時補近期對話原文.
    reflection_src_lines: list[str] = []
    for ev in (weighted_events or [])[:MAX_REFLECTION_EVENTS]:
        _s = (ev["summary"] or "").strip()
        if not _s:
            continue
        _v = float(ev["valence"] or 0.0)
        _sal = float(ev["emotional_salience"] or 0.0)
        reflection_src_lines.append(f"  - {_s[:160]} (情緒值={_v:+.2f}, 顯著度={_sal:.2f})")
    if not reflection_src_lines and recent_turns:
        # 沒有 episodic 事件 → 退回用近期對話原文當反思素材
        for t in reversed(recent_turns):
            _c = (t["content"] or "").strip()
            if not _c:
                continue
            _who = "觀眾" if t["actor"] == "user" else "我"
            reflection_src_lines.append(f"  - [{_who}] {_c[:160]}")
    if reflection_src_lines:
        reflection_block = "\n".join(reflection_src_lines[:MAX_REFLECTION_EVENTS])
        try:
            reflection_text = _llm_viewer_reflection(
                vault_root, final_name, reflection_block,
            ).strip()
        except Exception:
            reflection_text = ""
        if reflection_text:
            lines.append("## 我對這位的理解 (反思)")
            lines.append("")
            lines.append(reflection_text[:600])
            lines.append("")

    lines.append("## 我下次對這個觀眾的策略 (dispatcher hint)")
    lines.append("")
    if loyalty_tier == "banned":
        lines.append("- ⚠️ banned, 全拒回應")
    elif loyalty_tier == "vip":
        lines.append(f"- VIP 觀眾, intim={intimacy_score:.2f}, 可以熟一點, 引用過去 inside joke, 但仍不裝主人熟度")
    elif intimacy_score < 0.3:
        lines.append("- 初次/不熟, 保持禮貌 + 不深度共情 + 不裝熟 (對齊 V3 §27.2 紅線)")
    elif intimacy_score < 0.6:
        lines.append(f"- casual 認識中 ({intim_stage}), 可正常對話, 仍保留新鮮感")
    else:
        lines.append(f"- casual 但熟識 ({intim_stage}), 可正常對話 + 偶爾引用過去, 但不裝主人熟度")
    lines.append("")

    lines.append("---")
    lines.append(f"*Auto-generated by audience_writer.py V3-F1 ({now_iso[:19]})*")
    lines.append("*只有夥伴可以寫此檔, 中之人不該手動改 (除非清理).*")

    content_text = "\n".join(lines) + "\n"

    try:
        atomic_write(profile_path, content_text)
        return profile_path
    except Exception:
        return None


def load_viewer_profile_md(vault_root: Path, user_id: str) -> str:
    """V3-O.7 Round D (朋友卡 input 收束): 讀取 viewer profile markdown 給 LLM context 用.

    在 chat_runtime Step 14 prompt_packet 組裝時呼叫，把朋友卡注入 viewer_dynamic_context.
    先查 DB 確認 loyalty_tier，再讀對應路徑的 .md.
    回傳空字串表示尚無卡片 (新觀眾 / RC1 還沒跑過).
    """
    if not user_id or user_id in ("", "anonymous"):
        return ""
    try:
        with open_companion_db(vault_root) as conn:
            row = conn.execute(
                "SELECT loyalty_tier FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return ""
        tier = row["loyalty_tier"] or "casual"
        profile_path = get_viewer_profile_path(vault_root, user_id, tier)
        if profile_path.exists():
            return profile_path.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _active_viewer_ids(vault_root: Path, *, window_days: int, limit: int = 200) -> list[str]:
    """V3-O.11 階段3: 撈近 window_days 有互動的 non-owner viewer id (依 emotional_salience / intimacy 加權排序).

    對齊重要性加權: 先取近 N 天有 raw_events 的 user, 再按該 user 的 intimacy_score
    + 近期最高 emotional_salience 排序, 高分優先 (避免一次處理過多卡時 LLM call 爆量).
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT u.user_id AS uid, "
                "  COALESCE(i.intimacy_score, 0.0) AS intim, "
                "  COALESCE(MAX(e.emotional_salience), 0.0) AS sal "
                "FROM users u "
                "JOIN raw_events r ON r.user_id = u.user_id AND r.created_at > ? "
                "LEFT JOIN intimacy_states i ON i.user_id = u.user_id "
                "LEFT JOIN episodic_memories e ON e.user_id = u.user_id "
                "WHERE u.user_id IS NOT NULL AND u.user_id != 'anonymous' "
                "  AND (u.role IS NULL OR u.role != 'owner') "
                "GROUP BY u.user_id "
                "ORDER BY intim DESC, sal DESC "
                "LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [r["uid"] for r in rows if r["uid"]]
    except Exception:
        return []


def daily_refine_viewer_cards(vault_root: Path, *, max_cards: int = 30) -> int:
    """V3-O.11 階段3-4: 日重整 — 每日把各 viewer 朋友卡的反思昇華一次.

    對齊 companion_curator.run_layer3_24h_medium (24h medium) 節奏:
    撈近 24h 有互動的 viewer, 對每張卡 re-run write_viewer_profile —
    這會用記憶模型 (local_gemma) 重生『近期對話彙整』+『我對這位的理解 (反思)』段,
    等同把當天累積的互動昇華進反思. 重要性加權已在 write_viewer_profile 內處理.

    純 background (sleep cycle), 不影響 chat retrieve-time. LLM 不可用時各段 graceful 跳過.
    呼叫排程掛載點: companion_curator.run_layer3_24h_medium (見階段4/未來接線).

    Returns: 成功重整的卡片數 (寫檔成功計數).
    """
    viewer_ids = _active_viewer_ids(vault_root, window_days=1, limit=max_cards)
    refined = 0
    for uid in viewer_ids[:max_cards]:
        try:
            path = write_viewer_profile(vault_root, uid)
            if path is not None:
                refined += 1
        except Exception:
            continue  # 單張卡失敗不阻塞其餘
    return refined


def weekly_consolidate_viewer_cards(vault_root: Path, *, max_cards: int = 60) -> int:
    """V3-O.11 階段3-4: 7天總彙整 — 壓縮舊對話, 把一週互動整體再昇華.

    對齊 companion_curator.run_layer4_7d_deep (7d deep) 節奏:
    撈近 7d 有互動的 viewer (比 daily 更寬窗 + 更大 cap), 對每張卡 re-run
    write_viewer_profile — 記憶模型 (local_gemma) 會把累積對話重新壓縮成彙整段
    (取代逐句冗長) + 更新反思段, 達到「7天總彙整 / 壓縮舊對話」效果.

    純 background, 不影響 chat. LLM 不可用各段 graceful 跳過.
    呼叫排程掛載點: companion_curator.run_layer4_7d_deep (見階段4/未來接線).

    Returns: 成功彙整的卡片數.
    """
    viewer_ids = _active_viewer_ids(vault_root, window_days=7, limit=max_cards)
    consolidated = 0
    for uid in viewer_ids[:max_cards]:
        try:
            path = write_viewer_profile(vault_root, uid)
            if path is not None:
                consolidated += 1
        except Exception:
            continue
    return consolidated
