# -*- coding: utf-8 -*-
"""V3-O.15.25/26 (2026-06-07 user 拍板): 看圖線路 — 內部 vision LLM 分析圖片.

用戶傳圖片/伺服器貼圖時, 由 attachment_ingest 呼叫本模組, 用 vision 模型分析圖片 → 回繁中描述
→ 由 attachment_ingest 夾進 <attachment> XML block → prepend 進 current_user_message
→ 收束 prompt → bot 能「看到」圖回應.

V3-O.15.26: 模型/provider/timeout 全由 companion_config.yaml 的 `llm.sub_tasks.image_analysis`
決定 (每次呼叫重讀, 改 yaml 存檔即生效, 不用重啟 — 對齊統一設定檔精神). 讀不到才用預設.

自包含: 用 urllib (同 llm_client) 直打 openai-compatible multimodal API.
失敗一律回 "" (不阻擋主流程; attachment_ingest 會 fallback 成「收到圖但沒分析出」).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib import request as _url_request

# 預設 (companion_config.yaml 沒設 image_analysis 時的 fallback).
# 注意: 純文字模型 (如 qwen3.6-35b-a3b) 不支援視覺會 400; 要選有視覺的 (qwen3.6-flash 可).
DEFAULT_VISION_MODEL = "qwen/qwen3.6-flash"
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
_DEFAULT_PROMPT = (
    "請用繁體中文簡短客觀描述這張圖片/貼圖的內容: 主體是什麼、人物/角色與表情動作、"
    "氛圍情緒、若有文字也念出來。≤150 字, 只描述看到的內容, 不要評論或加戲。"
)


def _resolve_vision_config(vault_root) -> tuple[str, str, str, float]:
    """讀 companion_config.yaml 的 llm.sub_tasks.image_analysis → (model, endpoint_url, api_key, timeout_s).

    每次呼叫重讀 (改 yaml 即生效, 不用重啟). 讀不到 → 用預設.
    provider 解析自 llm.providers[<provider>] 的 base_url + api_key_env.
    """
    model = DEFAULT_VISION_MODEL
    base_url = _DEFAULT_BASE_URL
    api_key_env = _DEFAULT_KEY_ENV
    timeout_s = 60.0
    if vault_root:
        try:
            import yaml
            cfg_path = Path(vault_root) / "00_System_Core" / "companion_config.yaml"
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            llm = cfg.get("llm", {}) or {}
            ia = (llm.get("sub_tasks", {}) or {}).get("image_analysis", {}) or {}
            if ia.get("model"):
                model = str(ia["model"]).strip()
            if ia.get("timeout_s"):
                timeout_s = float(ia["timeout_s"])
            prov = (llm.get("providers", {}) or {}).get(str(ia.get("provider", "openrouter")), {}) or {}
            if prov.get("base_url"):
                base_url = str(prov["base_url"]).strip()
            if prov.get("api_key_env"):
                api_key_env = str(prov["api_key_env"]).strip()
        except Exception:
            pass
    api_key = (os.getenv(api_key_env, "").strip()
               or os.getenv("OPENROUTER_API_KEY", "").strip()
               or os.getenv("OPENROUTER_API_KEY_SUBTASK", "").strip())
    return model, base_url.rstrip("/") + "/chat/completions", api_key, timeout_s


def analyze_image_url(
    image_url: str,
    *,
    vault_root=None,
    prompt: str = "",
    model: str = "",
    timeout_s: float = 0.0,
    max_tokens: int = 400,
) -> str:
    """呼叫 vision 模型描述圖片. 模型/端點/key 由 companion_config.yaml 決定 (顯式參數可覆蓋).
    回描述文字; 失敗回 ''."""
    if not image_url or not str(image_url).strip():
        return ""
    _model, _url, _key, _timeout = _resolve_vision_config(vault_root)
    if model:
        _model = model
    if timeout_s and timeout_s > 0:
        _timeout = timeout_s
    if not _key:
        return ""
    body = json.dumps({
        "model": _model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt or _DEFAULT_PROMPT},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode("utf-8")
    req = _url_request.Request(_url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with _url_request.urlopen(req, timeout=_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message", {}) or {}
        content = msg.get("content", "")
        if isinstance(content, list):  # 某些模型回 content array
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip()
    except Exception:
        return ""
