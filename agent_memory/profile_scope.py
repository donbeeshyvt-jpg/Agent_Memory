"""Helpers for loading runtime read/write scope from persona route files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_memory.chat_session import sanitize_component
from agent_memory.runtime import RuntimeProfile
from agent_memory.vault import ObsidianVaultAdapter


def normalize_scope_prefix(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return ""
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


def parse_scope_list(raw: Any, fallback: list[str]) -> list[str]:
    if not isinstance(raw, list):
        return list(fallback)
    values: list[str] = []
    for item in raw:
        normalized = normalize_scope_prefix(str(item))
        if normalized:
            values.append(normalized)
    if not values:
        return list(fallback)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def load_yaml_object(path: str) -> dict[str, Any]:
    try:
        abs_path = Path(path)
        if not abs_path.exists():
            return {}
        payload = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def runtime_profile_for_persona(adapter: ObsidianVaultAdapter, persona_id: str) -> RuntimeProfile:
    persona = sanitize_component(persona_id, fallback="core").lower()
    defaults = RuntimeProfile(name=persona)

    route_rel = f"00_System/08_Runtime_Profiles/routes/{persona}.yaml"
    route_abs = adapter.absolute_path(route_rel)
    route_payload = load_yaml_object(str(route_abs))
    if not route_payload and persona != "core":
        core_abs = adapter.absolute_path("00_System/08_Runtime_Profiles/routes/core.yaml")
        route_payload = load_yaml_object(str(core_abs))

    if not route_payload:
        return defaults

    memory_scope = route_payload.get("memory_scope", {})
    write_scope = route_payload.get("write_scope", {})
    if not isinstance(memory_scope, dict):
        memory_scope = {}
    if not isinstance(write_scope, dict):
        write_scope = {}

    return RuntimeProfile(
        name=persona,
        read_include=parse_scope_list(memory_scope.get("include"), defaults.read_include),
        read_exclude=parse_scope_list(memory_scope.get("exclude"), defaults.read_exclude),
        write_allow=parse_scope_list(write_scope.get("allow"), defaults.write_allow),
        write_deny=parse_scope_list(write_scope.get("deny"), defaults.write_deny),
    )
