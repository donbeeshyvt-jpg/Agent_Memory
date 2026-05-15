"""Local tool execution helpers for tool-enabled personas."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TOOL_PREFIX = "/tool"
_ALLOWED_ACTIONS = {
    "list_dir",
    "read_file",
    "write_file",
    "append_file",
    "mkdir",
}


def _parse_fallback_payload(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    if not text:
        return {}
    payload: dict[str, Any] = {}
    for chunk in text.split(","):
        token = chunk.strip()
        if not token:
            continue
        sep = ":" if ":" in token else "=" if "=" in token else ""
        if not sep:
            continue
        key_raw, value_raw = token.split(sep, 1)
        key = key_raw.strip().strip("\"'").lower()
        value = value_raw.strip().strip("\"'")
        if not key:
            continue
        lowered = value.lower()
        if lowered in ("true", "false"):
            payload[key] = lowered == "true"
            continue
        try:
            payload[key] = int(value)
            continue
        except Exception:  # noqa: BLE001
            pass
        payload[key] = value
    return payload


def maybe_parse_tool_request(message: str) -> dict[str, Any] | None:
    text = str(message or "").strip()
    if not text.lower().startswith(_TOOL_PREFIX):
        return None
    raw = text[len(_TOOL_PREFIX) :].strip()
    if not raw:
        raise ValueError("tool request missing JSON payload. Example: /tool {\"action\":\"list_dir\",\"path\":\".\"}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = _parse_fallback_payload(raw)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("tool request must be a JSON object")
    return payload


def _safe_int(raw: Any, fallback: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except Exception:  # noqa: BLE001
        value = fallback
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _resolve_path(root: Path, path_value: str) -> Path:
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError("path is required")
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(f"path escapes root: {raw}") from exc
    return candidate


def _target_root(*, vault_root: Path, workspace_root: Path, target: str | None) -> tuple[str, Path]:
    key = str(target or "workspace").strip().lower()
    if key in ("workspace", "project", "repo"):
        return "workspace", workspace_root.resolve()
    if key in ("vault", "memory"):
        return "vault", vault_root.resolve()
    raise ValueError("target must be workspace or vault")


def execute_tool_request(
    *,
    vault_root: Path,
    workspace_root: Path,
    request: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("request must be object")
    action = str(request.get("action", "")).strip().lower()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"unsupported action: {action}")

    target_name, root = _target_root(
        vault_root=Path(vault_root),
        workspace_root=Path(workspace_root),
        target=str(request.get("target", "workspace")),
    )
    rel_path = str(request.get("path", "")).strip()
    path = _resolve_path(root, rel_path or ".")
    payload: dict[str, Any] = {
        "action": action,
        "target": target_name,
        "root": str(root),
        "path": str(path),
        "ok": True,
    }

    if action == "list_dir":
        limit = _safe_int(request.get("limit", 200), 200, min_value=1, max_value=500)
        if not path.exists():
            raise FileNotFoundError(f"directory not found: {path}")
        if not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        rows: list[dict[str, Any]] = []
        for idx, item in enumerate(sorted(path.iterdir(), key=lambda p: p.name.lower())):
            if idx >= limit:
                break
            rows.append(
                {
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                }
            )
        payload["items"] = rows
        payload["count"] = len(rows)
        return payload

    if action == "read_file":
        max_chars = _safe_int(request.get("max_chars", 12000), 12000, min_value=200, max_value=200000)
        encoding = str(request.get("encoding", "utf-8")).strip() or "utf-8"
        if not path.exists():
            raise FileNotFoundError(f"file not found: {path}")
        if not path.is_file():
            raise ValueError(f"not a file: {path}")
        text = path.read_text(encoding=encoding)
        clipped = text[:max_chars]
        payload["encoding"] = encoding
        payload["content"] = clipped
        payload["truncated"] = len(clipped) < len(text)
        payload["char_count"] = len(clipped)
        return payload

    if action in ("write_file", "append_file"):
        content = request.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        encoding = str(request.get("encoding", "utf-8")).strip() or "utf-8"
        create_parents = bool(request.get("create_parents", True))
        if create_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        if action == "write_file":
            overwrite = bool(request.get("overwrite", True))
            if path.exists() and not overwrite:
                raise FileExistsError(f"file exists: {path}")
            path.write_text(content, encoding=encoding)
        else:
            with path.open("a", encoding=encoding) as fh:
                fh.write(content)
        payload["encoding"] = encoding
        payload["bytes"] = path.stat().st_size if path.exists() else 0
        return payload

    if action == "mkdir":
        path.mkdir(parents=bool(request.get("parents", True)), exist_ok=bool(request.get("exist_ok", True)))
        payload["exists"] = path.exists()
        payload["is_dir"] = path.is_dir()
        return payload

    raise ValueError(f"unsupported action: {action}")


def render_tool_result(result: dict[str, Any]) -> str:
    action = str(result.get("action", ""))
    target = str(result.get("target", ""))
    path = str(result.get("path", ""))
    if action == "list_dir":
        rows = result.get("items", [])
        if not isinstance(rows, list):
            rows = []
        preview = ", ".join([str(item.get("name", "")) for item in rows[:20] if isinstance(item, dict)])
        return f"[tool:list_dir] target={target} path={path} count={len(rows)} items={preview}"
    if action == "read_file":
        content = str(result.get("content", ""))
        return f"[tool:read_file] target={target} path={path}\n{content}"
    if action in ("write_file", "append_file"):
        return f"[tool:{action}] target={target} path={path} bytes={result.get('bytes', 0)}"
    if action == "mkdir":
        return f"[tool:mkdir] target={target} path={path} exists={result.get('exists', False)}"
    return json.dumps(result, ensure_ascii=False)


# ============================================================
# /llm slash command — 對話中切換 LLM 模型
# ============================================================
_LLM_PREFIX = "/llm"

# key → (profile, model, human-readable label)
_LLM_PRESETS: dict[str, tuple[str, str, str]] = {
    # 本地 GGUF
    "gemma4": (
        "llama_cpp_local",
        "../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf",
        "本機 gemma-4 E4B (Q8)",
    ),
    "qwen9": (
        "llama_cpp_local",
        "../../0_Models/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q8_0.gguf",
        "本機 Qwen3.5-9B (Q8)",
    ),
    "qwen30": (
        "llama_cpp_local",
        "../../0_Models/Qwen3-30B-A3B-GGUF/Qwen3-30B-A3B-UD-Q4_K_XL.gguf",
        "本機 Qwen3-30B-A3B (Q4_K_XL)",
    ),
    # Google Gemini / Gemma API（4 個）
    "gemini": (
        "gemini",
        "gemini-2.5-flash",
        "Google Gemini 2.5 Flash",
    ),
    "gemini-pro": (
        "gemini",
        "gemini-2.5-pro",
        "Google Gemini 2.5 Pro",
    ),
    "gemma-31b": (
        "gemini",
        "gemma-4-31b-it",
        "Google Gemma 4 31B",
    ),
    "gemma-26b": (
        "gemini",
        "gemma-4-26b-a4b-it",
        "Google Gemma 4 26B-A4B",
    ),
}


def maybe_parse_llm_switch_request(message: str) -> dict[str, Any] | None:
    """Parse /llm <key> | /llm persona <id> <key> | /llm list | /llm show | /llm help.

    Returns None if message is not a /llm command. Raises ValueError on malformed input.
    """
    text = str(message or "").strip()
    if not text.lower().startswith(_LLM_PREFIX):
        return None
    rest = text[len(_LLM_PREFIX):].strip()
    if not rest:
        return {"action": "help"}
    parts = rest.split(None, 2)
    cmd = parts[0].lower()
    if cmd in ("help", "?"):
        return {"action": "help"}
    if cmd == "list":
        return {"action": "list"}
    if cmd == "show":
        return {"action": "show"}
    if cmd == "persona":
        if len(parts) < 3:
            raise ValueError("用法：/llm persona <persona_id> <key>")
        return {
            "action": "switch_persona",
            "persona": parts[1].strip(),
            "key": parts[2].strip().lower(),
        }
    # 一般情況：/llm <key>
    return {
        "action": "switch_default",
        "key": cmd,
    }


def execute_llm_switch(vault_root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Execute /llm switch request. Returns result dict."""
    # 延遲 import 避免 circular dependency
    from agent_memory.llm_routing import load_llm_router_config, save_llm_router_config

    action = request.get("action")
    if action == "help":
        return {
            "ok": True,
            "action": "help",
            "presets": list(_LLM_PRESETS.keys()),
        }
    if action == "list":
        return {
            "ok": True,
            "action": "list",
            "presets": [
                {"key": k, "profile": v[0], "model": v[1], "label": v[2]}
                for k, v in _LLM_PRESETS.items()
            ],
        }
    if action == "show":
        cfg = load_llm_router_config(vault_root)
        return {
            "ok": True,
            "action": "show",
            "global_default": cfg.get("global_default", {}),
            "persona_overrides": cfg.get("persona_overrides", {}),
        }
    if action in ("switch_default", "switch_persona"):
        key = str(request.get("key", "")).strip().lower()
        preset = _LLM_PRESETS.get(key)
        if not preset:
            return {
                "ok": False,
                "error": f"unknown_llm_key: {key}",
                "available": list(_LLM_PRESETS.keys()),
            }
        profile, model, label = preset
        cfg = load_llm_router_config(vault_root)
        if action == "switch_default":
            if not isinstance(cfg.get("global_default"), dict):
                cfg["global_default"] = {}
            cfg["global_default"]["profile"] = profile
            cfg["global_default"]["model"] = model
        else:
            persona = str(request.get("persona", "")).strip()
            if not persona:
                return {"ok": False, "error": "persona required"}
            if not isinstance(cfg.get("persona_overrides"), dict):
                cfg["persona_overrides"] = {}
            cfg["persona_overrides"][persona] = {"profile": profile, "model": model}
        save_llm_router_config(vault_root, cfg)
        return {
            "ok": True,
            "action": action,
            "profile": profile,
            "model": model,
            "label": label,
            "persona": request.get("persona"),
        }
    return {"ok": False, "error": f"unknown_action: {action}"}


def render_llm_switch_result(result: dict[str, Any]) -> str:
    """Convert result dict to user-friendly message."""
    if not result.get("ok"):
        err = result.get("error", "unknown")
        avail = result.get("available")
        if avail:
            return f"[llm:err] {err}\n可用 key: {', '.join(avail)}"
        return f"[llm:err] {err}"
    action = result.get("action")
    if action == "help":
        presets = result.get("presets", [])
        return (
            "[llm:help] 對話中切模型：\n"
            "  /llm <key>                   切全域預設\n"
            "  /llm persona <id> <key>      切某 persona 專屬\n"
            "  /llm list                    列出全部 preset\n"
            "  /llm show                    看目前設定\n"
            f"  可用 key: {', '.join(presets)}"
        )
    if action == "list":
        presets = result.get("presets", [])
        lines = ["[llm:list] 可用 preset:"]
        for p in presets:
            key = str(p.get("key", "")).ljust(14)
            label = p.get("label", "")
            lines.append(f"  {key}{label}")
        return "\n".join(lines)
    if action == "show":
        gd = result.get("global_default", {})
        po = result.get("persona_overrides", {})
        lines = [f"[llm:show] global_default: {gd.get('profile')} / {gd.get('model')}"]
        if po:
            lines.append("persona_overrides:")
            for k, v in po.items():
                if isinstance(v, dict):
                    lines.append(f"  {k}: {v.get('profile')} / {v.get('model')}")
        else:
            lines.append("persona_overrides: (無)")
        return "\n".join(lines)
    if action == "switch_default":
        label = result.get("label", "")
        profile = result.get("profile", "")
        model = result.get("model", "")
        return f"[llm:switched] 預設模型已切到 {label}\n  ({profile} / {model})\n下一條訊息會用新模型。"
    if action == "switch_persona":
        persona = result.get("persona", "")
        label = result.get("label", "")
        profile = result.get("profile", "")
        model = result.get("model", "")
        return f"[llm:switched-persona] {persona} 已切到 {label}\n  ({profile} / {model})\n下一條訊息會用新模型。"
    return json.dumps(result, ensure_ascii=False)


# ============================================================
# Phase A C3 (A.5): Agent autonomous memory tool calling
# ------------------------------------------------------------
# 跟 /tool slash command 不同, 這是讓 LLM 在回應中自主呼叫 memory 工具.
# LLM 輸出格式: [TOOL]memory{"action":"add","path":"...","content":"..."}[/TOOL]
# 沙盒: 透過 MemoryRuntime.apply_memory_tool 走 profile.can_write 治理,
#       禁止寫入 20/80/90 raw 區 + 限制在 vault 內.
# ============================================================

# Regex 抓 [TOOL]<name>{...json...}[/TOOL] block. multiline + non-greedy.
_AGENT_TOOL_PATTERN = re.compile(
    r"\[TOOL\]\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(\{.*?\})\s*\[/TOOL\]",
    re.DOTALL,
)


def parse_agent_tool_calls(response_text: str) -> list[dict[str, Any]]:
    """Parse `[TOOL]<name>{json}[/TOOL]` blocks from LLM response.

    Returns list of `{"tool": str, "args": dict, "raw": str}`.
    `raw` is the matched substring (used for stripping later).
    Invalid JSON entries are skipped with `args={"_parse_error": "..."}`.
    """
    if not response_text:
        return []
    out: list[dict[str, Any]] = []
    for m in _AGENT_TOOL_PATTERN.finditer(response_text):
        tool_name = m.group(1).strip()
        json_blob = m.group(2)
        raw_block = m.group(0)
        try:
            args = json.loads(json_blob)
            if not isinstance(args, dict):
                args = {"_parse_error": "args must be JSON object"}
        except json.JSONDecodeError as exc:
            args = {"_parse_error": f"json decode: {exc.msg}"}
        out.append({"tool": tool_name, "args": args, "raw": raw_block})
    return out


def strip_agent_tool_blocks(response_text: str) -> str:
    """Remove `[TOOL]...[/TOOL]` blocks from response (for user-facing display)."""
    if not response_text:
        return ""
    cleaned = _AGENT_TOOL_PATTERN.sub("", response_text)
    # 清理因移除 tool block 留下的多餘空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def build_agent_tools_prompt(
    *,
    write_allow: list[str],
    write_deny: list[str],
    enabled: bool = True,
) -> str:
    """Build system prompt section describing available tools to the LLM.

    Returns empty string if `enabled=False` (persona has tools_enabled=false).
    """
    if not enabled:
        return ""
    allow_lines = "\n".join(f"    - `{p}`" for p in write_allow) or "    (無)"
    deny_lines = "\n".join(f"    - `{p}` (永遠唯讀)" for p in write_deny) or "    (無)"
    return (
        "\n\n=== AVAILABLE TOOLS (你可以在回應中自主呼叫) ===\n"
        "你有「自主寫入第二大腦」的能力。當使用者:\n"
        "  • 告訴你一個事實 / 偏好 / 重要資訊 (例如「我叫阿凱」「我偏好簡潔回覆」)\n"
        "  • 請你記住某件事\n"
        "  • 對話中產生值得留底的結論 / 決策\n"
        "你可以在回應中**直接呼叫 memory 工具寫入**, 不用問使用者「要不要存」(他都已經告訴你了).\n\n"
        "## 呼叫格式\n\n"
        "```\n"
        "[TOOL]memory{\"action\":\"add\",\"path\":\"10_Permanent/Manual_Inputs/<檔名>.md\",\"content\":\"<完整 markdown>\",\"reason\":\"<為何記住>\"}[/TOOL]\n"
        "```\n\n"
        "- `action`: `add` (新建) / `replace` (覆蓋) / `remove` (刪除) / `get` (讀)\n"
        "- `path`: vault 相對路徑 (見下方允許區)\n"
        "- `content`: markdown 內容 (add/replace 必要)。建議格式: 加 YAML frontmatter (參考既有 .md) + `<summary>` + `<context>` XML 標籤\n"
        "- `reason`: 1-2 句話說明為何要寫 (台帳用)\n\n"
        "## 沙盒邊界 (寫入允許區)\n\n"
        f"{allow_lines}\n\n"
        "## 禁區 (永遠不能寫)\n\n"
        f"{deny_lines}\n\n"
        "## 寫入規則\n\n"
        "- **使用者偏好 / 個人事實** → 寫 `10_Permanent/Manual_Inputs/<topic>.md`\n"
        "- **跨對話通用知識** → 寫 `10_Permanent/Facts/<topic>.md` 或 `10_Permanent/Concepts/<topic>.md`\n"
        "- **本 session 工作上下文** → 自動寫到 session log, 不需手動 call tool\n"
        "- **不要重複寫**: 先用 `get` 確認檔不存在再 `add`; 已存在用 `replace`\n"
        "- **不要寫敏感資料** (token / API key / 私密對話) 到一般區, 必要時加 `security_level: confidential` frontmatter\n\n"
        "## 範例\n\n"
        "使用者: 我偏好簡潔技術回覆\n"
        "你的回應:\n"
        "```\n"
        "好的, 已記住你偏好簡潔技術回覆。下次對話會用這風格。\n"
        "[TOOL]memory{\"action\":\"add\",\"path\":\"10_Permanent/Manual_Inputs/style_concise_tech.md\",\"content\":\"---\\ntype: user_profile\\nsource: user\\ntags: [manual_input, style]\\nai_ready: true\\netl_status: internalised\\nsecurity_level: safe_data\\n---\\n\\n# 對話風格偏好\\n\\n<summary>\\n使用者偏好簡潔、技術導向的回覆, 不要過度禮貌或鋪陳。\\n</summary>\\n\\n<context>\\n- 直接給結論 + 步驟\\n- 跳過寒暄\\n- 程式碼 + 路徑優先於敘述\\n</context>\",\"reason\":\"user_stated_preference\"}[/TOOL]\n"
        "```\n\n"
        "## 重要原則\n\n"
        "1. **正常回應在前, tool block 在後** — 不要只回 tool block, 使用者也要看得到你的回應\n"
        "2. **tool block 會被自動移除不顯示給使用者** — 你不用解釋「我即將呼叫 tool」, 直接 call 即可\n"
        "3. **執行結果會在下次對話告知你** — 第一次 call 是「fire and forget」, 不要假設你看得到結果\n"
        "4. **錯誤時系統會把錯誤訊息加在你的回應末端** — 使用者看得到「[tool error: ...]」, 之後你可以對話中修正\n"
        "=== END TOOLS ===\n"
    )


def execute_agent_tool_call(
    runtime: Any,
    call: dict[str, Any],
    *,
    operator: str = "agent",
) -> dict[str, Any]:
    """Execute a single parsed tool call via MemoryRuntime.apply_memory_tool.

    Returns: `{"tool": str, "ok": bool, "path": str, "action": str, "message": str, "error": str}`.
    Path treatment governance is enforced inside apply_memory_tool (raises PermissionError on raw zones).
    """
    tool_name = str(call.get("tool", "")).lower()
    args = call.get("args", {})
    result = {
        "tool": tool_name,
        "ok": False,
        "path": str(args.get("path", "")),
        "action": str(args.get("action", "")),
        "message": "",
        "error": "",
    }
    if "_parse_error" in args:
        result["error"] = f"args parse failed: {args['_parse_error']}"
        return result
    if tool_name != "memory":
        result["error"] = f"unsupported tool: {tool_name}"
        return result

    action = str(args.get("action", "")).strip().lower()
    path = str(args.get("path", "")).strip()
    content = args.get("content", "")
    reason = str(args.get("reason", "")).strip()

    if action not in ("add", "replace", "remove", "get"):
        result["error"] = f"unsupported memory action: {action}"
        return result
    if not path:
        result["error"] = "memory path is required"
        return result

    try:
        op = runtime.apply_memory_tool(
            action=action,
            path=path,
            content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            reason=reason,
            agent=operator,
        )
        result["ok"] = bool(op.ok)
        result["message"] = str(op.message)
        if op.note is not None and not result["path"]:
            result["path"] = str(op.note.path)
    except PermissionError as exc:
        result["error"] = f"permission denied: {exc}"
    except ValueError as exc:
        result["error"] = f"invalid: {exc}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def render_agent_tool_summary(results: list[dict[str, Any]]) -> str:
    """Build a human-readable footer summarizing tool execution.

    Appended to user-facing response after tool blocks are stripped.
    """
    if not results:
        return ""
    lines = ["", "---", "[已執行 agent 工具]"]
    for r in results:
        tool = r.get("tool", "?")
        action = r.get("action", "?")
        path = r.get("path", "")
        if r.get("ok"):
            lines.append(f"  ✓ {tool}.{action} {path}  ({r.get('message', 'ok')})")
        else:
            err = r.get("error") or r.get("message", "failed")
            lines.append(f"  ✗ {tool}.{action} {path}  [error: {err}]")
    return "\n".join(lines)
