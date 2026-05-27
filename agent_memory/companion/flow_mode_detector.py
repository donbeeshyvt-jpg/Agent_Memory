"""V3 C15c/C15d/C18g Flow Mode Detector + Handler — 4 流量模式自動切換.

對齊 V3 §26.2 流量極端情況 + D-V3-41~45.

4 模式 (D46-V3 / D50-V3 / D52-V3):
- burst_mode: ≥10 msg/60s OR chat_velocity ≥ 1.5 msg/sec
- normal_mode: 0.05-1.5 msg/sec
- dead_chat_mode: chat_velocity<0.05 ≥5min OR viewers=0 ≥10min
- owner_solo_mode: 唯一說話者=owner ≥5min

burst 行為 (D47-V3): Attention K=3 + batch appraisal + backlog
dead 行為 (D51-V3): LLM 頻率降 1/3 + Daydream 持續
owner_solo (D52-V3): 自動切 intimate_mode personality
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# 流量模式 thresholds (D46-V3 拍板)
_BURST_MIN_MSG_PER_60S = 10
_BURST_VELOCITY = 1.5
_DEAD_VELOCITY = 0.05
_DEAD_MIN_MINUTES = 10  # D50-V3
_OWNER_SOLO_MIN_MINUTES = 5  # D52-V3


@dataclass(slots=True)
class FlowModeContext:
    """流量偵測 input."""

    chat_velocity: float = 0.5  # msg/sec
    concurrent_viewers: int = 0
    minute_msg_count: int = 0
    last_message_at: Optional[str] = None  # ISO
    last_owner_message_at: Optional[str] = None
    sole_speaker_user_id: Optional[str] = None
    sole_speaker_owner: bool = False
    sole_speaker_duration_minutes: float = 0.0


def detect_flow_mode(ctx: FlowModeContext) -> str:
    """V3 §26.2.A: 流量分區判定. Returns 4 模式之一."""
    # owner_solo 最優先 (對 dead chat 變 owner_solo)
    if ctx.sole_speaker_owner and ctx.sole_speaker_duration_minutes >= _OWNER_SOLO_MIN_MINUTES:
        return "owner_solo_mode"

    # burst 偵測
    if ctx.chat_velocity >= _BURST_VELOCITY or ctx.minute_msg_count >= _BURST_MIN_MSG_PER_60S:
        return "burst_mode"

    # dead chat 偵測
    if ctx.chat_velocity < _DEAD_VELOCITY and ctx.concurrent_viewers <= 1:
        # 還要看 last_message_at 多久 (簡化: 直接判低 velocity)
        return "dead_chat_mode"

    return "normal_mode"


# ─── Mode-specific behavior modifiers ─────────────────────────────────
@dataclass(slots=True)
class FlowModeBehavior:
    """V3 §26.2.D: 各模式對 emotion / balance / proactive / LLM 的影響."""

    attention_top_k: int = 999  # normal 不限
    batch_appraisal: bool = False  # burst 才 batch
    silence_intolerance_cap: Optional[float] = None  # burst 強制 ≤ 0.2
    proactive_speech_enabled: bool = True
    llm_call_freq_ratio: float = 1.0  # dead chat 降 1/3 = 0.33
    personality_override: Optional[str] = None  # owner_solo 切 intimate
    daydream_externally_visible: bool = False  # dead chat → True 主舞台
    knowledge_gap_detect_enabled: bool = True  # burst 關
    self_modification_flush_enabled: bool = True  # burst 不 flush
    backlog_enabled: bool = False  # burst 才 backlog


def get_flow_mode_behavior(mode: str) -> FlowModeBehavior:
    """V3 §26.2.D: 每模式 behavior 設定."""
    if mode == "burst_mode":
        return FlowModeBehavior(
            attention_top_k=3,  # D34-V3
            batch_appraisal=True,
            silence_intolerance_cap=0.2,
            proactive_speech_enabled=False,
            llm_call_freq_ratio=1.0,  # burst LLM 跟 normal 一樣積極 (處理 top-K)
            knowledge_gap_detect_enabled=False,  # 資訊太多無法準
            self_modification_flush_enabled=False,
            backlog_enabled=True,
        )
    if mode == "dead_chat_mode":
        return FlowModeBehavior(
            attention_top_k=999,
            llm_call_freq_ratio=1.0 / 3.0,  # D51-V3 節電
            daydream_externally_visible=True,  # D-V3-45 主舞台
        )
    if mode == "owner_solo_mode":
        return FlowModeBehavior(
            attention_top_k=999,
            personality_override="intimate_mode",  # D52-V3
            llm_call_freq_ratio=1.0,
        )
    # normal_mode
    return FlowModeBehavior()


# ─── flow_mode_history persistence ────────────────────────────────────
def record_flow_mode_transition(
    vault_root: Path,
    session_id: str,
    new_mode: str,
    *,
    chat_velocity_avg: float = 0.0,
    concurrent_viewers_avg: int = 0,
    transition_reason: str = "",
    backlog_count: int = 0,
) -> str:
    """V3 §26.2.E: 寫 flow_mode_history 表 (給 audit + 之後反查)."""
    mode_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with open_companion_db(vault_root) as conn:
        # 關閉前一個 (若 session 內有 active 的)
        conn.execute(
            "UPDATE flow_mode_history SET ended_at=? WHERE session_id=? AND ended_at IS NULL",
            (now, session_id),
        )
        conn.execute(
            "INSERT INTO flow_mode_history (mode_id, session_id, mode, started_at, chat_velocity_avg, concurrent_viewers_avg, backlog_count, transition_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mode_id, session_id, new_mode, now, chat_velocity_avg, concurrent_viewers_avg, backlog_count, transition_reason),
        )
        conn.commit()
    return mode_id


def maybe_record_flow_mode_transition(
    vault_root: Path,
    session_id: str,
    new_mode: str,
    *,
    chat_velocity_avg: float = 0.0,
    concurrent_viewers_avg: int = 0,
    transition_reason: str = "",
) -> Optional[str]:
    """V3-H3 殘-06: 只在 transition (mode 變化) 才寫 flow_mode_history.

    對齊 V3 §26.2.E. 比對 session 內最近 active row 的 mode, 不同才 record.
    避免每 turn 寫 → DB 爆炸.

    Returns: 新寫的 mode_id 或 None (沒 transition).
    """
    if not session_id or not new_mode:
        return None
    try:
        with open_companion_db(vault_root) as conn:
            row = conn.execute(
                "SELECT mode FROM flow_mode_history WHERE session_id=? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row and row["mode"] == new_mode:
            return None  # 沒 transition, 不寫
    except Exception:
        return None
    try:
        return record_flow_mode_transition(
            vault_root, session_id, new_mode,
            chat_velocity_avg=chat_velocity_avg,
            concurrent_viewers_avg=concurrent_viewers_avg,
            transition_reason=transition_reason,
        )
    except Exception:
        return None


def list_flow_mode_history(vault_root: Path, session_id: str) -> list[dict]:
    """V3 §26.2.E: 列 session 內各模式切換歷史."""
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT mode_id, mode, started_at, ended_at, chat_velocity_avg, transition_reason FROM flow_mode_history WHERE session_id=? ORDER BY started_at ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]
