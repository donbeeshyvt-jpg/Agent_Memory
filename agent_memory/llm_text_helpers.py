"""LLM text/JSON helpers — R11 C41: 統一 prompt→response 包裝.

R9/R10 引入 5 個 LLM-using 模組 (umbrella_llm / weekly_digest / reflect / gap_analysis /
external_ingest_summarize) 各自有 `_default_call_llm(prompt)` 寫法, 但 5 個都呼叫
`LLMClient()` 缺 vault_root + `generate(prompt=, max_tokens=)` 用了不存在的 kwarg —
真實 LLM 從未成功 call, 只有 mock_response 走得通 (HANDOFF §4.3 known issue).

本檔提供 3 個共用 helper, 統一:
- 取得 LLMClient(vault_root)
- 把 prompt 包進 messages=[{role:user, content:prompt}]
- 用正確 .generate(messages=, persona_id=, temperature=, timeout_s=)
- 解析 LLM 回傳成 str / dict / list[dict]

設計重點 (對齊 MISSION §5.4):
- LLM 不可用時拋 Exception 讓 caller fallback skip — 不阻擋 curator 其他 step
- 不在 retrieve-time call (caller 都是 sleep-cycle / on-demand)
- mock_response 上層 caller 自己 short-circuit, 不進本檔
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_first_json_block(text: str, *, expect_array: bool = False) -> str:
    """從 LLM 回傳抽第一段 JSON. 處理 LLM 包 ```json ... ``` 或 ``` ... ``` fence.

    expect_array=True 找 `[...]`, False 找 `{...}`.
    回傳裸 JSON 字串 (沒 fence). 若找不到, 回原 text.
    """
    open_char, close_char = ("[", "]") if expect_array else ("{", "}")
    text = text.strip()
    # 1. 試 ```json ... ``` fence
    m = _JSON_FENCE_RE.search(text)
    if m:
        inner = m.group(1).strip()
        if inner.startswith(open_char) and inner.endswith(close_char):
            return inner
    # 2. 試裸 JSON
    if text.startswith(open_char) and text.endswith(close_char):
        return text
    # 3. fallback: 找第一個 open_char 到最後一個 close_char
    start = text.find(open_char)
    end = text.rfind(close_char)
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def call_llm_for_text(
    vault_root: Path,
    prompt: str,
    *,
    persona_id: str = "steward",
    temperature: float = 0.2,
    timeout_s: float = 60.0,
    auxiliary: str | None = None,
) -> str:
    """Prompt → plain text. LLM 不可用拋 Exception.

    Args:
        vault_root: vault 根 (從 llm_router.yaml 讀路由)
        prompt: 純文字 prompt (會包成 user message)
        persona_id: 用哪個 persona 的路由設定 (預設 steward)
        temperature: LLM 溫度
        timeout_s: 單次 generate 超時
        auxiliary: R21.x C117 — 子任務 name (umbrella/curator/triage/etc), 傳給
                   LLMClient.generate 走 auxiliary_overrides 路由. None = 既有行為.

    Returns: LLM 回傳純文字 (strip 過).

    Raises: LLMClientError / RuntimeError / TypeError 等 — caller 該包 try/except fallback.
    """
    from agent_memory.llm_client import LLMClient  # lazy 避循環

    client = LLMClient(Path(vault_root).expanduser().resolve())
    messages = [{"role": "user", "content": prompt}]
    result = client.generate(
        messages=messages,
        persona_id=persona_id,
        temperature=temperature,
        timeout_s=timeout_s,
        auxiliary=auxiliary,
    )
    content = result.content if hasattr(result, "content") else str(result)
    return content.strip()


def call_llm_for_json(
    vault_root: Path,
    prompt: str,
    *,
    persona_id: str = "steward",
    temperature: float = 0.1,
    timeout_s: float = 60.0,
    auxiliary: str | None = None,
) -> dict[str, Any]:
    """Prompt → JSON object (dict). LLM 不可用 / 回非 JSON → 拋 Exception.

    自動 strip ```json ... ``` fence + 找第一個 {} 塊.
    R21.x C117: 加 auxiliary kwarg propagate 給 call_llm_for_text.
    """
    text = call_llm_for_text(
        vault_root,
        prompt,
        persona_id=persona_id,
        temperature=temperature,
        timeout_s=timeout_s,
        auxiliary=auxiliary,
    )
    raw = _extract_first_json_block(text, expect_array=False)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


def call_llm_for_json_list(
    vault_root: Path,
    prompt: str,
    *,
    persona_id: str = "steward",
    temperature: float = 0.1,
    timeout_s: float = 60.0,
    auxiliary: str | None = None,
) -> list[dict[str, Any]]:
    """Prompt → JSON array of objects. LLM 不可用 / 回非 array → 拋 Exception.

    自動 strip ```json ... ``` fence + 找第一個 [] 塊.
    R21.x C117: 加 auxiliary kwarg propagate.
    """
    text = call_llm_for_text(
        vault_root,
        prompt,
        persona_id=persona_id,
        temperature=temperature,
        timeout_s=timeout_s,
        auxiliary=auxiliary,
    )
    raw = _extract_first_json_block(text, expect_array=True)
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"expected JSON array, got {type(parsed).__name__}")
    out: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            out.append(item)
    return out
