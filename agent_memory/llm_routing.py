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
    """V1 範圍：只支援『本機 GGUF (llama_cpp_local)』+『Google Gemini / Gemma API (gemini)』。

    其他 provider (openai/openrouter/anthropic/ollama/opencode) 已從 V1 預設移除。
    使用者要的話可手動編輯 yaml 補回。
    """
    return {
        "schema_version": 1,
        "description": "V1：本機 GGUF + Google Gemini API。改 global_default / persona_overrides 切角色用哪顆。",
        "resolution_order": [
            "request_override",
            "auxiliary_override",
            "persona_override",
            "global_default",
            "fallback_chain",
        ],
        "global_default": {
            "profile": "llama_cpp_local",
            "model": "../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf",
        },
        "fallback_chain": [
            {"profile": "gemini", "model": "gemma-4-31b-it"},
        ],
        "persona_overrides": {},
        # R21 C111 (A1): auxiliary.* 子任務 LLM 分工 (參考 hermes auxiliary.* 設計).
        # auxiliary_default 是 cheap 子任務通用 model (curator/umbrella/triage 等),
        # auxiliary_overrides 是 per-task override (e.g. umbrella 走特定 model).
        # Priority: request_override > auxiliary_override > persona_override > global_default.
        # 都缺 → 用 global_default (backward compat, 既有沒設 auxiliary 的 db 不影響).
        "auxiliary_default": {
            "profile": "",  # 空字串 = 不啟用 auxiliary 分流, 仍用 global/persona
            "model": "",
        },
        "auxiliary_overrides": {},  # e.g. {"umbrella": {"profile": "gemini", "model": "gemma-4-31b-it"}}
        "providers": {
            "llama_cpp_local": {
                "kind": "llama_cpp_python",
                "zh_label": "本機 GGUF 直連（llama-cpp-python）",
                "base_url": "local://llama-cpp-python",
                "api_key_env": "",
                "requires_api_key": False,
                "model_path": "../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf",
                "n_ctx": 4096,
                "n_gpu_layers": 999,
                "n_batch": 512,
                "n_threads": 16,
                "flash_attn": True,
                "max_tokens": 1024,
                "strip_think_tags": True,
            },
            "gemini": {
                "kind": "openai_compatible",
                "zh_label": "Google Gemini / Gemma API",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key_env": "GOOGLE_API_KEY",
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


def _merge_companion_llm_config(vault_root: Path, base_config: dict[str, Any]) -> dict[str, Any]:
    """V3-O.10 #3+#18: 把 companion_config.yaml.llm merge 進 router config.

    讓既有 resolve_llm_route 無痛讀到 companion 的 main_chat / sub_tasks / providers,
    不必改 _generate_core. companion vault 統一入口 (companion_config.yaml.llm 優先).

    轉換對映:
      llm.main_chat        → global_default + fallback_chain
      llm.sub_tasks.<task> → auxiliary_overrides.<task> (provider→profile, model 從 sub_task
                             或 provider.model_path / provider.model 推)
      llm.providers.*      → providers.* (merge, companion 優先)

    非 companion vault (無 companion_config.yaml) 或無 llm 段 → 原樣返回 (不破 steward).
    """
    ccfg_path = vault_root / "00_System_Core" / "companion_config.yaml"
    if not ccfg_path.exists():
        return base_config
    try:
        ccfg = yaml.safe_load(ccfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return base_config
    if not isinstance(ccfg, dict):
        return base_config
    llm = ccfg.get("llm", {})
    if not isinstance(llm, dict) or not llm:
        return base_config

    merged = dict(base_config)

    # 1. providers merge (companion 優先覆蓋同名)
    providers = dict(merged.get("providers", {}) if isinstance(merged.get("providers"), dict) else {})
    cc_providers = llm.get("providers", {})
    _LOCAL_KINDS = {"llama_cpp_python", "llama_cpp_local", "ollama"}
    if isinstance(cc_providers, dict):
        for name, pcfg in cc_providers.items():
            if isinstance(pcfg, dict):
                pcfg = dict(pcfg)
                # 本地 provider (GGUF/ollama) 自動補 2 個欄位, 避免 _generate_core 誤跳:
                #   requires_api_key=False — 否則預設 True + 無 key → skip → 誤 fallback 線上
                #   base_url 佔位 — _generate_core 有 "if not base_url: skip" check,
                #     本地不需 URL 但要給佔位 (對齊 legacy llm_router llama_cpp_local 設計)
                if str(pcfg.get("kind", "")).strip() in _LOCAL_KINDS:
                    pcfg.setdefault("requires_api_key", False)
                    if not str(pcfg.get("base_url", "")).strip():
                        pcfg["base_url"] = "local://" + str(pcfg.get("kind", "local"))
                providers[name] = pcfg
    merged["providers"] = providers

    # 2. main_chat → global_default + fallback_chain
    main_chat = llm.get("main_chat", {})
    if isinstance(main_chat, dict):
        mc_provider = str(main_chat.get("provider", "")).strip()
        mc_model = str(main_chat.get("model", "")).strip()
        if mc_provider and mc_model:
            merged["global_default"] = {"profile": mc_provider, "model": mc_model}
            # companion vault 統一由 companion_config.yaml.main_chat 管主對話 —
            # 清掉 llm_router.yaml 殘留的 persona_overrides (否則 persona 層優先於 global
            # → 主對話走舊 persona model, main_chat 設的新 model 被蓋掉).
            merged["persona_overrides"] = {}
            fb: list[dict[str, str]] = []
            for item in main_chat.get("fallback_chain", []) or []:
                if isinstance(item, dict):
                    fp = str(item.get("provider", "")).strip()
                    fm = str(item.get("model", "")).strip()
                    if fp and fm:
                        fb.append({"profile": fp, "model": fm})
            if fb:
                merged["fallback_chain"] = fb

    # 3. sub_tasks → auxiliary_overrides (provider→profile, 推 model)
    aux = dict(merged.get("auxiliary_overrides", {}) if isinstance(merged.get("auxiliary_overrides"), dict) else {})
    sub_tasks = llm.get("sub_tasks", {})
    if isinstance(sub_tasks, dict):
        for task, tcfg in sub_tasks.items():
            if not isinstance(tcfg, dict):
                continue
            prov = str(tcfg.get("provider", "")).strip()
            if not prov:
                continue
            prov_cfg = providers.get(prov, {}) if isinstance(providers.get(prov), dict) else {}
            # model 來源優先序: sub_task.model > provider.model_path (本地 GGUF) > provider.model
            model_ref = (
                str(tcfg.get("model", "")).strip()
                or str(prov_cfg.get("model_path", "")).strip()
                or str(prov_cfg.get("model", "")).strip()
            )
            if prov and model_ref:
                aux[task] = {"profile": prov, "model": model_ref}
    if aux:
        merged["auxiliary_overrides"] = aux

    return merged


def load_llm_router_config(vault_root: Path) -> dict[str, Any]:
    """Load routing config and auto-bootstrap defaults when missing.

    V3-O.10 #3+#18: companion vault 會 merge companion_config.yaml.llm
    (main_chat/sub_tasks/providers) 進來, 讓子任務本地 gemma 分流生效.
    """

    path = ensure_llm_router_file(vault_root)
    raw = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(raw) or {}
    if not isinstance(payload, dict):
        raise ValueError("llm_router.yaml 格式錯誤：必須是 YAML object")
    # V3-O.10: companion vault 把 companion_config.yaml.llm 疊上來
    payload = _merge_companion_llm_config(Path(vault_root).expanduser().resolve(), payload)
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
    auxiliary: str | None = None,
) -> dict[str, Any]:
    """Resolve effective model chain from override/auxiliary/persona/global/fallback.

    R21 C111 (A1): auxiliary kwarg 新加 — 子任務 LLM 分流 (curator/umbrella/triage 等
    走 cheap model, 不吃 persona 主要 model). Priority order:
      override > auxiliary > persona > global > fallback_chain
    """

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

    # R21 C111 (A1): auxiliary 層 — auxiliary_overrides[name] > auxiliary_default
    aux_profile = ""
    aux_model = ""
    if auxiliary:
        aux_overrides = config.get("auxiliary_overrides", {})
        if isinstance(aux_overrides, dict):
            aux_entry = aux_overrides.get(auxiliary, {})
            if isinstance(aux_entry, dict):
                aux_profile = str(aux_entry.get("profile", "")).strip()
                aux_model = str(aux_entry.get("model", "")).strip()
        if not aux_profile or not aux_model:
            aux_default = config.get("auxiliary_default", {})
            if isinstance(aux_default, dict):
                aux_profile = aux_profile or str(aux_default.get("profile", "")).strip()
                aux_model = aux_model or str(aux_default.get("model", "")).strip()

    selected_profile = (
        (override_profile or "").strip()
        or aux_profile
        or str(persona_entry.get("profile", "")).strip()
        or str(global_default.get("profile", "")).strip()
    )
    selected_model = _normalize_model_ref(
        override_model
        or aux_model
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
