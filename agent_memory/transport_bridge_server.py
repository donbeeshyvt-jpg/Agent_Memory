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
                # V3-O.10 #26: /webhook/<transport>/stream → SSE chunked streaming
                if path.endswith("/stream"):
                    transport = transport.removesuffix("/stream").rstrip("/")
                    return self._handle_webhook_streaming(transport, body)
                return self._send_json(HTTPStatus.OK, self._handle_webhook(transport, body))
        except Exception as exc:  # noqa: BLE001
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_webhook_streaming(self, transport: str, body: dict[str, Any]) -> None:
        """V3-O.10 #26: SSE chunked streaming response.

        Client 收到 data: {"token": "..."} 逐 chunk，最後收到 data: {"done": true, "response": "..."}
        """
        import threading as _th
        import queue as _q
        token_queue: _q.Queue = _q.Queue()
        full_parts: list[str] = []
        error_holder: list[str] = []

        def _on_token(tok: str) -> None:
            full_parts.append(tok)
            token_queue.put(("token", tok))

        def _run() -> None:
            try:
                result = run_transport_event(
                    vault_root=self.server.vault_root,
                    transport=transport,
                    payload={**body, "_on_token": _on_token},
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
                token_queue.put(("done", result))
            except Exception as exc:
                error_holder.append(str(exc))
                token_queue.put(("error", str(exc)))

        worker = _th.Thread(target=_run, daemon=True)
        worker.start()

        # SSE headers
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        timeout_s = float(body.get("timeout", 90.0)) + 15.0
        start = __import__("time").monotonic()
        try:
            while True:
                if __import__("time").monotonic() - start > timeout_s:
                    break
                try:
                    kind, data = token_queue.get(timeout=1.0)
                except _q.Empty:
                    continue
                if kind == "token":
                    line = json.dumps({"token": data}, ensure_ascii=False)
                elif kind == "done":
                    resp = data if isinstance(data, dict) else {"response": str(data)}
                    line = json.dumps({"done": True, **resp}, ensure_ascii=False)
                    self._write_sse(f"data: {line}\n\n")
                    break
                elif kind == "error":
                    line = json.dumps({"error": data}, ensure_ascii=False)
                    self._write_sse(f"data: {line}\n\n")
                    break
                else:
                    continue
                self._write_sse(f"data: {line}\n\n")
        except Exception:
            pass

    def _write_sse(self, text: str) -> None:
        try:
            chunk = text.encode("utf-8")
            self.wfile.write(f"{len(chunk):x}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def _handle_webhook(self, transport: str, body: dict[str, Any]) -> dict[str, Any]:
        import concurrent.futures as _cf
        timeout_s = float(body.get("timeout", 90.0))
        # V3-O.10 #22: bridge timeout abort — 超時自動 abort 防殭屍 LLM call
        abort_timeout = timeout_s + 10.0  # 給 LLM 多 10s 緩衝, 超過直接取消
        with _cf.ThreadPoolExecutor(max_workers=1) as _executor:
            _future = _executor.submit(
                run_transport_event,
                vault_root=self.server.vault_root,
                transport=transport,
                payload=body,
                explicit_persona=str(body.get("persona", "")).strip() or None,
                context_override=str(body.get("context", "")).strip() or None,
                session_override=str(body.get("session", "")).strip() or None,
                override_profile=str(body.get("override_profile", "")).strip() or None,
                override_model=str(body.get("override_model", "")).strip() or None,
                temperature=float(body.get("temperature", 0.2)),
                timeout_s=timeout_s,
                memory_mode=str(body.get("memory_mode", "session_and_daily")).strip() or "session_and_daily",
                dialogue_mode=str(body.get("dialogue_mode", body.get("mode", ""))).strip() or None,
                allow_llm_degraded=_coerce_bool(body.get("allow_llm_degraded"), True),
            )
            try:
                return _future.result(timeout=abort_timeout)
            except _cf.TimeoutError:
                _future.cancel()
                return {"error": f"bridge_timeout ({abort_timeout:.0f}s)", "response": ""}

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
