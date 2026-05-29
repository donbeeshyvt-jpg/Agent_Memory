"""Minimal LLM client with profile routing and fallback chain."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import gc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

from agent_memory.llm_routing import load_llm_router_config, resolve_llm_route


class LLMClientError(RuntimeError):
    """Raised when all provider attempts fail."""


@dataclass(slots=True)
class LLMAttemptFailure:
    """One failed model attempt in fallback chain."""

    profile: str
    model: str
    reason: str


@dataclass(slots=True)
class LLMGenerateResult:
    """Successful generation result with route metadata."""

    content: str
    profile: str
    model: str
    provider_kind: str
    base_url: str
    attempts: list[LLMAttemptFailure] = field(default_factory=list)


_THINK_BLOCK_RE = re.compile(r"<think>\s*.*?\s*</think>\s*", flags=re.IGNORECASE | re.DOTALL)
_LLAMA_CACHE_LOCK = threading.Lock()
_LLAMA_CACHE: dict[tuple[str, int, int, int, int, bool, str], Any] = {}


def _build_llm_concurrency_primitive() -> threading.BoundedSemaphore:
    """V3-O.6.2 #8 (user 2026-05-28): LLM 並行原語.

    第 4 輪 viewer barrage 22/62 timeout 全卡 lock — V3-O.6 #6 把 lock 拉到 120s 是
    *延長等待* 治標, 但人多時根本是 serialize 排隊 → 同時 3-5 viewer 也只能 1 by 1.

    改用 BoundedSemaphore(N), env AGENT_MEMORY_LLM_PARALLEL 覆蓋:
      - N=1 (預設, backward compat) — 跟舊 Lock 一樣 strict serial
      - N=2~3 — viewer barrage 場景, 2-3 LLM 平行跑
      - N>3 — 對 OpenRouter free tier 風險 rate-limit, 留 user 自決

    BoundedSemaphore.acquire(timeout=x) API 跟 Lock 一致, caller 不用改.
    """
    raw = os.getenv("AGENT_MEMORY_LLM_PARALLEL", "1").strip()
    try:
        n = max(1, int(raw))
    except ValueError:
        n = 1
    return threading.BoundedSemaphore(n)


_LLM_GENERATE_LOCK = _build_llm_concurrency_primitive()

# V3-O.10 #3 (Q1 fix): per-provider lock pool — 主對話 vs 子任務分離鎖
# 子任務 (auxiliary != None) 用 _LLM_SUB_TASK_LOCK, 不再跟主對話搶 _LLM_GENERATE_LOCK.
# 解 B2: self_modification / umbrella_consolidation 等不再阻塞 owner 主對話.
_LLM_SUB_TASK_LOCK = threading.BoundedSemaphore(2)  # 子任務最多 2 個並行

# V3-O.10 #5: Priority Queue — owner > VIP > viewer 排隊優先
# 實作: PriorityQueue + worker thread pool，owner priority=0, VIP=1, viewer=2
import queue as _queue
import concurrent.futures as _cf
import concurrent.futures  # noqa: F811 — needed for type annotation

_PRIORITY_LEVELS = {"owner": 0, "vip": 1, "viewer": 2}
_LLM_PRIORITY_QUEUE: _queue.PriorityQueue = _queue.PriorityQueue()
_LLM_PRIORITY_WORKER_COUNT = 2  # 同時最多 2 個 LLM 並行
_LLM_PRIORITY_INITIALIZED = False
_LLM_PRIORITY_INIT_LOCK = threading.Lock()
_LLM_REQUEST_COUNTER = 0  # tie-break: 同優先級按到達順序
_LLM_COUNTER_LOCK = threading.Lock()


class _LLMPriorityRequest:
    """Priority queue 中的一個 LLM 請求."""
    __slots__ = ("priority", "seq", "fn", "future")

    def __init__(self, priority: int, seq: int, fn):
        self.priority = priority
        self.seq = seq
        self.fn = fn
        self.future: _cf.Future = _cf.Future()

    def __lt__(self, other: "_LLMPriorityRequest") -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.seq < other.seq  # 同優先級: 先到先得


def _priority_worker() -> None:
    """Priority queue worker — 從 queue 取請求並執行."""
    while True:
        try:
            req: _LLMPriorityRequest = _LLM_PRIORITY_QUEUE.get(block=True, timeout=1.0)
        except _queue.Empty:
            continue
        try:
            result = req.fn()
            req.future.set_result(result)
        except Exception as exc:
            req.future.set_exception(exc)
        finally:
            _LLM_PRIORITY_QUEUE.task_done()


def _ensure_priority_workers() -> None:
    """懶初始化 priority worker threads."""
    global _LLM_PRIORITY_INITIALIZED
    if _LLM_PRIORITY_INITIALIZED:
        return
    with _LLM_PRIORITY_INIT_LOCK:
        if _LLM_PRIORITY_INITIALIZED:
            return
        for _ in range(_LLM_PRIORITY_WORKER_COUNT):
            t = threading.Thread(target=_priority_worker, daemon=True)
            t.start()
        _LLM_PRIORITY_INITIALIZED = True


def _submit_priority_request(priority: int, fn) -> concurrent.futures.Future:
    """提交 LLM 請求到 priority queue，回傳 Future."""
    global _LLM_REQUEST_COUNTER
    _ensure_priority_workers()
    with _LLM_COUNTER_LOCK:
        seq = _LLM_REQUEST_COUNTER
        _LLM_REQUEST_COUNTER += 1
    req = _LLMPriorityRequest(priority, seq, fn)
    _LLM_PRIORITY_QUEUE.put(req)
    return req.future


# V3-O.10 #6: viewer drop policy — 同 user_id 5s 內重發丟舊
_VIEWER_PENDING: dict[str, float] = {}
_VIEWER_PENDING_LOCK = threading.Lock()
_VIEWER_DROP_COOLDOWN_S = 5.0


def _evict_llama_cache_until(max_cached_models: int) -> None:
    limit = max(1, int(max_cached_models))
    while len(_LLAMA_CACHE) >= limit:
        old_key = next(iter(_LLAMA_CACHE.keys()))
        old_model = _LLAMA_CACHE.pop(old_key, None)
        if old_model is not None:
            close_fn = getattr(old_model, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001
                    pass
    gc.collect()


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout_s: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = url_request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)

    try:
        with url_request.urlopen(req, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
    except url_error.HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            details = exc.reason if hasattr(exc, "reason") else str(exc)
        raise LLMClientError(f"HTTP {exc.code} {url}: {details}") from exc
    except url_error.URLError as exc:
        raise LLMClientError(f"Network error {url}: {exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"Invalid JSON from {url}: {raw[:400]}") from exc
    if not isinstance(payload, dict):
        raise LLMClientError(f"Unexpected payload type from {url}: {type(payload).__name__}")
    return payload


def _message_list(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in messages:
        role = str(item.get("role", "")).strip() or "user"
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def _as_int(raw: Any, *, default: int, min_value: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if min_value is not None and value < min_value:
        value = min_value
    return value


def _as_float(raw: Any, *, default: float, min_value: float | None = None) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    if min_value is not None and value < min_value:
        value = min_value
    return value


def _as_bool(raw: Any, *, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if raw is None:
        return default
    return bool(raw)


def _strip_think_block(content: str) -> str:
    text = content.strip()
    if not text:
        return text
    stripped = _THINK_BLOCK_RE.sub("", text).strip()
    return stripped or text


def _looks_like_gguf_path(raw: str) -> bool:
    text = raw.strip().lower()
    if not text:
        return False
    if text.endswith(".gguf"):
        return True
    return "/" in text or "\\" in text


class LLMClient:
    """Route LLM calls with global default + persona override + fallback chain."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve()

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        persona_id: str,
        override_profile: str | None = None,
        override_model: str | None = None,
        temperature: float = 0.2,
        timeout_s: float = 90.0,
        auxiliary: str | None = None,
        priority: str = "viewer",  # V3-O.10 #5: "owner" | "vip" | "viewer"
    ) -> LLMGenerateResult:
        """Generate one assistant response from routed provider chain.

        R21.x C117: 加 auxiliary kwarg, 對應 R21 C111 auxiliary.* LLM 分工.
        子任務 (umbrella/curator/triage/etc) 傳 auxiliary="<task_name>" → 走
        llm_router.yaml auxiliary_overrides[task_name] (priority 高過 persona, 低過 override).
        backward compat: 不傳 auxiliary → 完全跟既有行為一致.
        """
        """Generate one assistant response from routed provider chain."""
        serialize_text = os.getenv("AGENT_MEMORY_SERIALIZE_LLM", "1").strip().lower()
        serialize = serialize_text not in {"0", "false", "no", "off"}
        acquired = True
        # V3-O.10 #21: lock_timeout 從 companion_config.yaml llm.concurrency 讀 (env 仍可 override)
        env_lock_raw = os.getenv("AGENT_MEMORY_LLM_LOCK_TIMEOUT_S", "").strip()
        try:
            base_lock_timeout = float(env_lock_raw) if env_lock_raw else 120.0
        except ValueError:
            base_lock_timeout = 120.0
        # 嘗試從 yaml 讀 concurrency.lock_timeout_s (env 優先)
        if not env_lock_raw:
            try:
                import yaml as _yaml
                _ccfg_path = self.vault_root / "00_System_Core" / "companion_config.yaml"
                if _ccfg_path.exists():
                    _ccfg = _yaml.safe_load(_ccfg_path.read_text(encoding="utf-8")) or {}
                    _yaml_lock = float(_ccfg.get("llm", {}).get("concurrency", {}).get("lock_timeout_s", 120.0))
                    base_lock_timeout = _yaml_lock
            except Exception:
                pass
        lock_timeout = max(5.0, float(timeout_s) + 5.0, base_lock_timeout)

        # V3-O.10 #5: 主對話走 priority queue (owner > VIP > viewer)
        # 子任務走獨立 lock pool (解 B2)
        if serialize and not auxiliary:
            # 主對話: 提交到 priority queue，等待 worker 執行
            prio_level = _PRIORITY_LEVELS.get(priority, _PRIORITY_LEVELS["viewer"])
            _inner_self = self
            _inner_msgs = list(messages)
            _inner_kwargs = dict(
                persona_id=persona_id,
                override_profile=override_profile,
                override_model=override_model,
                temperature=temperature,
                timeout_s=timeout_s,
                auxiliary=auxiliary,
                priority=priority,
            )

            def _execute_in_worker() -> LLMGenerateResult:
                return _inner_self._generate_core(
                    messages=_inner_msgs,
                    persona_id=persona_id,
                    override_profile=override_profile,
                    override_model=override_model,
                    temperature=temperature,
                    timeout_s=timeout_s,
                    auxiliary=None,
                )

            future = _submit_priority_request(prio_level, _execute_in_worker)
            try:
                return future.result(timeout=lock_timeout)
            except _cf.TimeoutError:
                raise LLMClientError(f"LLM priority queue timeout ({lock_timeout:.1f}s, priority={priority})")
            except Exception as exc:
                raise

        # 子任務: 走獨立 lock pool (直接執行, 不過 priority queue)
        active_lock = _LLM_SUB_TASK_LOCK
        acquired = True
        if serialize:
            acquired = active_lock.acquire(timeout=lock_timeout)
            if not acquired:
                raise LLMClientError(f"LLM sub_task lock timeout ({lock_timeout:.1f}s)")
        try:
            return self._generate_core(
                messages=messages,
                persona_id=persona_id,
                override_profile=override_profile,
                override_model=override_model,
                temperature=temperature,
                timeout_s=timeout_s,
                auxiliary=auxiliary,
            )
        finally:
            if serialize and acquired:
                active_lock.release()

    def _generate_core(
        self,
        *,
        messages: list[dict[str, str]],
        persona_id: str,
        override_profile: str | None = None,
        override_model: str | None = None,
        temperature: float = 0.2,
        timeout_s: float = 90.0,
        auxiliary: str | None = None,
    ) -> LLMGenerateResult:
        """核心 LLM generate 邏輯 (無 lock, 由呼叫方管理)."""
        clean_messages = _message_list(messages)
        if not clean_messages:
            raise LLMClientError("messages 不可為空")

        cfg = load_llm_router_config(self.vault_root)
        resolved = resolve_llm_route(
            cfg,
            persona_id=persona_id,
            override_profile=override_profile,
            override_model=override_model,
            auxiliary=auxiliary,  # R21.x C117: 子任務 LLM 分工 propagate
        )
        providers = cfg.get("providers", {})
        if not isinstance(providers, dict):
            providers = {}

        failures: list[LLMAttemptFailure] = []
        for candidate in resolved["chain"]:
            profile = str(candidate.get("profile", "")).strip()
            model = str(candidate.get("model", "")).strip()
            provider_cfg = providers.get(profile, {})
            if not isinstance(provider_cfg, dict):
                provider_cfg = {}
            kind = str(provider_cfg.get("kind", "openai_compatible")).strip()
            base_url = str(provider_cfg.get("base_url", "")).strip()
            requires_key = bool(provider_cfg.get("requires_api_key", True))
            key_env = str(provider_cfg.get("api_key_env", "")).strip()
            api_key = os.getenv(key_env, "").strip() if key_env else ""
            if requires_key and not api_key:
                failures.append(LLMAttemptFailure(profile=profile, model=model, reason=f"missing {key_env}"))
                continue
            if not base_url:
                failures.append(LLMAttemptFailure(profile=profile, model=model, reason="missing base_url"))
                continue

            # R15 C65 (Codex 第 16 焦點 T3.2/T12.3 Gemini 500):
            # 對同一 provider 試一次 retry, 只在 transient 5xx (HTTP 5..) 或
            # "Internal error" 字串時. 一般 4xx / network / json 錯誤直接 fallback
            # 走 chain 下一個 provider, 不再 retry 同一個.
            content: str | None = None
            last_exc_msg: str = ""
            for attempt_idx in range(2):
                try:
                    content = self._dispatch_generate(
                        kind=kind,
                        base_url=base_url,
                        model=model,
                        api_key=api_key,
                        provider_cfg=provider_cfg,
                        messages=clean_messages,
                        temperature=temperature,
                        timeout_s=timeout_s,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    last_exc_msg = msg
                    # 5xx (HTTP 5\d\d) 或 Gemini "Internal error" — transient, retry 同 provider 一次
                    is_transient = bool(
                        re.search(r"HTTP\s+5\d\d", msg)
                        or "Internal error" in msg
                        or "internal_error" in msg
                    )
                    if is_transient and attempt_idx == 0:
                        time.sleep(1.0)
                        continue
                    break

            if content is None:
                failures.append(LLMAttemptFailure(profile=profile, model=model, reason=last_exc_msg or "unknown error"))
                continue

            if not content.strip():
                failures.append(LLMAttemptFailure(profile=profile, model=model, reason="empty response"))
                continue

            return LLMGenerateResult(
                content=content.strip(),
                profile=profile,
                model=model,
                provider_kind=kind,
                base_url=base_url,
                attempts=failures,
            )

        reason_lines = [f"- {f.profile}/{f.model}: {f.reason}" for f in failures]
        raise LLMClientError("所有 LLM 嘗試失敗：\n" + "\n".join(reason_lines))

    def _dispatch_generate(
        self,
        *,
        kind: str,
        base_url: str,
        model: str,
        api_key: str,
        provider_cfg: dict[str, Any],
        messages: list[dict[str, str]],
        temperature: float,
        timeout_s: float,
    ) -> str:
        kind_norm = kind.lower().strip()
        if kind_norm == "ollama":
            return self._call_ollama(
                base_url=base_url,
                model=model,
                messages=messages,
                temperature=temperature,
                timeout_s=timeout_s,
            )
        if kind_norm in {"llama_cpp_python", "llama_cpp_local"}:
            return self._call_llama_cpp_python(
                provider_cfg=provider_cfg,
                model=model,
                messages=messages,
                temperature=temperature,
            )
        if kind_norm == "anthropic":
            return self._call_anthropic(
                base_url=base_url,
                model=model,
                api_key=api_key,
                messages=messages,
                temperature=temperature,
                timeout_s=timeout_s,
            )
        return self._call_openai_compatible(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=messages,
            temperature=temperature,
            timeout_s=timeout_s,
        )

    def _call_ollama(
        self,
        *,
        base_url: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        timeout_s: float,
    ) -> str:
        url = base_url.rstrip("/") + "/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        data = _post_json(url, payload, headers={}, timeout_s=timeout_s)
        msg = data.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
        raise LLMClientError(f"Ollama response missing message.content: {data}")

    def _call_openai_compatible(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        messages: list[dict[str, str]],
        temperature: float,
        timeout_s: float,
        on_token=None,  # V3-O.10 #26: optional streaming callback (token: str) -> None
    ) -> str:
        url = base_url.rstrip("/") + "/chat/completions"
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # V3-O.10 #26: streaming mode (on_token callback 存在時啟用)
        if on_token is not None:
            return self._call_openai_streaming(
                url=url, model=model, messages=messages,
                temperature=temperature, timeout_s=timeout_s,
                headers=headers, on_token=on_token,
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        data = _post_json(url, payload, headers=headers, timeout_s=timeout_s)
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise LLMClientError(f"OpenAI-compatible response missing choices: {data}")
        msg = choices[0].get("message", {})
        if not isinstance(msg, dict):
            raise LLMClientError(f"OpenAI-compatible response missing message: {data}")
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "\n".join(parts).strip()
        raise LLMClientError(f"Unsupported content format: {content}")

    def _call_openai_streaming(
        self,
        *,
        url: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        timeout_s: float,
        headers: dict[str, str],
        on_token,
    ) -> str:
        """V3-O.10 #26: SSE streaming — 逐 token 呼叫 on_token(chunk), 回傳完整文字."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        body = json.dumps(payload).encode("utf-8")
        req = url_request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        for k, v in headers.items():
            req.add_header(k, v)

        full_text = []
        try:
            with url_request.urlopen(req, timeout=timeout_s) as resp:
                for line in resp:
                    if isinstance(line, bytes):
                        line = line.decode("utf-8")
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        try:
                            chunk_data = json.loads(line[6:])
                            delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                full_text.append(token)
                                try:
                                    on_token(token)
                                except Exception:
                                    pass
                        except Exception:
                            pass
        except url_error.HTTPError as exc:
            raise LLMClientError(f"HTTP {exc.code} streaming {url}") from exc
        except url_error.URLError as exc:
            raise LLMClientError(f"Network error streaming {url}: {exc.reason}") from exc

        return "".join(full_text)

    def _call_anthropic(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        messages: list[dict[str, str]],
        temperature: float,
        timeout_s: float,
    ) -> str:
        if not api_key:
            raise LLMClientError("anthropic requires API key")

        system_parts: list[str] = []
        user_assistant_messages: list[dict[str, str]] = []
        for item in messages:
            if item["role"] == "system":
                system_parts.append(item["content"])
            else:
                role = item["role"] if item["role"] in {"user", "assistant"} else "user"
                user_assistant_messages.append({"role": role, "content": item["content"]})
        if not user_assistant_messages:
            user_assistant_messages = [{"role": "user", "content": "請回覆。"}]

        url = base_url.rstrip("/") + "/messages"
        payload: dict[str, Any] = {
            "model": model,
            "messages": user_assistant_messages,
            "max_tokens": 1024,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        data = _post_json(url, payload, headers=headers, timeout_s=timeout_s)
        content = data.get("content", [])
        if not isinstance(content, list):
            raise LLMClientError(f"Anthropic response missing content list: {data}")
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text", "")))
        text = "\n".join(texts).strip()
        if not text:
            raise LLMClientError(f"Anthropic text content empty: {data}")
        return text

    def _call_llama_cpp_python(
        self,
        *,
        provider_cfg: dict[str, Any],
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
    ) -> str:
        self._apply_path_prepend(provider_cfg)

        try:
            from llama_cpp import Llama
        except Exception as exc:  # noqa: BLE001
            raise LLMClientError(
                "provider kind=llama_cpp_python 需要安裝 llama-cpp-python "
                "（可先用 pip 安裝 CPU/CUDA wheel）"
            ) from exc

        model_path = self._resolve_llama_model_path(provider_cfg, model)
        n_ctx = _as_int(provider_cfg.get("n_ctx"), default=4096, min_value=256)
        n_gpu_layers = _as_int(provider_cfg.get("n_gpu_layers"), default=999, min_value=-1)
        n_batch = _as_int(provider_cfg.get("n_batch"), default=512, min_value=16)
        cpu_count = os.cpu_count() or 8
        n_threads = _as_int(provider_cfg.get("n_threads"), default=max(1, cpu_count // 2), min_value=1)
        flash_attn = _as_bool(provider_cfg.get("flash_attn"), default=True)
        chat_format = str(provider_cfg.get("chat_format", "")).strip()
        max_tokens = _as_int(provider_cfg.get("max_tokens"), default=1024, min_value=1)
        top_p = _as_float(provider_cfg.get("top_p"), default=0.95, min_value=0.0)
        top_k = _as_int(provider_cfg.get("top_k"), default=40, min_value=0)
        min_p = _as_float(provider_cfg.get("min_p"), default=0.05, min_value=0.0)
        repeat_penalty = _as_float(provider_cfg.get("repeat_penalty"), default=1.0, min_value=0.0)
        strip_think_tags = _as_bool(provider_cfg.get("strip_think_tags"), default=True)
        max_cached_models = _as_int(provider_cfg.get("max_cached_models"), default=1, min_value=1)

        llm = self._get_or_create_llama(
            Llama=Llama,
            model_path=model_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            n_batch=n_batch,
            n_threads=n_threads,
            flash_attn=flash_attn,
            chat_format=chat_format,
            max_cached_models=max_cached_models,
        )

        response = llm.create_chat_completion(
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            repeat_penalty=repeat_penalty,
            max_tokens=max_tokens,
            stream=False,
        )
        choices = response.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise LLMClientError(f"llama_cpp_python response missing choices: {response}")

        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            raise LLMClientError(f"llama_cpp_python response missing message: {response}")

        content = message.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            content = "\n".join(parts)
        if not isinstance(content, str):
            raise LLMClientError(f"llama_cpp_python content format unsupported: {type(content).__name__}")

        cleaned = _strip_think_block(content) if strip_think_tags else content.strip()
        return cleaned

    def _resolve_llama_model_path(self, provider_cfg: dict[str, Any], model: str) -> Path:
        model_ref = model.strip()
        configured = str(provider_cfg.get("model_path", "")).strip()
        raw = configured or model_ref
        if model_ref:
            if "/" in model_ref or "\\" in model_ref:
                raw = model_ref
            elif _looks_like_gguf_path(model_ref) and configured:
                configured_path = Path(configured)
                if configured_path.suffix.lower() == ".gguf":
                    raw = str(configured_path.with_name(model_ref))
                else:
                    raw = str(configured_path / model_ref)
            elif not configured:
                raw = model_ref
        if not raw:
            raise LLMClientError("llama_cpp_python 缺少 model_path（可填 providers.<id>.model_path）")

        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not resolved.exists():
                raise LLMClientError(f"GGUF 檔不存在：{resolved}")
            return resolved

        search_roots = [
            self.vault_root,
            self.vault_root.parent,
            self.vault_root.parent.parent,
            self.vault_root.parent.parent / "0_Models",
            Path.cwd(),
            Path.cwd().parent,
            Path.cwd().parent / "0_Models",
        ]
        for root in search_roots:
            resolved = (root / candidate).resolve()
            if resolved.exists():
                return resolved

        raise LLMClientError(
            "找不到 GGUF 檔："
            f"{raw}（已嘗試相對於 vault_root / vault_root.parent / vault_root.parent.parent / cwd / 0_Models）"
        )

    def _apply_path_prepend(self, provider_cfg: dict[str, Any]) -> None:
        raw = provider_cfg.get("path_prepend")
        if raw is None:
            return
        values: list[str] = []
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list):
            values = [str(item) for item in raw if str(item).strip()]
        if not values:
            return

        current = os.environ.get("PATH", "")
        parts = current.split(os.pathsep) if current else []
        prepend: list[str] = []
        seen = {p.lower() for p in parts}
        for item in values:
            path_text = item.strip()
            if not path_text:
                continue
            expanded = str(Path(path_text).expanduser())
            key = expanded.lower()
            if key in seen:
                continue
            prepend.append(expanded)
            seen.add(key)
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend + parts)

    def _get_or_create_llama(
        self,
        *,
        Llama: Any,
        model_path: Path,
        n_ctx: int,
        n_gpu_layers: int,
        n_batch: int,
        n_threads: int,
        flash_attn: bool,
        chat_format: str,
        max_cached_models: int,
    ) -> Any:
        key = (
            str(model_path),
            n_ctx,
            n_gpu_layers,
            n_batch,
            n_threads,
            flash_attn,
            chat_format,
        )
        cached = _LLAMA_CACHE.get(key)
        if cached is not None:
            return cached

        with _LLAMA_CACHE_LOCK:
            cached = _LLAMA_CACHE.get(key)
            if cached is not None:
                return cached
            _evict_llama_cache_until(max_cached_models)
            kwargs: dict[str, Any] = {
                "model_path": str(model_path),
                "n_ctx": n_ctx,
                "n_gpu_layers": n_gpu_layers,
                "n_batch": n_batch,
                "n_threads": n_threads,
                "flash_attn": flash_attn,
                "verbose": False,
            }
            if chat_format:
                kwargs["chat_format"] = chat_format
            instance = Llama(**kwargs)
            _LLAMA_CACHE[key] = instance
            return instance
