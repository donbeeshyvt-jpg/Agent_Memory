"""Transport profile config for line/discord/web inbound normalization."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

TRANSPORT_PROFILES_RELATIVE_PATH = "00_System/08_Runtime_Profiles/transport_profiles.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_profiles() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "description": "多媒介入口設定。只定義事件解析與預設 context/session，不限制角色行為。",
        "defaults": {
            "enabled": True,
            "use_binding": True,
            "context_template": "{transport}:{channel_id}",
            "session_template": "{transport}-{channel_id}",
            "parser": "generic",
        },
        "transports": {
            "web": {
                "enabled": True,
                "parser": "generic",
                "message_candidates": ["message", "text", "content"],
                "channel_candidates": ["channel_id", "conversation_id", "thread_id", "user_id"],
                "user_candidates": ["user_id", "author_id"],
            },
            "discord": {
                "enabled": True,
                "parser": "discord_message",
                "message_candidates": ["content", "message.content", "text"],
                "channel_candidates": ["channel_id", "message.channel_id", "thread_id"],
                "user_candidates": ["author.id", "user.id", "member.user.id"],
            },
            "line": {
                "enabled": True,
                "parser": "line_webhook",
                "message_candidates": ["events[0].message.text", "message", "text"],
                "channel_candidates": [
                    "events[0].source.groupId",
                    "events[0].source.roomId",
                    "events[0].source.userId",
                    "channel_id",
                    "user_id",
                ],
                "user_candidates": ["events[0].source.userId", "user_id"],
            },
        },
        "updated_at": _now_iso(),
    }


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def ensure_transport_profiles_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / TRANSPORT_PROFILES_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    payload = _default_profiles()
    atomic_write(target, _dump_yaml(payload))
    return target


def load_transport_profiles(vault_root: Path) -> dict[str, Any]:
    path = ensure_transport_profiles_file(vault_root)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        payload = {}
    defaults = payload.get("defaults", {})
    transports = payload.get("transports", {})
    if not isinstance(defaults, dict):
        defaults = {}
    if not isinstance(transports, dict):
        transports = {}
    payload["defaults"] = defaults
    payload["transports"] = transports
    payload["updated_at"] = str(payload.get("updated_at", "")).strip() or _now_iso()
    return payload


def save_transport_profiles(vault_root: Path, payload: dict[str, Any]) -> Path:
    path = ensure_transport_profiles_file(vault_root)
    payload["updated_at"] = _now_iso()
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(payload))
    return path


def resolve_transport_profile(config: dict[str, Any], transport: str) -> dict[str, Any]:
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    transports = config.get("transports", {})
    if not isinstance(transports, dict):
        transports = {}
    raw = transports.get(transport, {})
    if not isinstance(raw, dict):
        raw = {}

    profile = dict(defaults)
    profile.update(raw)
    profile["transport"] = transport
    profile["enabled"] = bool(profile.get("enabled", True))
    profile["use_binding"] = bool(profile.get("use_binding", True))
    profile["parser"] = str(profile.get("parser", "generic")).strip() or "generic"
    return profile
