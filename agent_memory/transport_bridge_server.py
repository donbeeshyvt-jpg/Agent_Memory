"""極簡 Transport Webhook HTTP 服務（Discord / LINE → run_transport_event）。

適合在本機或內網以固定 URL 接住轉發的 JSON（例如 Discord bot、LINE webhook 反向代理）。
與 `serve-dashboard` 的 `POST /webhook/{transport}` 語意對齊，僅不提供 HTML 控制台。
"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_memory.chat_session import sanitize_component
from agent_memory.transport_ingest import run_transport_event


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


class _TransportBridgeServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve()
        super().__init__(server_address, _TransportBridgeHandler)


class _TransportBridgeHandler(BaseHTTPRequestHandler):
    server: _TransportBridgeServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health"):
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "agent-memory-transport-bridge",
                    "vault_root": str(self.server.vault_root),
                    "endpoints": {
                        "discord": "POST /webhook/discord",
                        "line": "POST /webhook/line",
                    },
                },
            )
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json()
        try:
            path = parsed.path.strip("/")
            if path.startswith("webhook/"):
                transport = sanitize_component(path.removeprefix("webhook/"), fallback="web").lower()
                return self._send_json(HTTPStatus.OK, self._handle_webhook(transport, body))
        except Exception as exc:  # noqa: BLE001
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_webhook(self, transport: str, body: dict[str, Any]) -> dict[str, Any]:
        return run_transport_event(
            vault_root=self.server.vault_root,
            transport=transport,
            payload=body,
            explicit_persona=str(body.get("persona", "")).strip() or None,
            context_override=str(body.get("context", "")).strip() or None,
            session_override=str(body.get("session", "")).strip() or None,
            override_profile=str(body.get("override_profile", "")).strip() or None,
            override_model=str(body.get("override_model", "")).strip() or None,
            temperature=float(body.get("temperature", 0.2)),
            timeout_s=float(body.get("timeout", 90.0)),
            memory_mode=str(body.get("memory_mode", "session_and_daily")).strip() or "session_and_daily",
            dialogue_mode=str(body.get("dialogue_mode", body.get("mode", ""))).strip() or None,
            allow_llm_degraded=_coerce_bool(body.get("allow_llm_degraded"), True),
        )

    def _read_json(self) -> dict[str, Any]:
        length_raw = self.headers.get("Content-Length", "0").strip()
        length = int(length_raw) if length_raw.isdigit() else 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
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


def serve_transport_bridge(vault_root: Path, *, host: str = "127.0.0.1", port: int = 16000) -> None:
    server = _TransportBridgeServer((host, int(port)), Path(vault_root))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
