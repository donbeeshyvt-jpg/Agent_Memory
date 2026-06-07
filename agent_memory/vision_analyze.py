# -*- coding: utf-8 -*-
"""V3-O.15.25 (2026-06-07 user 拍板): 看圖線路 — 內部 vision LLM 分析圖片.

用戶傳圖片/伺服器貼圖時, 由 attachment_ingest 呼叫本模組, 用 openrouter 上的 vision 模型
(預設 qwen/qwen3.6-35b-a3b, user 指定) 分析圖片 → 回繁中描述 → 由 attachment_ingest 夾進
<attachment> XML block → prepend 進 current_user_message → 收束 prompt → bot 能「看到」圖回應.

自包含: 用 urllib (同 llm_client) 直打 openrouter multimodal API, 不依賴 llm_client 的純文字路徑.
失敗一律回 "" (不阻擋主流程; attachment_ingest 會 fallback 成「收到圖但沒分析出」).
"""
from __future__ import annotations

import json
import os
from urllib import request as _url_request

# V3-O.15.25: user 拍板 qwen/qwen3.6-flash (Flash 版支援視覺; a3b 純文字版不支援 → 400).
# 可換其他 vision 模型: google/gemini-2.5-flash (描述最細) / qwen/qwen2.5-vl-72b-instruct / openai/gpt-4o-mini
DEFAULT_VISION_MODEL = "qwen/qwen3.6-flash"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_PROMPT = (
    "請用繁體中文簡短客觀描述這張圖片/貼圖的內容: 主體是什麼、人物/角色與表情動作、"
    "氛圍情緒、若有文字也念出來。≤150 字, 只描述看到的內容, 不要評論或加戲。"
)


def analyze_image_url(
    image_url: str,
    *,
    prompt: str = "",
    model: str = DEFAULT_VISION_MODEL,
    timeout_s: float = 60.0,
    max_tokens: int = 400,
) -> str:
    """呼叫 openrouter vision 模型描述圖片. 回描述文字; 失敗回 ''."""
    if not image_url or not str(image_url).strip():
        return ""
    api_key = (os.getenv("OPENROUTER_API_KEY", "").strip()
               or os.getenv("OPENROUTER_API_KEY_SUBTASK", "").strip())
    if not api_key:
        return ""
    body = json.dumps({
        "model": model,
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
    req = _url_request.Request(_OPENROUTER_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with _url_request.urlopen(req, timeout=timeout_s) as resp:
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
