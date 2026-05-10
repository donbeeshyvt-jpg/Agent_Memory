"""Persona bundle pack/unpack for cross-brain portability."""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write

_RUNTIME_ROOT = "00_System/08_Runtime_Profiles"
_REGISTRY_REL = f"{_RUNTIME_ROOT}/registry.yaml"
_PERSONA_REL = f"{_RUNTIME_ROOT}/personas"
_ROUTE_REL = f"{_RUNTIME_ROOT}/routes"
_PERSONA_SKILLS_REL = "00_System/Skills/_Persona"
_SESSION_LOG_REL = "70_Active_Plans/Session_Logs"
_BUNDLE_OUT_REL = "11_AI_Mirror/external_ingest/persona_bundles"
_BRAIN_MANIFEST_REL = f"{_RUNTIME_ROOT}/brain_manifest.yaml"

_SAFE_PERSONA_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug_persona(raw: str) -> str:
    cleaned = _SAFE_PERSONA_RE.sub("-", str(raw).strip()).strip("-").lower()
    return cleaned


def _load_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _to_rel_text(vault_root: Path, abs_path: Path) -> str:
    return str(abs_path.resolve().relative_to(vault_root.resolve())).replace("\\", "/")


def _safe_rel_from_zip(raw: str) -> str:
    rel = raw.replace("\\", "/").strip().lstrip("/")
    if not rel:
        raise ValueError("bundle 內含空路徑")
    path = PurePosixPath(rel)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"bundle 路徑不安全：{raw}")
    return str(path)


def _collect_persona_paths(
    *,
    vault_root: Path,
    persona_id: str,
    include_sessions: bool,
    include_persona_skills: bool,
) -> list[str]:
    persona = _slug_persona(persona_id)
    if not persona:
        raise ValueError("persona_id 不可為空")

    root = vault_root.resolve()
    paths: list[str] = []
    persona_file = root / _PERSONA_REL / f"{persona}.md"
    route_file = root / _ROUTE_REL / f"{persona}.yaml"
    if not persona_file.exists():
        raise FileNotFoundError(f"找不到 persona 檔：{persona_file}")
    if not route_file.exists():
        raise FileNotFoundError(f"找不到 route 檔：{route_file}")
    paths.append(_to_rel_text(root, persona_file))
    paths.append(_to_rel_text(root, route_file))

    if include_persona_skills:
        skill_root = (root / _PERSONA_SKILLS_REL / persona).resolve()
        if skill_root.exists() and skill_root.is_dir():
            for file_path in skill_root.rglob("*"):
                if not file_path.is_file():
                    continue
                paths.append(_to_rel_text(root, file_path))

    if include_sessions:
        session_root = (root / _SESSION_LOG_REL).resolve()
        if session_root.exists() and session_root.is_dir():
            prefix = f"{persona}__"
            for file_path in session_root.rglob("*.md"):
                if file_path.name.startswith(prefix):
                    paths.append(_to_rel_text(root, file_path))

    deduped = sorted({item for item in paths if item})
    return deduped


def export_persona_bundle(
    *,
    vault_root: Path,
    persona_id: str,
    output_dir: Path | None = None,
    include_sessions: bool = True,
    include_persona_skills: bool = True,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    persona = _slug_persona(persona_id)
    if not persona:
        raise ValueError("persona_id 不可為空")
    paths = _collect_persona_paths(
        vault_root=root,
        persona_id=persona,
        include_sessions=include_sessions,
        include_persona_skills=include_persona_skills,
    )
    if not paths:
        raise ValueError(f"persona={persona} 沒有可打包路徑")

    out_dir = (Path(output_dir).expanduser().resolve() if output_dir else (root / _BUNDLE_OUT_REL).resolve())
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle_path = out_dir / f"persona-{persona}-{stamp}.zip"

    brain_manifest = _load_yaml_object((root / _BRAIN_MANIFEST_REL).resolve())
    manifest = {
        "schema_version": 1,
        "persona_id": persona,
        "exported_at": _now_iso(),
        "source_vault_root": str(root),
        "source_brain_id": str(brain_manifest.get("brain_id", "")),
        "source_owner_id": str(brain_manifest.get("owner_id", "")),
        "include_sessions": include_sessions,
        "include_persona_skills": include_persona_skills,
        "paths": paths,
    }

    with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel in paths:
            abs_path = (root / rel).resolve()
            if not abs_path.exists() or not abs_path.is_file():
                continue
            arcname = f"files/{rel}"
            zf.write(abs_path, arcname=arcname)

    return {
        "persona_id": persona,
        "bundle_path": str(bundle_path),
        "path_count": len(paths),
        "include_sessions": include_sessions,
        "include_persona_skills": include_persona_skills,
        "paths": paths,
    }


def import_persona_bundle(
    *,
    vault_root: Path,
    bundle_path: Path,
    overwrite: bool = False,
    force_active: bool = True,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    bundle = Path(bundle_path).expanduser().resolve()
    if not bundle.exists() or not bundle.is_file():
        raise FileNotFoundError(f"bundle 不存在：{bundle}")

    with zipfile.ZipFile(bundle, mode="r") as zf:
        if "manifest.json" not in zf.namelist():
            raise ValueError("bundle 缺少 manifest.json")
        manifest_raw = zf.read("manifest.json").decode("utf-8")
        manifest = json.loads(manifest_raw)
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 格式錯誤")
        persona = _slug_persona(str(manifest.get("persona_id", "")))
        if not persona:
            raise ValueError("bundle manifest 缺少 persona_id")

        copied: list[str] = []
        skipped: list[str] = []
        for member in zf.infolist():
            name = member.filename
            if not name.startswith("files/") or name.endswith("/"):
                continue
            rel_raw = name[len("files/") :]
            rel = _safe_rel_from_zip(rel_raw)
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"bundle 路徑逃逸：{rel}") from exc
            if target.exists() and not overwrite:
                skipped.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(member)
            target.write_bytes(data)
            copied.append(rel)

    registry_abs = (root / _REGISTRY_REL).resolve()
    registry = _load_yaml_object(registry_abs)
    if not registry:
        registry = {"schema_version": 1, "default_persona": "core", "personas": {}}
    personas = registry.get("personas", {})
    if not isinstance(personas, dict):
        personas = {}

    persona_entry = personas.get(persona, {}) if isinstance(personas.get(persona), dict) else {}
    persona_entry["persona_path"] = f"personas/{persona}.md"
    persona_entry["route_path"] = f"routes/{persona}.yaml"
    if force_active:
        persona_entry["status"] = "active"
    elif not str(persona_entry.get("status", "")).strip():
        persona_entry["status"] = "active"
    persona_entry["imported_at"] = _now_iso()
    persona_entry["imported_from_bundle"] = str(bundle)
    personas[persona] = persona_entry
    registry["personas"] = personas
    registry["updated_at"] = _now_iso()
    if not str(registry.get("default_persona", "")).strip():
        registry["default_persona"] = "core"
    registry_abs.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(registry_abs, _dump_yaml(registry))

    return {
        "persona_id": persona,
        "bundle_path": str(bundle),
        "force_active": force_active,
        "overwrite": overwrite,
        "copied_count": len(copied),
        "skipped_count": len(skipped),
        "copied_paths": copied,
        "skipped_paths": skipped,
        "registry_path": str(registry_abs),
    }
