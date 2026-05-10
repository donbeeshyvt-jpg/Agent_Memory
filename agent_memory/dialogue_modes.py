"""Dialogue mode profiles for persona-aware communication patterns."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.chat_session import sanitize_component
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

DIALOGUE_MODES_RELATIVE_PATH = "00_System/08_Runtime_Profiles/dialogue_modes.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "description": "對話模式設定。可依 persona/transport 指定預設溝通橋段。",
        "defaults": {
            "mode": "standard",
            "fallback_to_default": True,
        },
        "modes": {
            "standard": {
                "label": "標準執行",
                "prompt": (
                    "回覆時先給結論，再給可執行步驟。若資訊不足，先提出 1~2 個關鍵澄清問題。"
                ),
            },
            "coach": {
                "label": "教練引導",
                "prompt": (
                    "先用一句話定義目前狀態，再給最短下一步。"
                    "優先用簡短步驟引導使用者完成操作，不做過度理論化。"
                ),
            },
            "strategist": {
                "label": "策略規劃",
                "prompt": (
                    "先拆分目標、限制、風險，再給優先順序與取捨。"
                    "需要時用清單標示角色責任與交付條件。"
                ),
            },
            "executor": {
                "label": "工程執行",
                "prompt": (
                    "以交付為導向，直接提供可執行方案與驗證方式。"
                    "回覆要明確指出修改點、驗收命令與預期結果。"
                ),
            },
        },
        "persona_defaults": {
            "core": "standard",
            "manager": "strategist",
            "writer-curator": "coach",
            "coder": "executor",
        },
        "transport_defaults": {
            "web": "standard",
            "discord": "standard",
            "line": "coach",
        },
        "updated_at": _now_iso(),
    }


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _normalize_mode_key(value: Any) -> str:
    return sanitize_component(str(value), fallback="").lower().strip()


def ensure_dialogue_modes_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / DIALOGUE_MODES_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    atomic_write(target, _dump_yaml(_default_config()))
    return target


def load_dialogue_modes(vault_root: Path) -> dict[str, Any]:
    path = ensure_dialogue_modes_file(vault_root)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        payload = {}

    defaults = payload.get("defaults", {})
    modes = payload.get("modes", {})
    persona_defaults = payload.get("persona_defaults", {})
    transport_defaults = payload.get("transport_defaults", {})

    if not isinstance(defaults, dict):
        defaults = {}
    if not isinstance(modes, dict):
        modes = {}
    if not isinstance(persona_defaults, dict):
        persona_defaults = {}
    if not isinstance(transport_defaults, dict):
        transport_defaults = {}

    normalized_modes: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in modes.items():
        mode_key = _normalize_mode_key(raw_key)
        if not mode_key:
            continue
        mode_payload = raw_value if isinstance(raw_value, dict) else {}
        normalized_modes[mode_key] = {
            "label": str(mode_payload.get("label", mode_key)).strip() or mode_key,
            "prompt": str(mode_payload.get("prompt", "")).strip(),
        }

    if not normalized_modes:
        fallback = _default_config()
        normalized_modes = dict(fallback.get("modes", {}))
        defaults = dict(fallback.get("defaults", {}))
        persona_defaults = dict(fallback.get("persona_defaults", {}))
        transport_defaults = dict(fallback.get("transport_defaults", {}))

    payload["defaults"] = defaults
    payload["modes"] = normalized_modes
    payload["persona_defaults"] = persona_defaults
    payload["transport_defaults"] = transport_defaults
    payload["updated_at"] = str(payload.get("updated_at", "")).strip() or _now_iso()
    return payload


def save_dialogue_modes(vault_root: Path, payload: dict[str, Any]) -> Path:
    path = ensure_dialogue_modes_file(vault_root)
    payload["updated_at"] = _now_iso()
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(payload))
    return path


def resolve_dialogue_mode(
    config: dict[str, Any],
    *,
    persona_id: str,
    transport: str,
    requested_mode: str | None = None,
) -> dict[str, str]:
    defaults = config.get("defaults", {})
    modes = config.get("modes", {})
    persona_defaults = config.get("persona_defaults", {})
    transport_defaults = config.get("transport_defaults", {})

    if not isinstance(defaults, dict):
        defaults = {}
    if not isinstance(modes, dict):
        modes = {}
    if not isinstance(persona_defaults, dict):
        persona_defaults = {}
    if not isinstance(transport_defaults, dict):
        transport_defaults = {}

    mode_keys = list(modes.keys())
    default_mode = _normalize_mode_key(defaults.get("mode")) or (mode_keys[0] if mode_keys else "standard")
    fallback_to_default = bool(defaults.get("fallback_to_default", True))

    def _pick_entry(mode_key: str, source: str) -> dict[str, str] | None:
        raw = modes.get(mode_key)
        if not isinstance(raw, dict):
            return None
        label = str(raw.get("label", mode_key)).strip() or mode_key
        prompt = str(raw.get("prompt", "")).strip()
        return {
            "mode": mode_key,
            "label": label,
            "prompt": prompt,
            "source": source,
        }

    requested = _normalize_mode_key(requested_mode or "")
    if requested:
        chosen = _pick_entry(requested, "requested")
        if chosen:
            return chosen
        if not fallback_to_default:
            raise ValueError(f"無效 dialogue_mode：{requested}")

    persona_key = _normalize_mode_key(persona_defaults.get(persona_id, ""))
    if persona_key:
        chosen = _pick_entry(persona_key, "persona_default")
        if chosen:
            return chosen

    transport_key = _normalize_mode_key(transport_defaults.get(transport, ""))
    if transport_key:
        chosen = _pick_entry(transport_key, "transport_default")
        if chosen:
            return chosen

    fallback_mode = default_mode if default_mode in modes else (mode_keys[0] if mode_keys else "standard")
    chosen = _pick_entry(fallback_mode, "global_default")
    if chosen:
        return chosen

    return {
        "mode": "standard",
        "label": "標準執行",
        "prompt": "",
        "source": "builtin_fallback",
    }
