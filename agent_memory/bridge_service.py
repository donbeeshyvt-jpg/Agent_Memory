"""hermes ↔ Agent_Memory 雙向 bridge service (R22 stage 1, stdlib http.server only, 零第三方 web framework 依賴).

對齊使用者 D47 Flow W sandwich-inverted 流程:
    [Users] → [Agent_Memory 入口 ingest + 自記憶]
            → [hermes 加工 + hermes 自記憶]
            → [Agent_Memory 出口過濾 + 統一 reply] → [Users]

stage 1 = 基礎建設: HTTP server + 3 endpoint signature + auth + stdlib only
stage 2 = 真接 hermes 端 + Flow W 全流程 e2e + outbox queue (留 R22 後續)

Endpoints:
    GET  /health        public; 回 service ok + version
    POST /retrieve      需 X-Bridge-Secret; 包 MemorySearchManager.search
    POST /chat          需 X-Bridge-Secret; 包 transport_ingest.run_transport_event
                        + hermes_augmentation passthrough (stage 1 不過濾)

Auth:
    BRIDGE_SECRET env  → POST 必須帶 X-Bridge-Secret header 對應
    BRIDGE_AUTH_DISABLED=1 → dev 開放 (預設 false; 沒設 BRIDGE_SECRET 預設 deny POST)

Port: --port / BRIDGE_PORT env, default 16001
跟 transport_bridge_server.py (port 16000, Discord/LINE webhook) 區分.
"""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_memory.runtime import MemoryRuntime
from agent_memory.transport_ingest import run_transport_event
from agent_memory.vault import ObsidianVaultAdapter


SERVICE_NAME = "agent-memory-hermes-bridge"
HEALTH_VERSION = "r22-stage1"
DEFAULT_PORT = 16001
SECRET_HEADER = "X-Bridge-Secret"
HERMES_TRANSPORT = "hermes-bridge"


def _read_secret_env() -> str:
    return (os.environ.get("BRIDGE_SECRET") or "").strip()


def _auth_disabled_env() -> bool:
    return (os.environ.get("BRIDGE_AUTH_DISABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class _HermesBridgeServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        vault_root: Path,
        *,
        expected_secret: str,
        auth_disabled: bool,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.expected_secret = expected_secret
        self.auth_disabled = auth_disabled
        super().__init__(server_address, _HermesBridgeHandler)


class _HermesBridgeHandler(BaseHTTPRequestHandler):
    server: _HermesBridgeServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": SERVICE_NAME,
                    "version": HEALTH_VERSION,
                    "auth_disabled": self.server.auth_disabled,
                    "endpoints": {
                        "health": "GET /health",
                        "retrieve": "POST /retrieve",
                        "chat": "POST /chat",
                    },
                },
            )
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._check_auth():
            return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        try:
            body = self._read_json()
            if parsed.path == "/retrieve":
                return self._send_json(HTTPStatus.OK, self._handle_retrieve(body))
            if parsed.path == "/chat":
                return self._send_json(HTTPStatus.OK, self._handle_chat(body))
        except ValueError as exc:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            return self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error", "type": type(exc).__name__},
            )
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _check_auth(self) -> bool:
        if self.server.auth_disabled:
            return True
        expected = self.server.expected_secret
        if not expected:
            return False  # 預設 deny: 沒設 BRIDGE_SECRET 不可 POST
        provided = self.headers.get(SECRET_HEADER, "").strip()
        return bool(provided) and provided == expected

    def _handle_retrieve(self, body: dict[str, Any]) -> dict[str, Any]:
        query = str(body.get("query", "")).strip()
        if not query:
            raise ValueError("query 為空")
        try:
            max_results = int(body.get("max_results", 5))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_results 必須為整數") from exc
        if max_results <= 0:
            raise ValueError("max_results 必須 > 0")
        if max_results > 50:
            max_results = 50  # cap, 避免 hermes 端打爆

        adapter = ObsidianVaultAdapter(self.server.vault_root)
        runtime = MemoryRuntime(adapter)
        hits = runtime.search_manager.search(query=query, max_results=max_results)
        return {
            "ok": True,
            "query": query,
            "max_results": max_results,
            "hits": [
                {
                    "path": getattr(h, "path", ""),
                    "score": float(getattr(h, "score", 0.0)),
                    "snippet": (getattr(h, "snippet", "") or "")[:500],
                    "source": getattr(h, "source", ""),
                }
                for h in hits
            ],
        }

    def _handle_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        persona = str(body.get("persona", "")).strip() or "core"
        message = str(body.get("user_message") or body.get("message") or "").strip()
        if not message:
            raise ValueError("user_message 為空")

        # stage 1 passthrough: hermes_augmentation 直接附在 prompt 後
        # stage 2 才加品質檢查 / 出口過濾
        hermes_augmentation = str(body.get("hermes_augmentation", "")).strip()
        if hermes_augmentation:
            message = f"{message}\n\n[hermes_augmentation]\n{hermes_augmentation}"

        transport = str(body.get("transport", "")).strip() or HERMES_TRANSPORT
        channel_id = str(body.get("channel_id", "")).strip() or "hermes-default"
        user_id = str(body.get("user_id", "")).strip() or "hermes-user"
        payload = {
            "message": message,
            "channel_id": channel_id,
            "user_id": user_id,
        }
        result = run_transport_event(
            vault_root=self.server.vault_root,
            transport=transport,
            payload=payload,
            explicit_persona=persona,
            context_override=str(body.get("context", "")).strip() or None,
            session_override=str(body.get("session", "")).strip() or None,
        )
        # R22.1 C123: transport_ingest.run_transport_event 把 session/daily/shared_channel 全收在
        # nested dict `memory_paths` (line 488/593/747), 不是 flat `memory_session_path`.
        # Codex 第 43 輪 Phase 4 抓到 mapping miss (session_path 一直空字串). 修讀法.
        memory_paths_raw = result.get("memory_paths") or {}
        memory_paths = memory_paths_raw if isinstance(memory_paths_raw, dict) else {}
        return {
            "ok": True,
            "persona": persona,
            "reply": str(result.get("response", "")),
            "session_path": str(memory_paths.get("session") or ""),
            "daily_path": str(memory_paths.get("daily") or ""),
            "shared_channel_path": str(memory_paths.get("shared_channel") or ""),
            "hermes_augmentation_applied": bool(hermes_augmentation),
        }

    def _read_json(self) -> dict[str, Any]:
        length_raw = self.headers.get("Content-Length", "0").strip()
        length = int(length_raw) if length_raw.isdigit() else 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON parse error: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body 必須為 object")
        return payload

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def serve_hermes_bridge(
    vault_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    expected_secret: str | None = None,
    auth_disabled: bool = False,
) -> None:
    secret = expected_secret if expected_secret is not None else _read_secret_env()
    server = _HermesBridgeServer(
        (host, int(port)),
        Path(vault_root),
        expected_secret=secret,
        auth_disabled=auth_disabled or _auth_disabled_env(),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent_Memory hermes bridge service (R22 stage 1)"
    )
    parser.add_argument("--vault-root", required=True, help="vault root path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BRIDGE_PORT", DEFAULT_PORT)),
    )
    parser.add_argument(
        "--auth-disabled",
        action="store_true",
        help="dev only; disable BRIDGE_SECRET auth check",
    )
    args = parser.parse_args(argv)
    serve_hermes_bridge(
        Path(args.vault_root),
        host=args.host,
        port=args.port,
        auth_disabled=args.auth_disabled,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
