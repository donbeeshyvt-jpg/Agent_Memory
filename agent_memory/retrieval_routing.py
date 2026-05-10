"""Retrieval routing config (embedding backend + search policy)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

RETRIEVAL_ROUTER_RELATIVE_PATH = "00_System/08_Runtime_Profiles/retrieval_router.yaml"


def _default_retrieval_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "description": "檢索策略：embedding backend（hash/provider）與 MMR 參數。",
        "embedding": {
            "mode": "hash",
            "profile": "openai",
            "model": "text-embedding-3-small",
            "timeout_s": 20.0,
        },
        "search": {
            "default_strategy": "hybrid",
            "mmr_enabled": True,
            "mmr_lambda": 0.7,
            "mmr_candidate_multiplier": 4,
        },
        "persona_overrides": {},
    }


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def ensure_retrieval_router_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / RETRIEVAL_ROUTER_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    atomic_write(target, _dump_yaml(_default_retrieval_config()))
    return target


def load_retrieval_router_config(vault_root: Path) -> dict[str, Any]:
    path = ensure_retrieval_router_file(vault_root)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("retrieval_router.yaml 格式錯誤：必須是 YAML object")
    return payload


def save_retrieval_router_config(vault_root: Path, config: dict[str, Any]) -> Path:
    path = ensure_retrieval_router_file(vault_root)
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(config))
    return path


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def resolve_retrieval_route(config: dict[str, Any], *, persona_id: str | None = None) -> dict[str, Any]:
    embedding = config.get("embedding", {})
    search = config.get("search", {})
    if not isinstance(embedding, dict):
        embedding = {}
    if not isinstance(search, dict):
        search = {}

    resolved = {"embedding": dict(embedding), "search": dict(search)}
    persona_overrides = config.get("persona_overrides", {})
    if isinstance(persona_overrides, dict):
        key = (persona_id or "").strip()
        override = persona_overrides.get(key, {})
        if isinstance(override, dict):
            resolved = _merge_dict(resolved, override)

    mode = str(resolved["embedding"].get("mode", "hash")).strip().lower()
    if mode not in {"hash", "provider"}:
        mode = "hash"
    resolved["embedding"]["mode"] = mode
    resolved["embedding"]["profile"] = str(resolved["embedding"].get("profile", "")).strip()
    resolved["embedding"]["model"] = str(resolved["embedding"].get("model", "")).strip()
    try:
        timeout_s = float(resolved["embedding"].get("timeout_s", 20.0))
    except (TypeError, ValueError):
        timeout_s = 20.0
    resolved["embedding"]["timeout_s"] = max(5.0, timeout_s)

    strategy = str(resolved["search"].get("default_strategy", "hybrid")).strip().lower()
    if strategy not in {"hybrid", "fts", "vector"}:
        strategy = "hybrid"
    resolved["search"]["default_strategy"] = strategy
    resolved["search"]["mmr_enabled"] = bool(resolved["search"].get("mmr_enabled", True))
    try:
        mmr_lambda = float(resolved["search"].get("mmr_lambda", 0.7))
    except (TypeError, ValueError):
        mmr_lambda = 0.7
    resolved["search"]["mmr_lambda"] = max(0.0, min(1.0, mmr_lambda))
    try:
        candidate_mul = int(resolved["search"].get("mmr_candidate_multiplier", 4))
    except (TypeError, ValueError):
        candidate_mul = 4
    resolved["search"]["mmr_candidate_multiplier"] = max(2, candidate_mul)
    return resolved
