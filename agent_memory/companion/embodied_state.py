"""V3 C18f Embodied State — §29.4 H4 身體感擬態.

對齊 V3 §29.4 + D-V3-30 (Phase 1 完整版, VTuber 沉浸感核心) + D31-V3.

模擬 energy / hunger / thirst / sleepiness / voice_strain.
隨直播時長自然消耗:
- energy -0.05 per 1h
- thirst +0.08 per 1h
- voice_strain +0.06 per 1h

影響:
- energy 低 → arousal baseline 降, valence 微負
- thirst 高 → tone 自然軟化 (suggestion 添加「喝水」)
- sleepiness 高 → 對話節奏變慢

Owner 互動可主動補充 (喝水 motion → thirst -0.3).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


@dataclass(slots=True)
class EmbodiedState:
    energy: float = 0.8
    hunger: float = 0.0
    thirst: float = 0.0
    sleepiness: float = 0.0
    voice_strain: float = 0.0
    stream_duration_minutes: int = 0
    triggered_state: str = ""

    def as_dict(self) -> dict:
        return {
            "energy": self.energy, "hunger": self.hunger,
            "thirst": self.thirst, "sleepiness": self.sleepiness,
            "voice_strain": self.voice_strain,
            "stream_duration_minutes": self.stream_duration_minutes,
            "triggered_state": self.triggered_state,
        }


def update_embodied_over_time(state: EmbodiedState, *, elapsed_minutes: int) -> EmbodiedState:
    """V3 §29.4: 直播時長消耗.

    每小時 energy -0.05 / thirst +0.08 / voice_strain +0.06 / sleepiness +0.04.
    """
    hours = elapsed_minutes / 60.0
    state.energy = max(0.0, state.energy - 0.05 * hours)
    state.thirst = min(1.0, state.thirst + 0.08 * hours)
    state.voice_strain = min(1.0, state.voice_strain + 0.06 * hours)
    state.sleepiness = min(1.0, state.sleepiness + 0.04 * hours)
    state.hunger = min(1.0, state.hunger + 0.03 * hours)
    state.stream_duration_minutes += elapsed_minutes
    # 觸發特殊 state
    if state.thirst > 0.7:
        state.triggered_state = "thirsty"
    elif state.energy < 0.3:
        state.triggered_state = "tired"
    elif state.voice_strain > 0.7:
        state.triggered_state = "voice_strained"
    elif state.sleepiness > 0.7:
        state.triggered_state = "sleepy"
    else:
        state.triggered_state = ""
    return state


def apply_action(state: EmbodiedState, action: str) -> EmbodiedState:
    """V3 §29.4: Owner 可主動補充. 例: drink_water → thirst -0.3."""
    if action == "drink_water":
        state.thirst = max(0.0, state.thirst - 0.3)
    elif action == "rest":
        state.energy = min(1.0, state.energy + 0.2)
        state.sleepiness = max(0.0, state.sleepiness - 0.2)
    elif action == "eat":
        state.hunger = max(0.0, state.hunger - 0.4)
    elif action == "voice_rest":
        state.voice_strain = max(0.0, state.voice_strain - 0.3)
    return state


def get_affect_modifier(state: EmbodiedState) -> dict:
    """V3 §29.4: embodied state 對 affect / tone 的修正建議."""
    mods = {}
    if state.energy < 0.4:
        mods["arousal_offset"] = -0.15
        mods["valence_offset"] = -0.05
    if state.thirst > 0.6:
        mods["tone_hint"] = "想喝水"
    if state.sleepiness > 0.6:
        mods["tone_hint"] = "想休息"
    return mods


def write_embodied(vault_root: Path, state: EmbodiedState, *, session_id: str = "") -> None:
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embodied_state (state_id, timestamp, energy, hunger, thirst, sleepiness, voice_strain, stream_duration_minutes, triggered_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(),
             state.energy, state.hunger, state.thirst, state.sleepiness,
             state.voice_strain, state.stream_duration_minutes, state.triggered_state),
        )
        conn.commit()


def read_latest_embodied(vault_root: Path) -> Optional[EmbodiedState]:
    with open_companion_db(vault_root) as conn:
        row = conn.execute(
            "SELECT energy, hunger, thirst, sleepiness, voice_strain, stream_duration_minutes, triggered_state FROM embodied_state ORDER BY timestamp DESC LIMIT 1",
        ).fetchone()
    if row is None:
        return None
    return EmbodiedState(**dict(row))
