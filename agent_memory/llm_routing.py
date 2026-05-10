"""LLM routing config helpers (global default + persona override + fallback)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

LLM_ROUTER_RELATIVE_PATH = "00_System/08_Runtime_Profiles/llm_router.yaml"


def _normalize_model_ref(raw: str | None, fallback: str = "") -> str:
    value = (raw or "").strip()
    return value or fallback


def _default_router_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "description": "全域預設 LLM + 人格覆蓋 + fallback 鏈。改這裡即可影響所有入口。",
        "resolution_order": [
            "request_override",
            "persona_override",
            "global_default",
            "fallback_chain",
        ],
        "global_default": {
            "profile": "ollama_local",
            "model": "qwen3:14b",
        },
        "fallback_chain": [
            {"profile": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
            {"profile": "openai", "model": "gpt-4.1-mini"},
        ],
        "persona_overrides": {
            # "coder": {"profile": "opencode_go", "model": "opencode/kimi-k2.6"},
            # "writer": {"profile": "gemini", "model": "gemini-2.5-pro"},
        },
        "providers": {
            "ollama_local": {
                "kind": "ollama",
                "zh_label": "本地免費模型（Ollama）",
                "base_url": "http://127.0.0.1:11434",
                "api_key_env": "OLLAMA_API_KEY",
                "requires_api_key": False,
            },
            "llama_cpp_local": {
                "kind": "llama_cpp_python",
                "zh_label": "本機 GGUF 直連（llama-cpp-python）",
                "base_url": "local://llama-cpp-python",
                "api_key_env": "",
                "requires_api_key": False,
                "model_path": "../../0_Models/Qwen3-30B-A3B-Q4_K_M.gguf",
                "n_ctx": 4096,
                "n_gpu_layers": 999,
                "n_batch": 512,
                "n_threads": 16,
                "flash_attn": True,
                "max_tokens": 1024,
                "strip_think_tags": True,
            },
            "openai": {
                "kind": "openai_compatible",
                "zh_label": "OpenAI 商用 API",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "requires_api_key": True,
            },
            "anthropic": {
                "kind": "anthropic",
                "zh_label": "Anthropic 商用 API",
                "base_url": "https://api.anthropic.com/v1",
                "api_key_env": "ANTHROPIC_API_KEY",
                "requires_api_key": True,
            },
            "gemini": {
                "kind": "openai_compatible",
                "zh_label": "Google Gemini API",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key_env": "GOOGLE_API_KEY",
                "requires_api_key": True,
            },
            "openrouter": {
                "kind": "openai_compatible",
                "zh_label": "OpenRouter 聚合 API",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "requires_api_key": True,
            },
            "opencode_zen": {
                "kind": "openai_compatible",
                "zh_label": "OpenCode Zen 商用聚合",
                "base_url": "https://opencode.ai/zen/v1",
                "api_key_env": "OPENCODE_ZEN_API_KEY",
                "requires_api_key": True,
            },
            "opencode_go": {
                "kind": "openai_compatible",
                "zh_label": "OpenCode Go（偏開源模型池）",
                "base_url": "https://opencode.ai/zen/go/v1",
                "api_key_env": "OPENCODE_GO_API_KEY",
                "requires_api_key": True,
            },
        },
    }


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def ensure_llm_router_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    """Ensure llm router yaml exists under runtime profiles."""

    root = Path(vault_root).expanduser().resolve()
    target = (root / LLM_ROUTER_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    atomic_write(target, _dump_yaml(_default_router_config()))
    return target


def load_llm_router_config(vault_root: Path) -> dict[str, Any]:
    """Load routing config and auto-bootstrap defaults when missing."""

    path = ensure_llm_router_file(vault_root)
    raw = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(raw) or {}
    if not isinstance(payload, dict):
        raise ValueError("llm_router.yaml 格式錯誤：必須是 YAML object")
    return payload


def save_llm_router_config(vault_root: Path, config: dict[str, Any]) -> Path:
    """Persist routing config with lock + atomic write."""

    path = ensure_llm_router_file(vault_root)
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(config))
    return path


def resolve_llm_route(
    config: dict[str, Any],
    *,
    persona_id: str | None = None,
    override_profile: str | None = None,
    override_model: str | None = None,
) -> dict[str, Any]:
    """Resolve effective model chain from override/persona/global/fallback."""

    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}

    persona_overrides = config.get("persona_overrides", {})
    if not isinstance(persona_overrides, dict):
        persona_overrides = {}

    global_default = config.get("global_default", {})
    if not isinstance(global_default, dict):
        global_default = {}

    persona_entry = persona_overrides.get(persona_id or "", {})
    if not isinstance(persona_entry, dict):
        persona_entry = {}

    selected_profile = (
        (override_profile or "").strip()
        or str(persona_entry.get("profile", "")).strip()
        or str(global_default.get("profile", "")).strip()
    )
    selected_model = _normalize_model_ref(
        override_model
        or str(persona_entry.get("model", ""))
        or str(global_default.get("model", "")),
        fallback="",
    )

    if not selected_profile:
        raise ValueError("無法解析 LLM profile：請設定 global_default.profile")
    if not selected_model:
        raise ValueError("無法解析 LLM model：請設定 global_default.model")

    fallback_chain = config.get("fallback_chain", [])
    if not isinstance(fallback_chain, list):
        fallback_chain = []

    chain: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _append(profile: str, model: str) -> None:
        key = (profile.strip(), model.strip())
        if not key[0] or not key[1]:
            return
        if key in seen:
            return
        seen.add(key)
        chain.append({"profile": key[0], "model": key[1]})

    _append(selected_profile, selected_model)
    for item in fallback_chain:
        if not isinstance(item, dict):
            continue
        _append(str(item.get("profile", "")), str(item.get("model", "")))

    resolved_chain: list[dict[str, Any]] = []
    for idx, item in enumerate(chain):
        profile = item["profile"]
        provider_cfg = providers.get(profile, {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}
        api_key_env = str(provider_cfg.get("api_key_env", "")).strip()
        requires_key = bool(provider_cfg.get("requires_api_key", True))
        has_key = bool(os.getenv(api_key_env, "").strip()) if api_key_env else False
        resolved_chain.append(
            {
                "rank": idx + 1,
                "profile": profile,
                "model": item["model"],
                "kind": str(provider_cfg.get("kind", "unknown")),
                "zh_label": str(provider_cfg.get("zh_label", "")),
                "base_url": str(provider_cfg.get("base_url", "")),
                "api_key_env": api_key_env,
                "requires_api_key": requires_key,
                "api_key_present": has_key if requires_key else True,
            }
        )

    return {
        "persona_id": persona_id or "",
        "selected_profile": selected_profile,
        "selected_model": selected_model,
        "chain": resolved_chain,
    }
