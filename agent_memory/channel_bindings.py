"""Channel/persona binding helpers for multi-transport chat routing."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

_BINDINGS_RELATIVE_PATH = "00_System/08_Runtime_Profiles/channel_bindings.yaml"
_ID_RE = re.compile(r"[^a-zA-Z0-9._:@/+-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(raw: str, *, fallback: str) -> str:
    cleaned = _ID_RE.sub("-", raw.strip()).strip("-").lower()
    return cleaned or fallback


def _channel_key(transport: str, channel_id: str) -> str:
    return f"{_normalize_text(transport, fallback='transport')}:{_normalize_text(channel_id, fallback='channel')}"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def ensure_channel_bindings_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / _BINDINGS_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target

    payload: dict[str, Any] = {
        "schema_version": 1,
        "description": "多入口 channel 綁定 persona。key 格式：<transport>:<channel_id>",
        "default_persona": "core",
        "bindings": {},
        "updated_at": _now_iso(),
    }
    atomic_write(target, _dump_yaml(payload))
    return target


def load_channel_bindings(vault_root: Path) -> dict[str, Any]:
    path = ensure_channel_bindings_file(vault_root)
    payload = _load_yaml(path)
    if not payload:
        payload = {
            "schema_version": 1,
            "default_persona": "core",
            "bindings": {},
            "updated_at": _now_iso(),
        }
    if not isinstance(payload.get("bindings"), dict):
        payload["bindings"] = {}
    if not str(payload.get("default_persona", "")).strip():
        payload["default_persona"] = "core"
    return payload


def save_channel_bindings(vault_root: Path, payload: dict[str, Any]) -> Path:
    path = ensure_channel_bindings_file(vault_root)
    if not isinstance(payload.get("bindings"), dict):
        payload["bindings"] = {}
    payload["updated_at"] = _now_iso()
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(payload))
    return path


def bind_channel_persona(
    vault_root: Path,
    *,
    transport: str,
    channel_id: str,
    persona_id: str,
    operator: str = "user",
) -> tuple[Path, str]:
    payload = load_channel_bindings(vault_root)
    bindings = payload["bindings"]
    key = _channel_key(transport, channel_id)
    bindings[key] = {
        "transport": _normalize_text(transport, fallback="transport"),
        "channel_id": _normalize_text(channel_id, fallback="channel"),
        "persona_id": _normalize_text(persona_id, fallback="core"),
        "operator": _normalize_text(operator, fallback="user"),
        "updated_at": _now_iso(),
    }
    path = save_channel_bindings(vault_root, payload)
    return path, key


def set_default_persona(
    vault_root: Path,
    *,
    persona_id: str,
    operator: str = "user",
) -> tuple[Path, str]:
    payload = load_channel_bindings(vault_root)
    payload["default_persona"] = _normalize_text(persona_id, fallback="core")
    payload["default_operator"] = _normalize_text(operator, fallback="user")
    path = save_channel_bindings(vault_root, payload)
    return path, str(payload["default_persona"])


def unbind_channel(vault_root: Path, *, transport: str, channel_id: str) -> tuple[Path, str, bool]:
    payload = load_channel_bindings(vault_root)
    bindings = payload["bindings"]
    key = _channel_key(transport, channel_id)
    removed = key in bindings
    if removed:
        del bindings[key]
    path = save_channel_bindings(vault_root, payload)
    return path, key, removed


def resolve_channel_persona(
    vault_root: Path,
    *,
    transport: str,
    channel_id: str,
    fallback_persona: str = "core",
) -> str:
    payload = load_channel_bindings(vault_root)
    bindings = payload.get("bindings", {})
    if not isinstance(bindings, dict):
        bindings = {}
    key = _channel_key(transport, channel_id)
    item = bindings.get(key, {})
    if isinstance(item, dict):
        persona = str(item.get("persona_id", "")).strip()
        if persona:
            return persona

    default_persona = str(payload.get("default_persona", "")).strip()
    if default_persona:
        return default_persona
    return _normalize_text(fallback_persona, fallback="core")


def list_channel_bindings(vault_root: Path) -> dict[str, Any]:
    payload = load_channel_bindings(vault_root)
    bindings = payload.get("bindings", {})
    if not isinstance(bindings, dict):
        bindings = {}
    return {
        "default_persona": str(payload.get("default_persona", "core")),
        "bindings": bindings,
        "updated_at": str(payload.get("updated_at", "")),
    }
