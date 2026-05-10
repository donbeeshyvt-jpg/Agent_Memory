"""Configuration helpers for CLI/runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


def _project_base() -> Path:
    return Path(__file__).resolve().parents[2]


def user_config_path() -> Path:
    """User-level config path (~/.agent_memory/config.toml)."""

    return Path.home() / ".agent_memory" / "config.toml"


ENV_VAULT_ROOT = "AGENT_MEMORY_VAULT_ROOT"


def default_vault_root() -> Path:
    """Default vault path for this workspace."""

    return _project_base() / "SecondBrains" / "default_second_brain"


def _read_user_config() -> dict[str, Any]:
    if tomllib is None:
        return {}
    config_path = user_config_path()
    if not config_path.exists():
        return {}
    raw = config_path.read_text(encoding="utf-8")
    return tomllib.loads(raw)


def resolve_vault_root_with_source(cli_value: str | None) -> tuple[Path, str]:
    """Resolve vault root path and return its source."""

    if cli_value:
        return Path(cli_value).expanduser().resolve(), "cli_arg"

    env_value = os.getenv(ENV_VAULT_ROOT, "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve(), f"env:{ENV_VAULT_ROOT}"

    cfg = _read_user_config()
    vault_cfg = cfg.get("vault", {})
    root = vault_cfg.get("root")
    if isinstance(root, str) and root.strip():
        return Path(root).expanduser().resolve(), "user_config"

    return default_vault_root().resolve(), "default_workspace"


def resolve_vault_root(cli_value: str | None) -> Path:
    """Resolve vault root path from CLI arg, config, then default."""

    root, _ = resolve_vault_root_with_source(cli_value)
    return root


def set_user_vault_root(path_value: str) -> Path:
    """Persist default vault path to user config."""

    resolved = Path(path_value).expanduser().resolve()
    cfg_path = user_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    content = "[vault]\n" + f"root = {json.dumps(str(resolved), ensure_ascii=False)}\n"
    cfg_path.write_text(content, encoding="utf-8")
    return resolved


def clear_user_vault_root() -> bool:
    """Delete user config file if present."""

    cfg_path = user_config_path()
    if not cfg_path.exists():
        return False
    cfg_path.unlink()
    return True
