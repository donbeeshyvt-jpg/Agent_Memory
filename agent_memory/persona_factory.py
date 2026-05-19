"""Persona proposal/approval helpers for runtime profile isolation."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.persona_governance import (
    disable_persona_governance,
    load_persona_governance,
    resolve_persona_governance,
    upsert_persona_governance,
)
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.vault import ObsidianVaultAdapter

_RUNTIME_ROOT = "00_System/08_Runtime_Profiles"
_REGISTRY_REL = f"{_RUNTIME_ROOT}/registry.yaml"
_PERSONA_DIR_REL = f"{_RUNTIME_ROOT}/personas"
_ROUTE_DIR_REL = f"{_RUNTIME_ROOT}/routes"
_PROPOSAL_DIR_REL = f"{_RUNTIME_ROOT}/proposals"
_EVENT_LOG_REL = "11_AI_Mirror/ingestion_logs/persona_events.md"

_PERSONA_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_DEFAULT_READ_INCLUDE = [
    "00_System/",
    "10_Permanent/",
    "11_AI_Mirror/",
    "20_Literature/",
    "30_Programming/",
    "40_Gaming/",
    "50_Media/",
    "60_Other_Domains/",
    "70_Active_Plans/",
    "80_Fleeting/",
    "90_Daily_Journal/",
    "99_Archive/",
]
_DEFAULT_READ_EXCLUDE: list[str] = []
_DEFAULT_WRITE_ALLOW = [
    "00_System/Skills/",
    "10_Permanent/",
    "11_AI_Mirror/",
    "70_Active_Plans/",
]
_DEFAULT_WRITE_DENY = [
    "20_Literature/",
    "80_Fleeting/",
    "90_Daily_Journal/",
]
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
_ROLE_DEFAULT_MODE = {
    "tooling": "executor",
    "chat": "standard",
    "emotive": "coach",
}


def _normalize_role_type(raw: str | None, *, fallback: str = "chat") -> str:
    key = str(raw or "").strip().lower()
    if not key:
        return fallback
    return _ROLE_TYPE_ALIASES.get(key, fallback)


def _resolve_role_type_for_creation(
    *,
    role_type: str | None,
    tool_access_enabled: bool | None,
    allow_experimental_role: bool,
) -> str:
    inferred_fallback = "tooling" if bool(tool_access_enabled) else "chat"
    resolved = _normalize_role_type(role_type, fallback=inferred_fallback)
    if resolved == "emotive" and not allow_experimental_role:
        raise ValueError("role_type=emotive 目前開發中，入口不可選。請先使用 tooling 或 chat。")
    return resolved


def _resolve_role_type_for_update(
    *,
    current_role_type: str,
    role_type: str | None,
    allow_experimental_role: bool,
) -> str:
    resolved = _normalize_role_type(role_type, fallback=current_role_type)
    if resolved == "emotive" and not allow_experimental_role:
        raise ValueError("role_type=emotive 目前開發中，入口不可選。請先使用 tooling 或 chat。")
    return resolved


def _resolve_default_mode(
    *,
    role_type: str,
    explicit_default_mode: str | None,
    fallback: str = "standard",
) -> str:
    mode = str(explicit_default_mode or "").strip()
    if mode:
        return mode
    return _ROLE_DEFAULT_MODE.get(role_type, fallback)


def _resolve_tools_for_creation(*, role_type: str, requested: bool | None) -> bool | None:
    if role_type == "chat":
        if requested is True:
            raise ValueError("role_type=chat 不可啟用工具能力。請改用 role_type=tooling。")
        return False
    if role_type == "tooling":
        if requested is None:
            return True
        return bool(requested)
    if requested is None:
        return False
    return bool(requested)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _normalize_persona_id(raw: str, *, fallback: str = "persona") -> str:
    cleaned = _PERSONA_ID_RE.sub("-", raw.strip()).strip("-").lower()
    return cleaned or fallback


def _normalize_scope_list(values: list[str] | None, fallback: list[str]) -> list[str]:
    raw_values = values or []
    normalized: list[str] = []
    for item in raw_values:
        value = item.replace("\\", "/").strip().lstrip("/")
        if not value:
            continue
        if not value.endswith("/"):
            value += "/"
        normalized.append(value)
    if not normalized:
        return list(fallback)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in normalized:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _load_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _ensure_event_log(vault_root: Path) -> Path:
    path = (vault_root / _EVENT_LOG_REL).resolve()
    if path.exists():
        return path
    atomic_write(
        path,
        "# Persona Events\n\n"
        "- ?桃?嚗??犖?潭?獢???刻??遝鈭辣?n"
        "- 閬?嚗?撖思?隞嗅撣喉????港犖?潸酉?n",
    )
    return path


def _append_event(
    *,
    vault_root: Path,
    event_type: str,
    persona_id: str,
    operator: str,
    status: str,
    detail: str,
    proposal_id: str | None = None,
) -> None:
    log_path = _ensure_event_log(vault_root)
    stamp = _now_iso()
    lines = [
        f"## {stamp} [{event_type}]",
        f"- persona_id: `{persona_id}`",
        f"- operator: `{operator}`",
        f"- status: `{status}`",
    ]
    if proposal_id:
        lines.append(f"- proposal_id: `{proposal_id}`")
    lines.append(f"- detail: {detail}")
    block = "\n".join(lines) + "\n"

    with file_lock(log_path, timeout=5.0):
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        text = existing.rstrip() + "\n\n" + block
        atomic_write(log_path, text)


def _proposal_path(vault_root: Path, proposal_id: str) -> Path:
    return (vault_root / _PROPOSAL_DIR_REL / f"{proposal_id}.yaml").resolve()


def _registry_path(vault_root: Path) -> Path:
    return (vault_root / _REGISTRY_REL).resolve()


def _persona_path(vault_root: Path, persona_id: str) -> Path:
    return (vault_root / _PERSONA_DIR_REL / f"{persona_id}.md").resolve()


def _route_path(vault_root: Path, persona_id: str) -> Path:
    return (vault_root / _ROUTE_DIR_REL / f"{persona_id}.yaml").resolve()


def _ensure_parent_dirs(vault_root: Path) -> None:
    for rel in (_PERSONA_DIR_REL, _ROUTE_DIR_REL, _PROPOSAL_DIR_REL):
        (vault_root / rel).resolve().mkdir(parents=True, exist_ok=True)


def _persona_markdown(
    *,
    persona_id: str,
    display_name: str,
    mission: str,
    style: str,
    language: str,
    role_type: str,
    default_mode: str,
) -> str:
    frontmatter = {
        "type": "system",
        "persona_id": persona_id,
        "display_name": display_name,
        "mission": mission,
        "style": style,
        "language": language,
        "role_type": role_type,
        "default_mode": default_mode,
        "schema_version": 1,
        "status": "active",
        "updated_at": _now_iso(),
    }
    body = (
        f"# Persona: {display_name}\n\n"
        f"- mission: {mission}\n"
        f"- style: {style}\n"
        f"- language: {language}\n"
        "- ??嚗摰?route 銝剔? memory_scope / write_scope ?霈???n"
    )
    dumped = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---\n\n{body}"


def _route_payload(
    *,
    persona_id: str,
    default_mode: str,
    include: list[str],
    exclude: list[str],
    allow: list[str],
    deny: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "persona_id": persona_id,
        "default_mode": default_mode,
        "memory_scope": {
            "include": include,
            "exclude": exclude,
        },
        "write_scope": {
            "allow": allow,
            "deny": deny,
        },
        "guardrails": {
            "path_priority_over_metadata": True,
            "immutable_sources": [
                "20_Literature/",
                "80_Fleeting/",
                "90_Daily_Journal/",
            ],
        },
        "updated_at": _now_iso(),
    }


def create_persona_proposal(
    *,
    vault_root: Path,
    display_name: str,
    mission: str,
    style: str = "concise",
    language: str = "zh-Hant",
    default_mode: str | None = None,
    role_type: str | None = None,
    allow_experimental_role: bool = False,
    persona_id: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    tool_access_enabled: bool | None = None,
    operator: str = "user",
    auto_approve: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    _ensure_parent_dirs(root)

    display = display_name.strip()
    if not display:
        raise ValueError("display_name 不可為空")

    pid = _normalize_persona_id(persona_id or display, fallback="persona")
    if pid == "core":
        raise ValueError("persona_id=core 保留給系統核心人格")

    registry = _load_yaml_object(_registry_path(root))
    personas = registry.get("personas", {}) if isinstance(registry.get("personas", {}), dict) else {}
    if pid in personas and str(personas[pid].get("status", "active")) != "disabled":
        raise ValueError(f"persona 已存在：{pid}")

    include_paths = _normalize_scope_list(include, _DEFAULT_READ_INCLUDE)
    exclude_paths = _normalize_scope_list(exclude, _DEFAULT_READ_EXCLUDE)
    allow_paths = _normalize_scope_list(allow, _DEFAULT_WRITE_ALLOW)
    deny_paths = _normalize_scope_list(deny, _DEFAULT_WRITE_DENY)
    resolved_role_type = _resolve_role_type_for_creation(
        role_type=role_type,
        tool_access_enabled=tool_access_enabled,
        allow_experimental_role=allow_experimental_role,
    )
    resolved_default_mode = _resolve_default_mode(
        role_type=resolved_role_type,
        explicit_default_mode=default_mode,
        fallback="standard",
    )
    resolved_tools_enabled = _resolve_tools_for_creation(role_type=resolved_role_type, requested=tool_access_enabled)

    proposal_id = f"pf-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    proposal_abs = _proposal_path(root, proposal_id)
    payload = {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "status": "pending_approval",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "operator": operator,
        "persona": {
            "persona_id": pid,
            "display_name": display,
            "mission": mission.strip() or f"{display} 撠惇隞餃?鈭箸",
            "style": style.strip() or "concise",
            "language": language.strip() or "zh-Hant",
            "role_type": resolved_role_type,
        },
        "route": {
            "default_mode": resolved_default_mode,
            "memory_scope": {
                "include": include_paths,
                "exclude": exclude_paths,
            },
            "write_scope": {
                "allow": allow_paths,
                "deny": deny_paths,
            },
        },
        "require_user_approval": not auto_approve,
    }
    if resolved_tools_enabled is not None:
        payload["capabilities"] = {"tools_enabled": bool(resolved_tools_enabled)}
    atomic_write(proposal_abs, _dump_yaml(payload))

    _append_event(
        vault_root=root,
        event_type="create_proposal",
        persona_id=pid,
        operator=operator,
        status="pending_approval",
        detail=f"proposal 撱箇???`{proposal_abs}`",
        proposal_id=proposal_id,
    )

    if auto_approve:
        approved = approve_persona_proposal(
            vault_root=root,
            proposal_id=proposal_id,
            operator=operator,
            overwrite=False,
        )
        approved["proposal_path"] = str(proposal_abs)
        return approved

    return {
        "persona_id": pid,
        "role_type": resolved_role_type,
        "proposal_id": proposal_id,
        "status": "pending_approval",
        "proposal_path": str(proposal_abs),
    }


def approve_persona_proposal(
    *,
    vault_root: Path,
    proposal_id: str,
    operator: str = "user",
    overwrite: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    proposal_abs = _proposal_path(root, proposal_id)
    proposal = _load_yaml_object(proposal_abs)
    if not proposal:
        raise FileNotFoundError(f"找不到 proposal：{proposal_abs}")

    persona_cfg = proposal.get("persona", {})
    route_cfg = proposal.get("route", {})
    capabilities_cfg = proposal.get("capabilities", {})
    if not isinstance(persona_cfg, dict) or not isinstance(route_cfg, dict):
        raise ValueError("proposal 格式錯誤：缺少 persona/route")

    pid = _normalize_persona_id(str(persona_cfg.get("persona_id", "")), fallback="")
    if not pid:
        raise ValueError("proposal 缺少 persona_id")

    requested_tools_enabled: bool | None = None
    if isinstance(capabilities_cfg, dict) and "tools_enabled" in capabilities_cfg:
        requested_tools_enabled = bool(capabilities_cfg.get("tools_enabled"))
    resolved_role_type = _resolve_role_type_for_creation(
        role_type=str(persona_cfg.get("role_type", "")),
        tool_access_enabled=requested_tools_enabled,
        allow_experimental_role=True,
    )
    resolved_default_mode = _resolve_default_mode(
        role_type=resolved_role_type,
        explicit_default_mode=str(route_cfg.get("default_mode", "")),
        fallback="standard",
    )

    persona_abs = _persona_path(root, pid)
    route_abs = _route_path(root, pid)
    if not overwrite and (persona_abs.exists() or route_abs.exists()):
        raise ValueError(f"人格已存在：{pid}。如需覆蓋請加 --overwrite。")

    persona_text = _persona_markdown(
        persona_id=pid,
        display_name=str(persona_cfg.get("display_name", pid)),
        mission=str(persona_cfg.get("mission", f"{pid} 人格任務")),
        style=str(persona_cfg.get("style", "concise")),
        language=str(persona_cfg.get("language", "zh-Hant")),
        role_type=resolved_role_type,
        default_mode=resolved_default_mode,
    )
    route_payload = _route_payload(
        persona_id=pid,
        default_mode=resolved_default_mode,
        include=_normalize_scope_list(
            route_cfg.get("memory_scope", {}).get("include") if isinstance(route_cfg.get("memory_scope"), dict) else [],
            _DEFAULT_READ_INCLUDE,
        ),
        exclude=_normalize_scope_list(
            route_cfg.get("memory_scope", {}).get("exclude") if isinstance(route_cfg.get("memory_scope"), dict) else [],
            _DEFAULT_READ_EXCLUDE,
        ),
        allow=_normalize_scope_list(
            route_cfg.get("write_scope", {}).get("allow") if isinstance(route_cfg.get("write_scope"), dict) else [],
            _DEFAULT_WRITE_ALLOW,
        ),
        deny=_normalize_scope_list(
            route_cfg.get("write_scope", {}).get("deny") if isinstance(route_cfg.get("write_scope"), dict) else [],
            _DEFAULT_WRITE_DENY,
        ),
    )

    atomic_write(persona_abs, persona_text)
    atomic_write(route_abs, _dump_yaml(route_payload))

    is_first_non_core = False
    registry_abs = _registry_path(root)
    with file_lock(registry_abs, timeout=5.0):
        registry = _load_yaml_object(registry_abs)
        if not registry:
            registry = {"schema_version": 1, "default_persona": "core", "personas": {}}
        personas = registry.get("personas", {})
        if not isinstance(personas, dict):
            personas = {}
        active_non_core = [
            key
            for key, item in personas.items()
            if key != "core" and isinstance(item, dict) and str(item.get("status", "active")) != "disabled"
        ]
        is_first_non_core = pid != "core" and pid not in active_non_core and len(active_non_core) == 0

        personas[pid] = {
            "persona_path": f"personas/{pid}.md",
            "route_path": f"routes/{pid}.yaml",
            "role_type": resolved_role_type,
            "status": "active",
            "approved_at": _now_iso(),
            "approved_by": operator,
        }
        registry["personas"] = personas
        registry["updated_at"] = _now_iso()
        if not str(registry.get("default_persona", "")).strip():
            registry["default_persona"] = "core"
        atomic_write(registry_abs, _dump_yaml(registry))

    proposal["status"] = "approved"
    proposal["approved_at"] = _now_iso()
    proposal["approved_by"] = operator
    proposal["updated_at"] = _now_iso()
    atomic_write(proposal_abs, _dump_yaml(proposal))

    governance_path, governance_entry = upsert_persona_governance(
        root,
        persona_id=pid,
        operator=operator,
        tools_enabled=requested_tools_enabled,
        is_first_non_core=is_first_non_core,
        source="auto_on_approve",
    )

    _append_event(
        vault_root=root,
        event_type="approve_persona",
        persona_id=pid,
        operator=operator,
        status="approved",
        detail=(
            f"建立 personas/{pid}.md 與 routes/{pid}.yaml "
            f"role_type={resolved_role_type} "
            f"tools_enabled={governance_entry.get('capabilities', {}).get('tools_enabled', False)}"
        ),
        proposal_id=proposal_id,
    )

    return {
        "persona_id": pid,
        "proposal_id": proposal_id,
        "status": "active",
        "role_type": resolved_role_type,
        "persona_path": str(persona_abs),
        "route_path": str(route_abs),
        "registry_path": str(registry_abs),
        "governance_path": str(governance_path),
        "governance": governance_entry,
    }


def disable_persona(
    *,
    vault_root: Path,
    persona_id: str,
    operator: str = "user",
    reason: str = "",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    pid = _normalize_persona_id(persona_id, fallback="")
    if not pid:
        raise ValueError("persona_id 不可為空")
    if pid == "core":
        raise ValueError("不可停用 core 人格")

    registry_abs = _registry_path(root)
    with file_lock(registry_abs, timeout=5.0):
        registry = _load_yaml_object(registry_abs)
        personas = registry.get("personas", {}) if isinstance(registry.get("personas", {}), dict) else {}
        if pid not in personas:
            raise ValueError(f"persona 不存在：{pid}")
        entry = personas[pid]
        if not isinstance(entry, dict):
            entry = {}
        entry["status"] = "disabled"
        entry["disabled_at"] = _now_iso()
        entry["disabled_by"] = operator
        if reason.strip():
            entry["disabled_reason"] = reason.strip()
        personas[pid] = entry
        registry["personas"] = personas
        if str(registry.get("default_persona", "")).strip() == pid:
            registry["default_persona"] = "core"
        registry["updated_at"] = _now_iso()
        atomic_write(registry_abs, _dump_yaml(registry))

    persona_abs = _persona_path(root, pid)
    if persona_abs.exists():
        adapter = ObsidianVaultAdapter(root)
        metadata, body = adapter.parse_frontmatter(persona_abs.read_text(encoding="utf-8"))
        metadata["status"] = "disabled"
        metadata["updated_at"] = _now_iso()
        if reason.strip():
            metadata["disabled_reason"] = reason.strip()
        atomic_write(persona_abs, adapter.serialize_frontmatter(metadata, body))

    governance_path, governance_entry = disable_persona_governance(
        root,
        persona_id=pid,
        operator=operator,
        reason=reason,
    )

    _append_event(
        vault_root=root,
        event_type="disable_persona",
        persona_id=pid,
        operator=operator,
        status="disabled",
        detail=reason.strip() or "手動停用",
        proposal_id=None,
    )

    return {
        "persona_id": pid,
        "status": "disabled",
        "registry_path": str(registry_abs),
        "persona_path": str(persona_abs),
        "governance_path": str(governance_path),
        "governance": governance_entry,
    }


def list_personas(*, vault_root: Path) -> dict[str, Any]:
    """R14 C55: 確保 list 回的 status 跟 registry.yaml 同步.

    Codex T8.5 GAP (2026-05-18 R11 前) 報告: persona-disable 後 persona-list 仍回 status=active.
    audit (HEAD ≥ `7340ed5`): 已無法重現. 但加 invariant assert 防回歸 —
    若 registry.yaml status 是 disabled, return 內必須也是 disabled.
    """
    root = Path(vault_root).expanduser().resolve()
    registry = _load_yaml_object(_registry_path(root))
    personas = registry.get("personas", {})
    if not isinstance(personas, dict):
        personas = {}
    # Invariant guard: 把 registry 內 disabled* metadata explicit 帶到 return
    # (本來就會帶, 加 explicit copy 避 caller 漏看欄位導致誤判)
    normalized: dict[str, Any] = {}
    for pid, entry in personas.items():
        if not isinstance(entry, dict):
            normalized[pid] = entry
            continue
        # 維持原欄位 + 確保 status 是 disabled 時 disabled_at 也在 (debug 用)
        copy = dict(entry)
        if str(copy.get("status", "active")) == "disabled":
            # disabled 時這幾個欄位該存在 (disable_persona 寫的); 沒有就補空字串避 NPE
            for k in ("disabled_at", "disabled_by"):
                if k not in copy:
                    copy[k] = ""
        normalized[pid] = copy
    return {
        "default_persona": str(registry.get("default_persona", "core")),
        "personas": normalized,
    }


def update_persona_profile(
    *,
    vault_root: Path,
    persona_id: str,
    operator: str = "user",
    display_name: str | None = None,
    mission: str | None = None,
    style: str | None = None,
    language: str | None = None,
    default_mode: str | None = None,
    role_type: str | None = None,
    allow_experimental_role: bool = False,
    tool_access_enabled: bool | None = None,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    pid = _normalize_persona_id(persona_id, fallback="")
    if not pid:
        raise ValueError("persona_id 不可為空")

    persona_abs = _persona_path(root, pid)
    route_abs = _route_path(root, pid)
    if not persona_abs.exists() or not route_abs.exists():
        raise FileNotFoundError(f"persona 不存在或缺檔：{pid}")

    adapter = ObsidianVaultAdapter(root)
    current_text = persona_abs.read_text(encoding="utf-8")
    metadata, _ = adapter.parse_frontmatter(current_text)
    if not isinstance(metadata, dict):
        metadata = {}

    route_payload = _load_yaml_object(route_abs)
    if not route_payload:
        raise ValueError(f"route 內容無效：{route_abs}")

    governance_cfg = load_persona_governance(root)
    governance_before = resolve_persona_governance(governance_cfg, persona_id=pid)
    governance_caps = governance_before.get("capabilities", {})
    if not isinstance(governance_caps, dict):
        governance_caps = {}

    before = {
        "display_name": str(metadata.get("display_name", pid)).strip() or pid,
        "mission": str(metadata.get("mission", f"{pid} 人格任務")).strip() or f"{pid} 人格任務",
        "style": str(metadata.get("style", "concise")).strip() or "concise",
        "language": str(metadata.get("language", "zh-Hant")).strip() or "zh-Hant",
        "role_type": _normalize_role_type(
            str(metadata.get("role_type", "")),
            fallback="tooling" if bool(governance_caps.get("tools_enabled", False)) else "chat",
        ),
        "default_mode": str(route_payload.get("default_mode", "standard")).strip() or "standard",
        "tools_enabled": bool(governance_caps.get("tools_enabled", False)),
    }

    after = dict(before)
    changed_fields: list[str] = []
    requested_role_type = _resolve_role_type_for_update(
        current_role_type=str(before["role_type"]),
        role_type=role_type,
        allow_experimental_role=allow_experimental_role,
    )
    role_type_changed = requested_role_type != str(before["role_type"])
    if role_type_changed:
        after["role_type"] = requested_role_type
        changed_fields.append("role_type")

    if display_name is not None and display_name.strip() and display_name.strip() != before["display_name"]:
        after["display_name"] = display_name.strip()
        changed_fields.append("display_name")
    if mission is not None and mission.strip() and mission.strip() != before["mission"]:
        after["mission"] = mission.strip()
        changed_fields.append("mission")
    if style is not None and style.strip() and style.strip() != before["style"]:
        after["style"] = style.strip()
        changed_fields.append("style")
    if language is not None and language.strip() and language.strip() != before["language"]:
        after["language"] = language.strip()
        changed_fields.append("language")

    resolved_tools_enabled = bool(before["tools_enabled"])
    if str(after["role_type"]) == "chat":
        if tool_access_enabled is True:
            raise ValueError("role_type=chat 不可啟用工具能力。請先改成 role_type=tooling。")
        resolved_tools_enabled = False
    else:
        if tool_access_enabled is not None:
            resolved_tools_enabled = bool(tool_access_enabled)
        elif role_type_changed and str(after["role_type"]) == "tooling":
            resolved_tools_enabled = True

    if resolved_tools_enabled != bool(before["tools_enabled"]):
        after["tools_enabled"] = resolved_tools_enabled
        changed_fields.append("tools_enabled")

    if default_mode is not None and default_mode.strip():
        if default_mode.strip() != before["default_mode"]:
            after["default_mode"] = default_mode.strip()
            changed_fields.append("default_mode")
    elif role_type_changed:
        auto_mode = _resolve_default_mode(
            role_type=str(after["role_type"]),
            explicit_default_mode=None,
            fallback=str(before["default_mode"]),
        )
        if auto_mode != before["default_mode"]:
            after["default_mode"] = auto_mode
            changed_fields.append("default_mode")

    if not changed_fields:
        return {
            "persona_id": pid,
            "status": "unchanged",
            "changed_fields": [],
            "persona_path": str(persona_abs),
            "route_path": str(route_abs),
            "before": before,
            "after": after,
        }

    route_payload["default_mode"] = after["default_mode"]
    route_payload["updated_at"] = _now_iso()

    persona_text = _persona_markdown(
        persona_id=pid,
        display_name=after["display_name"],
        mission=after["mission"],
        style=after["style"],
        language=after["language"],
        role_type=str(after["role_type"]),
        default_mode=after["default_mode"],
    )
    atomic_write(persona_abs, persona_text)
    atomic_write(route_abs, _dump_yaml(route_payload))

    governance_path: str | None = None
    governance_entry: dict[str, Any] | None = None
    if "tools_enabled" in changed_fields:
        gov_path, gov_entry = upsert_persona_governance(
            root,
            persona_id=pid,
            operator=operator,
            tools_enabled=bool(after["tools_enabled"]),
            source="persona_update",
        )
        governance_path = str(gov_path)
        governance_entry = gov_entry

    _append_event(
        vault_root=root,
        event_type="update_persona",
        persona_id=pid,
        operator=operator,
        status="updated",
        detail=f"changed_fields={','.join(changed_fields)}",
        proposal_id=None,
    )

    payload: dict[str, Any] = {
        "persona_id": pid,
        "status": "updated",
        "changed_fields": changed_fields,
        "persona_path": str(persona_abs),
        "route_path": str(route_abs),
        "before": before,
        "after": after,
    }
    if governance_path is not None and governance_entry is not None:
        payload["governance_path"] = governance_path
        payload["governance"] = governance_entry
    return payload


def ensure_default_steward_persona(
    *,
    vault_root: Path,
    operator: str = "system",
    persona_id: str = "steward",
    display_name: str = "管家",
    mission: str = "",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    _ensure_parent_dirs(root)

    registry = _load_yaml_object(_registry_path(root))
    personas = registry.get("personas", {}) if isinstance(registry.get("personas", {}), dict) else {}
    active_non_core = [
        key
        for key, item in personas.items()
        if key != "core" and isinstance(item, dict) and str(item.get("status", "active")) != "disabled"
    ]
    if active_non_core:
        return {
            "status": "skipped",
            "reason": "non_core_persona_exists",
            "active_non_core_personas": sorted(active_non_core),
        }

    pid = _normalize_persona_id(persona_id, fallback="steward")
    if pid == "core":
        pid = "steward"

    resolved_mission = mission.strip() or "做為管家角色，協助建立環境、管理角色，並可操作工具改寫專案檔案。"
    result = create_persona_proposal(
        vault_root=root,
        display_name=display_name.strip() or "管家",
        persona_id=pid,
        mission=resolved_mission,
        style="concise",
        language="zh-Hant",
        role_type="tooling",
        default_mode="executor",
        operator=operator,
        auto_approve=True,
        tool_access_enabled=True,
    )
    result["bootstrap"] = "default_steward"
    return result


