"""Embedding client for provider-backed retrieval vectors."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

from agent_memory.llm_routing import load_llm_router_config


class EmbeddingClientError(RuntimeError):
    """Raised when provider embedding requests fail."""


def _normalize_vector(raw: list[Any]) -> list[float]:
    vec = [float(x) for x in raw]
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


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
        try:
            detail = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            detail = str(exc)
        raise EmbeddingClientError(f"HTTP {exc.code} {url}: {detail}") from exc
    except url_error.URLError as exc:
        raise EmbeddingClientError(f"Network error {url}: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EmbeddingClientError(f"Invalid JSON from {url}: {raw[:300]}") from exc
    if not isinstance(parsed, dict):
        raise EmbeddingClientError(f"Unexpected embedding payload type: {type(parsed).__name__}")
    return parsed


class EmbeddingClient:
    """Resolve providers from llm_router and request embeddings."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve()
        cfg = load_llm_router_config(self.vault_root)
        providers = cfg.get("providers", {})
        self.providers = providers if isinstance(providers, dict) else {}

    def embed_texts(
        self,
        *,
        texts: list[str],
        profile: str,
        model: str,
        timeout_s: float = 20.0,
    ) -> list[list[float]]:
        clean = [str(text).strip() for text in texts if str(text).strip()]
        if not clean:
            return []
        provider_cfg = self.providers.get(profile, {})
        if not isinstance(provider_cfg, dict):
            raise EmbeddingClientError(f"unknown embedding profile: {profile}")

        kind = str(provider_cfg.get("kind", "")).strip().lower()
        base_url = str(provider_cfg.get("base_url", "")).strip()
        requires_key = bool(provider_cfg.get("requires_api_key", True))
        key_env = str(provider_cfg.get("api_key_env", "")).strip()
        api_key = os.getenv(key_env, "").strip() if key_env else ""
        if requires_key and not api_key:
            raise EmbeddingClientError(f"missing embedding api key env: {key_env}")
        if not base_url:
            raise EmbeddingClientError("embedding provider missing base_url")

        if kind == "openai_compatible":
            return self._embed_openai_compatible(
                base_url=base_url,
                api_key=api_key,
                model=model,
                texts=clean,
                timeout_s=timeout_s,
            )
        if kind == "ollama":
            return self._embed_ollama(
                base_url=base_url,
                model=model,
                texts=clean,
                timeout_s=timeout_s,
            )
        raise EmbeddingClientError(f"embedding kind not supported: {kind}")

    def _embed_openai_compatible(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        texts: list[str],
        timeout_s: float,
    ) -> list[list[float]]:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": model, "input": texts}
        data = _post_json(base_url.rstrip("/") + "/embeddings", payload, headers=headers, timeout_s=timeout_s)
        rows = data.get("data", [])
        if not isinstance(rows, list) or not rows:
            raise EmbeddingClientError("openai-compatible embedding response missing data")
        vectors: list[list[float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_embedding = row.get("embedding", [])
            if not isinstance(raw_embedding, list):
                continue
            vectors.append(_normalize_vector(raw_embedding))
        if len(vectors) != len(texts):
            raise EmbeddingClientError("embedding vector count mismatch")
        return vectors

    def _embed_ollama(
        self,
        *,
        base_url: str,
        model: str,
        texts: list[str],
        timeout_s: float,
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            payload = {"model": model, "prompt": text}
            data = _post_json(base_url.rstrip("/") + "/api/embeddings", payload, headers={}, timeout_s=timeout_s)
            raw_embedding = data.get("embedding", [])
            if not isinstance(raw_embedding, list) or not raw_embedding:
                raise EmbeddingClientError("ollama embedding response missing embedding vector")
            vectors.append(_normalize_vector(raw_embedding))
        return vectors
