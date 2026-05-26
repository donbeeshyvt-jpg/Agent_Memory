"""V3 C18b/C18c Companion Curator — 4 層流動節奏.

對齊 V3 §21 四層流動節奏 + §21.1 第 0 層 in-stream + §21.3-§21.5 + D-V3-37/38.

4 層 + 第 5 層 (§21.7):
0. in-stream micro-curator (每 5/30 turn): 強情緒即時升中 + emotion 衰減 + active_goals reminder
1. 22-step pipeline (對應 companion_chat_runtime)
2. live_ended hook (即時): 七情大幅衰減 + episodic batch 升中 + emotional_arc 抽取
3. 24h medium: self_modification heavy + 觀眾分層完整升降 + 親密度重算
4. 7d deep: LLM umbrella + Trait Evolution + drift guard + 90/180d decay + narrative arc

Phase 1 MVP: 純機械邏輯 (不靠 LLM); Phase 3 加 LLM consolidation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db
from agent_memory.companion.seven_emotions_balance import (
    EmotionState, decay_emotions, read_latest_emotion_state, write_emotion_state,
    BalanceState, decay_balance, read_latest_balance_state, write_balance_state,
)
from agent_memory.companion.affect_manager import AffectState
from agent_memory.companion.embodied_state import update_embodied_over_time, read_latest_embodied, write_embodied
from agent_memory.companion.multi_user_router import auto_promote_viewer_tier


@dataclass(slots=True)
class CuratorRunResult:
    layer: str = ""
    actions_performed: list[str] = field(default_factory=list)
    notes: str = ""


# ─── Layer 0: in-stream micro-curator ────────────────────────────────
def run_layer0_in_stream(
    vault_root: Path, session_id: str,
    *, all_user_ids: Optional[list[str]] = None,
) -> CuratorRunResult:
    """V3 §21.1 第 0 層: 每 5-30 turn 跑.

    動作 (對應 §21.1):
    - emotion_state 衰減 ×0.97 (high frequency)
    - balance_state 衰減 (主動 4 軸向 0.3 baseline)
    - 強情緒事件 |v|>0.7 即時升中 (大部分 chat_runtime Step 17 已做)
    - knowledge_gap priority 重排
    """
    actions = []
    for uid in (all_user_ids or []):
        emo = read_latest_emotion_state(vault_root, uid)
        if emo:
            decayed = decay_emotions(emo, rate=0.97)
            write_emotion_state(vault_root, uid, decayed, AffectState(), session_id=session_id)
            actions.append(f"emotion_decay({uid})")
        bal = read_latest_balance_state(vault_root, uid)
        if bal:
            decayed_b = decay_balance(bal, rate=0.95)
            write_balance_state(vault_root, uid, decayed_b)
            actions.append(f"balance_decay({uid})")

    # 強情緒事件即時升中 (已在 chat_runtime; 這裡保險再 scan)
    with open_companion_db(vault_root) as conn:
        promoted = conn.execute(
            "UPDATE episodic_memories SET lifecycle_state='mid' "
            "WHERE lifecycle_state='short' AND emotional_salience > 0.6"
        ).rowcount
        conn.commit()
    if promoted:
        actions.append(f"episodic_promote_to_mid({promoted})")

    return CuratorRunResult(layer="layer0_in_stream", actions_performed=actions)


# ─── Layer 2: live_ended hook (即時) ─────────────────────────────────
def run_layer2_live_ended(
    vault_root: Path, session_id: str,
    *, all_user_ids: Optional[list[str]] = None,
) -> CuratorRunResult:
    """V3 §21.3: 直播結束即時.

    - 七情大幅衰減 ×0.85 (向 baseline)
    - episodic batch 升中 (剩下沒升的)
    - session emotional_arc 抽取 (簡化版)
    """
    actions = []
    for uid in (all_user_ids or []):
        emo = read_latest_emotion_state(vault_root, uid)
        if emo:
            decayed = decay_emotions(emo, rate=0.85)
            write_emotion_state(vault_root, uid, decayed, AffectState(), session_id=session_id)
            actions.append(f"emotion_decay_strong({uid})")

    # batch 升中 (剩下強情緒沒升的)
    with open_companion_db(vault_root) as conn:
        promoted = conn.execute(
            "UPDATE episodic_memories SET lifecycle_state='mid' "
            "WHERE lifecycle_state='short' AND emotional_salience > 0.5"
        ).rowcount
        conn.commit()
    actions.append(f"episodic_batch_promote({promoted})")

    return CuratorRunResult(layer="layer2_live_ended", actions_performed=actions)


# ─── Layer 3: 24h medium ─────────────────────────────────────────────
def run_layer3_24h_medium(
    vault_root: Path,
    *, all_user_ids: Optional[list[str]] = None,
) -> CuratorRunResult:
    """V3 §21.4: 每天.

    - 親密度完整重算 + 自然衰減 (7d/30d/90d decay)
    - 觀眾分層完整升降 (含降級)
    - 強情緒事件 emotional_salience 重算
    - ⭐ V3-G5 (user 2026-05-27): LLM 摘要強情緒對話 → 寫 41_Daily_Knowledge
    """
    actions = []
    with open_companion_db(vault_root) as conn:
        # 觀眾分層升降
        rows = conn.execute("SELECT user_id, interaction_count, intimacy_score FROM intimacy_states").fetchall()
        for r in rows:
            promo = auto_promote_viewer_tier(
                vault_root, r["user_id"],
                interaction_count=r["interaction_count"],
                intimacy_score=r["intimacy_score"],
                in_stream_mode=False,  # 24h medium 不限只升不降
            )
            if promo:
                actions.append(f"tier_change({r['user_id']}: {promo})")

    # ⭐ V3-G5: 撈近 24h 強情緒事件 LLM 摘要 → 41_Daily_Knowledge (純 sleep cycle, 不影響 chat)
    daily_count = _consolidate_daily_knowledge(vault_root)
    if daily_count > 0:
        actions.append(f"daily_knowledge_consolidated({daily_count})")

    return CuratorRunResult(layer="layer3_24h_medium", actions_performed=actions)


def _consolidate_daily_knowledge(vault_root: Path) -> int:
    """V3-G5: 撈近 24h 強情緒 / 高 salience 事件 → LLM 摘要 → 寫 41_Daily_Knowledge.

    對齊 MISSION §5.4 「sleep cycle 該用 LLM」 + V3 §11.2 升格.
    純 background, 不影響 chat retrieve-time.

    Returns: 寫入的 daily_knowledge 數.
    """
    try:
        from agent_memory.companion.knowledge_base import write_daily_knowledge
        from agent_memory.llm_text_helpers import call_llm_for_text
        from agent_memory.llm_client import LLMClient
    except Exception:
        return 0

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            # 撈近 24h |valence| > 0.5 事件
            rows = conn.execute(
                "SELECT user_id, summary, valence, arousal, emotional_salience, memory_id "
                "FROM episodic_memories "
                "WHERE created_at > ? AND ABS(valence) > 0.5 AND lifecycle_state IN ('short','mid') "
                "ORDER BY ABS(valence) DESC, emotional_salience DESC LIMIT 5",
                (cutoff_24h,),
            ).fetchall()
    except Exception:
        return 0

    if not rows:
        return 0

    # LLM 摘要每筆 (Phase 1: 用 LLM helper; LLM 不可用時跳過)
    try:
        client = LLMClient(vault_root)
    except Exception:
        return 0

    written = 0
    for r in rows[:3]:  # 限 3 筆避免 LLM call 過多
        summary_input = (r["summary"] or "")[:300]
        if not summary_input.strip():
            continue
        prompt = (
            "你是夥伴大腦的 sleep cycle curator. 請把這段強情緒事件摘要成一句知識歸納 (≤80 字), "
            "格式: 「<topic>: <我學到的>」.\n\n"
            f"原文: {summary_input}\n"
            f"valence={r['valence']:.2f}, arousal={r['arousal']:.2f}\n\n"
            "輸出 (僅一行, 無解釋):"
        )
        try:
            result = call_llm_for_text(client, prompt, persona_id="companion", max_tokens=120)
            text = (result.text or "").strip()
        except Exception:
            continue
        if not text or ":" not in text and "：" not in text:
            continue
        # 抓 topic + claim
        sep = ":" if ":" in text else "："
        parts = text.split(sep, 1)
        if len(parts) != 2:
            continue
        topic = parts[0].strip()[:60]
        claim = parts[1].strip()[:200]
        if not topic or not claim:
            continue
        path = write_daily_knowledge(
            vault_root, topic, claim,
            source_event_ids=[r["memory_id"]],
            confidence=min(1.0, abs(r["valence"]) + 0.3),
            tags=["sleep_cycle", "daily"],
        )
        if path:
            written += 1
    return written


# ─── Layer 4: 7d deep ────────────────────────────────────────────────
def run_layer4_7d_deep(vault_root: Path) -> CuratorRunResult:
    """V3 §21.5: 每週 (LLM 介入).

    Phase 2 MVP: 純機械; Phase 3 / V3-G5 加 LLM umbrella + external_ingest.

    動作:
    - 長期 90d stale 標記 + 180d archive (簡化: 看 last_seen_at)
    - 極端情緒不降 (|v|>0.7)
    - ⭐ V3-G5: external_ingest_inbox LLM 摘要 → 42_External_Knowledge
    """
    actions = []
    with open_companion_db(vault_root) as conn:
        cutoff_180d = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        # 長期 episodic 90d 無命中 → archive (極端情緒 |v|>0.7 不降, D-V3-22)
        archived = conn.execute(
            "UPDATE episodic_memories SET lifecycle_state='archived' "
            "WHERE lifecycle_state='long' AND created_at < ? AND ABS(valence) < 0.7",
            (cutoff_180d,),
        ).rowcount
        conn.commit()
        actions.append(f"long_term_archive({archived})")

    # ⭐ V3-G5 (user 2026-05-27): 移植 V2 R10 external_ingest_summarize → companion 版
    # 對齊 MISSION §3.6 文獻吸收致用 + V3 §13.7 + V3-F4
    external_count = _ingest_external_knowledge(vault_root)
    if external_count > 0:
        actions.append(f"external_knowledge_ingested({external_count})")

    return CuratorRunResult(layer="layer4_7d_deep", actions_performed=actions)


def _ingest_external_knowledge(vault_root: Path) -> int:
    """V3-G5: 掃 42_External_Knowledge/_ingest_inbox/ → LLM 摘要 → 寫 42_External_Knowledge/<topic>.md.

    對齊 V2 R10 external_ingest_summarize.py pattern (移植 + companion 化).
    處理完的 file 移到 _ingest_inbox/_processed/ (避免重摘).

    Returns: 寫入的 external_knowledge 數.
    """
    try:
        from agent_memory.companion.knowledge_base import (
            write_external_knowledge, list_ingest_inbox, INGEST_INBOX_DIR,
        )
        from agent_memory.llm_text_helpers import call_llm_for_text
        from agent_memory.llm_client import LLMClient
    except Exception:
        return 0

    pending = list_ingest_inbox(vault_root)
    if not pending:
        return 0

    try:
        client = LLMClient(vault_root)
    except Exception:
        return 0

    processed_dir = vault_root / INGEST_INBOX_DIR / "_processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for src_file in pending[:5]:  # 限 5 檔避免 LLM call 過多
        if src_file.name.startswith("_") or src_file.is_dir():
            continue
        try:
            content = src_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not content.strip():
            continue
        # 截 8000 char 餵 LLM (避免 prompt overflow)
        content_trim = content[:8000]
        prompt = (
            "你是夥伴大腦的 sleep cycle curator. 請把這份外部文獻整理成知識條目, "
            "回傳格式如下 (僅這 3 行, 不加解釋):\n"
            "TOPIC: <一句話總結主題, ≤30 字>\n"
            "SUMMARY: <2-3 句摘要, ≤200 字>\n"
            "TAGS: <tag1,tag2,tag3>\n\n"
            f"文件原文 (來源: {src_file.name}):\n{content_trim}\n"
        )
        try:
            result = call_llm_for_text(client, prompt, persona_id="companion", max_tokens=400)
            text = (result.text or "").strip()
        except Exception:
            continue

        # 解析 LLM 輸出
        topic, summary, tags_str = "", "", ""
        for line in text.split("\n"):
            line_low = line.lower().strip()
            if line_low.startswith("topic:"):
                topic = line.split(":", 1)[1].strip()[:60]
            elif line_low.startswith("summary:"):
                summary = line.split(":", 1)[1].strip()[:400]
            elif line_low.startswith("tags:"):
                tags_str = line.split(":", 1)[1].strip()[:200]
        if not topic:
            topic = src_file.stem[:60]
        if not summary:
            summary = content_trim[:200]
        tags = [t.strip() for t in tags_str.split(",") if t.strip()][:5]
        if "external" not in tags:
            tags.append("external")

        path = write_external_knowledge(
            vault_root, topic, content_trim,
            source_path=src_file.relative_to(vault_root),
            summary=summary,
            confidence=0.85,
            tags=tags,
        )
        if path:
            # 移到 _processed/ 避免重摘
            try:
                target = processed_dir / src_file.name
                if target.exists():
                    target.unlink()
                src_file.rename(target)
            except Exception:
                pass
            written += 1
    return written
