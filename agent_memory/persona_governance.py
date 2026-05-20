"""Persona governance policy (supervision + capabilities)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

PERSONA_GOVERNANCE_RELATIVE_PATH = "00_System/08_Runtime_Profiles/persona_governance.yaml"

_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_ROLE_TYPE_ALIASES = {
    "tooling": "tooling",
    "tool": "tooling",
    "tools": "tooling",
    "builder": "tooling",
    "chat": "chat",
    "conversation": "chat",
    "emotive": "emotive",
    "emotion": "emotive",
    "emotional": "emotive",
}
_ROLE_TYPE_DEFAULT = "chat"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_id(raw: str, *, fallback: str) -> str:
    cleaned = _ID_RE.sub("-", str(raw).strip()).strip("-").lower()
    return cleaned or fallback


def normalize_role_type(raw: str | None, *, fallback: str = _ROLE_TYPE_DEFAULT) -> str:
    key = str(raw or "").strip().lower()
    if not key:
        return fallback
    return _ROLE_TYPE_ALIASES.get(key, fallback)


def _normalize_emotion(raw: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    profile = str(data.get("profile", fallback.get("profile", "none"))).strip() or "none"
    policy = str(data.get("policy", fallback.get("policy", "none"))).strip() or "none"
    return {
        "enabled": bool(data.get("enabled", fallback.get("enabled", False))),
        "profile": profile,
        "policy": policy,
    }


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _default_payload() -> dict[str, Any]:
    now = _now_iso()
    return {
        # R16 C68: schema_version 1 → 2 (加 memory_capture_enabled capability,
        # 對齊 MISSION §3.3 對話驅動雙向投餵 + V2_Round15 規格 §5.2)
        "schema_version": 2,
        "description": "人格治理策略：監督關係 + 工具能力。",
        "defaults": {
            "supervision": {
                "enabled": True,
                "reviewer_persona": "core",
                "arbiter_persona": "core",
            },
            "capabilities": {
                "tools_enabled": False,
                "code_write_enabled": False,
                "shell_enabled": False,
                "persona_management_enabled": False,
                # R16 C68: memory_capture 跟 tools_enabled 獨立, 預設 True 即使
                # tools_disabled persona 也能「記住記憶提醒」(對齊規格 §5.2 D2 拍板)
                "memory_capture_enabled": True,
            },
        },
        "first_persona_defaults": {
            "capabilities": {
                "tools_enabled": True,
                "code_write_enabled": True,
                "shell_enabled": True,
                "persona_management_enabled": True,
                "memory_capture_enabled": True,
            }
        },
        "persona_overrides": {
            "core": {
                "status": "active",
                "supervision": {
                    "enabled": False,
                    "reviewer_persona": "core",
                    "arbiter_persona": "core",
                },
                "capabilities": {
                    "tools_enabled": True,
                    "code_write_enabled": True,
                    "shell_enabled": True,
                    "persona_management_enabled": True,
                    "memory_capture_enabled": True,
                },
                "source": "system_core",
                "created_at": now,
                "updated_at": now,
                "updated_by": "system",
            }
        },
        "updated_at": now,
    }


def _normalize_supervision(raw: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    reviewer = _normalize_id(
        str(data.get("reviewer_persona", fallback.get("reviewer_persona", "core"))),
        fallback="core",
    )
    arbiter = _normalize_id(
        str(data.get("arbiter_persona", fallback.get("arbiter_persona", reviewer))),
        fallback=reviewer,
    )
    return {
        "enabled": bool(data.get("enabled", fallback.get("enabled", True))),
        "reviewer_persona": reviewer,
        "arbiter_persona": arbiter,
    }


def _normalize_capabilities(raw: Any, fallback: dict[str, Any]) -> dict[str, bool]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "tools_enabled": bool(data.get("tools_enabled", fallback.get("tools_enabled", False))),
        "code_write_enabled": bool(data.get("code_write_enabled", fallback.get("code_write_enabled", False))),
        "shell_enabled": bool(data.get("shell_enabled", fallback.get("shell_enabled", False))),
        "persona_management_enabled": bool(
            data.get("persona_management_enabled", fallback.get("persona_management_enabled", False))
        ),
        # R16 C68: memory_capture_enabled — backward-compat default True 即使 raw
        # / fallback 兩邊都沒這欄位 (舊 schema_version=1 vault 升級時自動補 True).
        # 對應規格 §5.2「除非 persona 顯式禁用」+ §3.1 對話驅動不需 menu.
        "memory_capture_enabled": bool(
            data.get("memory_capture_enabled", fallback.get("memory_capture_enabled", True))
        ),
    }


def ensure_persona_governance_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / PERSONA_GOVERNANCE_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    atomic_write(target, _dump_yaml(_default_payload()))
    return target


def load_persona_governance(vault_root: Path) -> dict[str, Any]:
    path = ensure_persona_governance_file(vault_root)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        payload = {}
    fallback = _default_payload()

    if not isinstance(payload.get("defaults"), dict):
        payload["defaults"] = fallback["defaults"]
    if not isinstance(payload.get("first_persona_defaults"), dict):
        payload["first_persona_defaults"] = fallback["first_persona_defaults"]
    if not isinstance(payload.get("persona_overrides"), dict):
        payload["persona_overrides"] = fallback["persona_overrides"]
    if "schema_version" not in payload:
        payload["schema_version"] = fallback["schema_version"]
    if "description" not in payload:
        payload["description"] = fallback["description"]
    if "updated_at" not in payload:
        payload["updated_at"] = fallback["updated_at"]
    return payload


def save_persona_governance(vault_root: Path, payload: dict[str, Any]) -> Path:
    path = ensure_persona_governance_file(vault_root)
    if not isinstance(payload.get("persona_overrides"), dict):
        payload["persona_overrides"] = {}
    payload["updated_at"] = _now_iso()
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(payload))
    return path


def resolve_persona_governance(config: dict[str, Any], *, persona_id: str) -> dict[str, Any]:
    pid = _normalize_id(persona_id, fallback="core")
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    super_defaults = _normalize_supervision(
        defaults.get("supervision", {}),
        {
            "enabled": True,
            "reviewer_persona": "core",
            "arbiter_persona": "core",
        },
    )
    cap_defaults = _normalize_capabilities(
        defaults.get("capabilities", {}),
        {
            "tools_enabled": False,
            "code_write_enabled": False,
            "shell_enabled": False,
            "persona_management_enabled": False,
        },
    )
    overrides = config.get("persona_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    entry = overrides.get(pid, {})
    if not isinstance(entry, dict):
        entry = {}

    supervision = _normalize_supervision(entry.get("supervision", {}), super_defaults)
    capabilities = _normalize_capabilities(entry.get("capabilities", {}), cap_defaults)
    if not capabilities["tools_enabled"]:
        capabilities["code_write_enabled"] = False
        capabilities["shell_enabled"] = False
        capabilities["persona_management_enabled"] = False
        # R16 C68: memory_capture_enabled **不**在這 block — 跟 tools_enabled 獨立.
        # tools_disabled persona 也能聰明接住「幫我記得 X」記憶提醒 (規格 §5.2 D2).
    return {
        "persona_id": pid,
        "status": str(entry.get("status", "active")),
        "supervision": supervision,
        "capabilities": capabilities,
        "source": str(entry.get("source", "defaults")),
        "updated_at": str(entry.get("updated_at", "")),
        "updated_by": str(entry.get("updated_by", "")),
    }


def upsert_persona_governance(
    vault_root: Path,
    *,
    persona_id: str,
    operator: str = "user",
    supervision_enabled: bool | None = None,
    reviewer_persona: str | None = None,
    arbiter_persona: str | None = None,
    tools_enabled: bool | None = None,
    persona_management_enabled: bool | None = None,
    is_first_non_core: bool = False,
    source: str = "auto_on_approve",
) -> tuple[Path, dict[str, Any]]:
    pid = _normalize_id(persona_id, fallback="")
    if not pid:
        raise ValueError("persona_id 不可為空")

    payload = load_persona_governance(vault_root)
    defaults = payload.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}

    super_defaults = _normalize_supervision(
        defaults.get("supervision", {}),
        {
            "enabled": True,
            "reviewer_persona": "core",
            "arbiter_persona": "core",
        },
    )
    cap_defaults = _normalize_capabilities(
        defaults.get("capabilities", {}),
        {
            "tools_enabled": False,
            "code_write_enabled": False,
            "shell_enabled": False,
            "persona_management_enabled": False,
        },
    )

    first_defaults = payload.get("first_persona_defaults", {})
    if not isinstance(first_defaults, dict):
        first_defaults = {}
    first_cap_defaults = _normalize_capabilities(first_defaults.get("capabilities", {}), cap_defaults)

    overrides = payload.get("persona_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    existing = overrides.get(pid, {})
    if not isinstance(existing, dict):
        existing = {}

    existing_super = _normalize_supervision(existing.get("supervision", {}), super_defaults)
    existing_cap = _normalize_capabilities(existing.get("capabilities", {}), cap_defaults)

    chosen_super_enabled = (
        bool(supervision_enabled)
        if supervision_enabled is not None
        else bool(existing_super.get("enabled", super_defaults["enabled"]))
    )
    chosen_reviewer = _normalize_id(
        reviewer_persona or str(existing_super.get("reviewer_persona", super_defaults["reviewer_persona"])),
        fallback="core",
    )
    chosen_arbiter = _normalize_id(
        arbiter_persona or str(existing_super.get("arbiter_persona", super_defaults["arbiter_persona"])),
        fallback=chosen_reviewer,
    )

    if tools_enabled is None:
        if "capabilities" in existing:
            chosen_tools_enabled = bool(existing_cap.get("tools_enabled", False))
        else:
            chosen_tools_enabled = bool(
                first_cap_defaults["tools_enabled"] if is_first_non_core else cap_defaults["tools_enabled"]
            )
    else:
        chosen_tools_enabled = bool(tools_enabled)

    cap_base = first_cap_defaults if is_first_non_core else cap_defaults
    if "capabilities" in existing and tools_enabled is None:
        code_write_enabled = bool(existing_cap.get("code_write_enabled", cap_base["code_write_enabled"]))
        shell_enabled = bool(existing_cap.get("shell_enabled", cap_base["shell_enabled"]))
    else:
        code_write_enabled = bool(cap_base["code_write_enabled"]) or bool(chosen_tools_enabled)
        shell_enabled = bool(cap_base["shell_enabled"]) or bool(chosen_tools_enabled)

    if persona_management_enabled is None:
        if "capabilities" in existing:
            chosen_manage = bool(existing_cap.get("persona_management_enabled", cap_base["persona_management_enabled"]))
        else:
            chosen_manage = bool(
                first_cap_defaults["persona_management_enabled"]
                if is_first_non_core
                else cap_defaults["persona_management_enabled"]
            )
    else:
        chosen_manage = bool(persona_management_enabled)

    if not chosen_tools_enabled:
        code_write_enabled = False
        shell_enabled = False
        chosen_manage = False

    now = _now_iso()
    entry: dict[str, Any] = {
        "status": "active",
        "supervision": {
            "enabled": chosen_super_enabled,
            "reviewer_persona": chosen_reviewer,
            "arbiter_persona": chosen_arbiter,
        },
        "capabilities": {
            "tools_enabled": chosen_tools_enabled,
            "code_write_enabled": code_write_enabled,
            "shell_enabled": shell_enabled,
            "persona_management_enabled": chosen_manage,
        },
        "source": str(source or "auto_on_approve"),
        "created_at": str(existing.get("created_at", now)),
        "updated_at": now,
        "updated_by": _normalize_id(operator, fallback="user"),
    }

    if "disabled_at" in existing:
        entry.pop("disabled_at", None)
        entry.pop("disabled_by", None)
        entry.pop("disabled_reason", None)

    overrides[pid] = entry
    payload["persona_overrides"] = overrides
    path = save_persona_governance(vault_root, payload)
    return path, entry


def disable_persona_governance(
    vault_root: Path,
    *,
    persona_id: str,
    operator: str = "user",
    reason: str = "",
) -> tuple[Path, dict[str, Any]]:
    pid = _normalize_id(persona_id, fallback="")
    if not pid:
        raise ValueError("persona_id 不可為空")

    payload = load_persona_governance(vault_root)
    overrides = payload.get("persona_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
    existing = overrides.get(pid, {})
    if not isinstance(existing, dict):
        existing = {}

    now = _now_iso()
    fallback_entry = resolve_persona_governance(payload, persona_id=pid)
    capabilities = fallback_entry.get("capabilities", {})
    if not isinstance(capabilities, dict):
        capabilities = {}
    capabilities["tools_enabled"] = False
    capabilities["code_write_enabled"] = False
    capabilities["shell_enabled"] = False
    capabilities["persona_management_enabled"] = False

    entry = {
        "status": "disabled",
        "supervision": fallback_entry.get("supervision", {}),
        "capabilities": capabilities,
        "source": str(existing.get("source", "persona_disabled")),
        "created_at": str(existing.get("created_at", now)),
        "updated_at": now,
        "updated_by": _normalize_id(operator, fallback="user"),
        "disabled_at": now,
        "disabled_by": _normalize_id(operator, fallback="user"),
    }
    if reason.strip():
        entry["disabled_reason"] = reason.strip()

    overrides[pid] = entry
    payload["persona_overrides"] = overrides
    path = save_persona_governance(vault_root, payload)
    return path, entry
