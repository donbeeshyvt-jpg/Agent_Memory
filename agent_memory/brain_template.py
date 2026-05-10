"""Template seeding helpers for creating multi-persona brain instances."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write

_RUNTIME_ROOT = "00_System/08_Runtime_Profiles"
_REGISTRY_REL = f"{_RUNTIME_ROOT}/registry.yaml"
_PERSONA_DIR_REL = f"{_RUNTIME_ROOT}/personas"
_ROUTE_DIR_REL = f"{_RUNTIME_ROOT}/routes"
_DIALOGUE_MODES_REL = f"{_RUNTIME_ROOT}/dialogue_modes.yaml"
_PERSONA_SKILLS_REL = "00_System/Skills/_Persona"
_SHARED_SKILLS_REL = "00_System/Skills"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_persona_id(raw: str) -> str:
    text = str(raw).strip().replace("\\", "/")
    if not text:
        return ""
    safe = []
    for char in text:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char.lower())
        else:
            safe.append("-")
    normalized = "".join(safe).strip("-")
    return normalized


def _load_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _copy_file(src: Path, dst: Path, *, overwrite: bool) -> bool:
    if not src.exists() or not src.is_file():
        return False
    if dst.exists() and not overwrite:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _iter_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def seed_brain_from_template(
    *,
    template_vault: Path,
    target_vault: Path,
    overwrite: bool = False,
    include_personas: bool = True,
    include_persona_skills: bool = True,
    include_shared_skills: bool = False,
    include_dialogue_modes: bool = True,
) -> dict[str, Any]:
    template_root = Path(template_vault).expanduser().resolve()
    target_root = Path(target_vault).expanduser().resolve()
    if not template_root.exists() or not template_root.is_dir():
        raise FileNotFoundError(f"template vault 不存在：{template_root}")
    if not target_root.exists() or not target_root.is_dir():
        raise FileNotFoundError(f"target vault 不存在：{target_root}")

    template_registry_abs = (template_root / _REGISTRY_REL).resolve()
    target_registry_abs = (target_root / _REGISTRY_REL).resolve()
    template_registry = _load_yaml_object(template_registry_abs)
    target_registry = _load_yaml_object(target_registry_abs)

    if not template_registry:
        raise FileNotFoundError(f"template registry 不存在或格式錯誤：{template_registry_abs}")
    if not target_registry:
        target_registry = {"schema_version": 1, "default_persona": "core", "personas": {}}

    template_personas = template_registry.get("personas", {})
    if not isinstance(template_personas, dict):
        template_personas = {}
    target_personas = target_registry.get("personas", {})
    if not isinstance(target_personas, dict):
        target_personas = {}

    copied_files: list[str] = []
    skipped_files: list[str] = []
    imported_personas: list[str] = []
    skipped_personas: list[str] = []

    if include_personas:
        for raw_pid, raw_entry in template_personas.items():
            pid = _normalize_persona_id(raw_pid)
            if not pid or pid == "core":
                continue
            entry = raw_entry if isinstance(raw_entry, dict) else {}

            persona_rel = str(entry.get("persona_path", f"personas/{pid}.md")).replace("\\", "/").strip().lstrip("/")
            route_rel = str(entry.get("route_path", f"routes/{pid}.yaml")).replace("\\", "/").strip().lstrip("/")
            persona_src = (template_root / _RUNTIME_ROOT / persona_rel).resolve()
            route_src = (template_root / _RUNTIME_ROOT / route_rel).resolve()
            persona_dst = (target_root / _RUNTIME_ROOT / f"personas/{pid}.md").resolve()
            route_dst = (target_root / _RUNTIME_ROOT / f"routes/{pid}.yaml").resolve()

            if (pid in target_personas) and not overwrite:
                skipped_personas.append(pid)
                continue

            copied_persona = _copy_file(persona_src, persona_dst, overwrite=overwrite)
            copied_route = _copy_file(route_src, route_dst, overwrite=overwrite)
            if copied_persona:
                copied_files.append(str(persona_dst.relative_to(target_root)).replace("\\", "/"))
            else:
                skipped_files.append(str(persona_dst.relative_to(target_root)).replace("\\", "/"))
            if copied_route:
                copied_files.append(str(route_dst.relative_to(target_root)).replace("\\", "/"))
            else:
                skipped_files.append(str(route_dst.relative_to(target_root)).replace("\\", "/"))

            if copied_persona and copied_route:
                imported_personas.append(pid)
                target_personas[pid] = {
                    "persona_path": f"personas/{pid}.md",
                    "route_path": f"routes/{pid}.yaml",
                    "status": "active",
                    "approved_at": str(entry.get("approved_at", "")),
                    "approved_by": str(entry.get("approved_by", "")),
                    "disabled_at": str(entry.get("disabled_at", "")),
                    "disabled_by": str(entry.get("disabled_by", "")),
                    "disabled_reason": str(entry.get("disabled_reason", "")),
                }
                target_personas[pid].pop("disabled_at", None)
                target_personas[pid].pop("disabled_by", None)
                target_personas[pid].pop("disabled_reason", None)
                # 移除空字串欄位，避免 registry 冗長
                target_personas[pid] = {k: v for k, v in target_personas[pid].items() if str(v).strip() != ""}

    if include_persona_skills:
        persona_skill_src_root = (template_root / _PERSONA_SKILLS_REL).resolve()
        persona_skill_dst_root = (target_root / _PERSONA_SKILLS_REL).resolve()
        for src in _iter_files(persona_skill_src_root):
            rel = src.relative_to(persona_skill_src_root)
            parts = rel.parts
            if not parts:
                continue
            persona_part = _normalize_persona_id(parts[0])
            if imported_personas and persona_part not in imported_personas:
                continue
            dst = persona_skill_dst_root / rel
            copied = _copy_file(src, dst, overwrite=overwrite)
            rel_text = str(dst.relative_to(target_root)).replace("\\", "/")
            if copied:
                copied_files.append(rel_text)
            else:
                skipped_files.append(rel_text)

    if include_shared_skills:
        shared_skill_src_root = (template_root / _SHARED_SKILLS_REL).resolve()
        shared_skill_dst_root = (target_root / _SHARED_SKILLS_REL).resolve()
        for skill_dir in sorted(shared_skill_src_root.iterdir() if shared_skill_src_root.exists() else []):
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_"):
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue
            for src in _iter_files(skill_dir):
                rel = src.relative_to(shared_skill_src_root)
                dst = shared_skill_dst_root / rel
                copied = _copy_file(src, dst, overwrite=overwrite)
                rel_text = str(dst.relative_to(target_root)).replace("\\", "/")
                if copied:
                    copied_files.append(rel_text)
                else:
                    skipped_files.append(rel_text)

    if include_dialogue_modes:
        dialogue_src = (template_root / _DIALOGUE_MODES_REL).resolve()
        dialogue_dst = (target_root / _DIALOGUE_MODES_REL).resolve()
        copied = _copy_file(dialogue_src, dialogue_dst, overwrite=overwrite)
        if copied:
            copied_files.append(str(dialogue_dst.relative_to(target_root)).replace("\\", "/"))
        elif dialogue_src.exists():
            skipped_files.append(str(dialogue_dst.relative_to(target_root)).replace("\\", "/"))

    target_registry["personas"] = target_personas
    target_registry["updated_at"] = _now_iso()
    if not str(target_registry.get("default_persona", "")).strip():
        target_registry["default_persona"] = "core"
    target_registry_abs.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target_registry_abs, _dump_yaml(target_registry))

    return {
        "template_vault": str(template_root),
        "target_vault": str(target_root),
        "overwrite": overwrite,
        "include_personas": include_personas,
        "include_persona_skills": include_persona_skills,
        "include_shared_skills": include_shared_skills,
        "include_dialogue_modes": include_dialogue_modes,
        "imported_personas": sorted(imported_personas),
        "skipped_personas": sorted(skipped_personas),
        "copied_files": sorted(set(copied_files)),
        "skipped_files": sorted(set(skipped_files)),
        "registry_path": str(target_registry_abs),
    }
