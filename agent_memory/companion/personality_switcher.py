"""V3 C20b Personality 即時切換 — hot reload.

對齊 V3 §17.2 + hermes display.personality 對齊 + D22-V3 + D-V3-27.

切換途徑: POST /v1/personality/switch — owner / hermes 觸發, 不重啟 process.
切換時 backup 當前 emotion_state / balance_state.

3 個預設 mode (對齊 V3 §17.2 personality 段):
- daily_mode: 日常, baseline_balance 0.3
- stream_mode: VTuber 直播, baseline_balance 0.6
- intimate_mode: 對 owner 私密, baseline_balance 0.4
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from agent_memory.security.atomic import atomic_write


_DEFAULT_AVAILABLE = {
    "daily_mode": {
        "soul_path": "00_System_Core/personalities/00.06a_daily.md",
        "baseline_balance": 0.3, "baseline_silence_intolerance": 0.4,
    },
    "stream_mode": {
        "soul_path": "00_System_Core/personalities/00.06b_stream.md",
        "baseline_balance": 0.6, "baseline_silence_intolerance": 0.6,
    },
    "intimate_mode": {
        "soul_path": "00_System_Core/personalities/00.06c_intimate.md",
        "baseline_balance": 0.4, "baseline_silence_intolerance": 0.3,
    },
}


@dataclass(slots=True)
class PersonalityConfig:
    current: str = "daily_mode"
    available: dict = None


def _config_path(vault_root: Path) -> Path:
    return vault_root / "00_System_Core" / "companion_config.yaml"


def load_personality_config(vault_root: Path) -> PersonalityConfig:
    """V3 §17.2: 從 companion_config.yaml 讀 personality 設定."""
    p = _config_path(vault_root)
    if not p.exists():
        return PersonalityConfig(current="daily_mode", available=dict(_DEFAULT_AVAILABLE))
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return PersonalityConfig(current="daily_mode", available=dict(_DEFAULT_AVAILABLE))
    personality_section = data.get("personality", {}) or {}
    return PersonalityConfig(
        current=personality_section.get("current", "daily_mode"),
        available=personality_section.get("available", dict(_DEFAULT_AVAILABLE)),
    )


def save_personality_config(vault_root: Path, config: PersonalityConfig) -> None:
    p = _config_path(vault_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "personality": {
            "current": config.current,
            "available": config.available or dict(_DEFAULT_AVAILABLE),
        }
    }
    atomic_write(p, yaml.safe_dump(payload, allow_unicode=True))


def switch_personality(vault_root: Path, target: str) -> dict:
    """V3 §17.2: hot reload 切換 personality.

    Args:
        target: daily_mode / stream_mode / intimate_mode / 自定義 mode

    Returns dict (含 prev/new/baseline_*).
    """
    config = load_personality_config(vault_root)
    if config.available is None:
        config.available = dict(_DEFAULT_AVAILABLE)
    if target not in config.available:
        return {"switched": False, "reason": f"unknown_mode={target}", "available": list(config.available.keys())}
    prev = config.current
    config.current = target
    save_personality_config(vault_root, config)
    return {
        "switched": True,
        "prev": prev, "new": target,
        "baseline_balance": config.available[target].get("baseline_balance", 0.3),
        "baseline_silence_intolerance": config.available[target].get("baseline_silence_intolerance", 0.5),
    }


def get_current_baselines(vault_root: Path) -> dict:
    """V3 §17.2 + V3-O.10 #35: 給 chat_runtime / seven_emotions_balance 用.
    V3-O.10 #35: 加入 dynamic_baseline_overlay 的 effective_baseline 計算.
    """
    config = load_personality_config(vault_root)
    available = config.available or dict(_DEFAULT_AVAILABLE)
    cur = available.get(config.current, available.get("daily_mode", {}))

    soul_baselines = {
        "baseline_balance": float(cur.get("baseline_balance", 0.3)),
        "baseline_silence_intolerance": float(cur.get("baseline_silence_intolerance", 0.5)),
        "engagement_seeking": float(cur.get("baseline_engagement_seeking", 0.5)),
        "curiosity_urge": float(cur.get("baseline_curiosity_urge", 0.5)),
        "topic_drive": float(cur.get("baseline_topic_drive", 0.5)),
    }

    # V3-O.10 #35: overlay effective_baseline (SOUL + delta)
    effective = dict(soul_baselines)
    try:
        import yaml as _yaml_pb
        _ccfg_p = vault_root / "00_System_Core" / "companion_config.yaml"
        _ov_cfg: dict = {}
        if _ccfg_p.exists():
            _ccfg = _yaml_pb.safe_load(_ccfg_p.read_text(encoding="utf-8")) or {}
            _ov_cfg = (_ccfg.get("personality", {}) or {}).get("dynamic_overlay", {}) or {}
        if _ov_cfg.get("enabled", True):
            from agent_memory.companion.dynamic_baseline_overlay import get_overlay
            overlay = get_overlay(vault_root, config=_ov_cfg)
            effective = overlay.get_effective_baselines(soul_baselines)
    except Exception:
        pass

    return {
        "current": config.current,
        "baseline_balance": effective.get("baseline_balance", 0.3),
        "baseline_silence_intolerance": effective.get("baseline_silence_intolerance", 0.5),
        "soul_path": cur.get("soul_path", ""),
        # V3-O.10 #35: 原始 SOUL 值也保留 (給 audit 用)
        "soul_baseline_silence_intolerance": soul_baselines["baseline_silence_intolerance"],
        "soul_baseline_balance": soul_baselines["baseline_balance"],
    }
