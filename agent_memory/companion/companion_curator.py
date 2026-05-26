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
    """
    actions = []
    with open_companion_db(vault_root) as conn:
        # 親密度自然衰減 (簡化: 距離 last_interaction 超 7d → ×0.95)
        # Phase 3 完整版用 datetime 比對
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

    return CuratorRunResult(layer="layer3_24h_medium", actions_performed=actions)


# ─── Layer 4: 7d deep ────────────────────────────────────────────────
def run_layer4_7d_deep(vault_root: Path) -> CuratorRunResult:
    """V3 §21.5: 每週 (LLM 介入).

    Phase 2 MVP: 純機械; Phase 3 加 LLM umbrella / Trait Evolution / drift guard.

    動作:
    - 長期 90d stale 標記 + 180d archive (簡化: 看 last_seen_at)
    - 極端情緒不降 (|v|>0.7)
    """
    actions = []
    with open_companion_db(vault_root) as conn:
        # 90d stale (簡化邏輯: 看 last_seen_at 距今天)
        cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        cutoff_180d = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        # 長期 episodic 90d 無命中 → archive (極端情緒 |v|>0.7 不降, D-V3-22)
        archived = conn.execute(
            "UPDATE episodic_memories SET lifecycle_state='archived' "
            "WHERE lifecycle_state='long' AND created_at < ? AND ABS(valence) < 0.7",
            (cutoff_180d,),
        ).rowcount
        conn.commit()
        actions.append(f"long_term_archive({archived})")

    return CuratorRunResult(layer="layer4_7d_deep", actions_performed=actions)
