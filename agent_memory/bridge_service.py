"""hermes ↔ Agent_Memory 雙向 bridge service (R22 stage 1+2, stdlib http.server only, 零第三方 web framework 依賴).

對齊使用者 D47 Flow W sandwich-inverted 流程:
    [Users] → [Agent_Memory 入口 ingest + 自記憶]
            → [hermes 加工 + hermes 自記憶]
            → [Agent_Memory 出口過濾 + 統一 reply] → [Users]

stage 1 (HEAD e0021dc / 1d6a11f): 基礎建設 HTTP + 3 endpoint + auth + stdlib only
stage 2 (本檔當前版本): + HMAC signing + multi-hermes namespace + outbox + telemetry

Endpoints:
    GET  /health             public; 回 service ok + version + outbox 統計
    POST /retrieve           需 X-Bridge-Secret; 包 MemorySearchManager.search
    POST /chat               需 X-Bridge-Secret; 包 transport_ingest.run_transport_event
                             + hermes_augmentation passthrough (sync) 或進 outbox (async)
    POST /chat?async=true    同上, 但 enqueue + 立刻 ack pending
    GET  /outbox             需 X-Bridge-Secret; 回 outbox status counts + dead-letter
    GET  /outbox/{id}        需 X-Bridge-Secret; 回單筆 row status + result

Auth (R22 stage 1):
    BRIDGE_SECRET env             POST/admin GET 必須帶 X-Bridge-Secret header
    BRIDGE_AUTH_DISABLED=1        dev 開放 (預設 false; 沒設 BRIDGE_SECRET 預設 deny)

Signing (R22 stage 2):
    HERMES_SIGNING_SECRET env      hermes_augmentation 必須帶 X-Hermes-Augmentation-Signature
                                   HMAC-SHA256(secret, augmentation) hex
                                   沒設 env → backward compat 跳過 (warn-only)

Namespace (R22 stage 2):
    X-Hermes-ID header             hermes 端報自己 ID (eg "hermes-default", "hermes-alpha")
                                   影響 outbox row + telemetry + session 命名; 沒帶 → "hermes-default"

Telemetry (R22 stage 2):
    .ai/bridge_events.jsonl        每個 endpoint call append 一行: ts/kind/status/latency/hermes_id

Outbox (R22 stage 2):
    .ai/hermes-bridge-outbox.db    sqlite queue; daemon worker thread 異步處理 + retry 3 次 → dead-letter

Port: --port / BRIDGE_PORT env, default 16001
跟 transport_bridge_server.py (port 16000, Discord/LINE webhook) 區分.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_memory.bridge_outbox import (
    BridgeOutbox,
    BridgeOutboxWorker,
    read_bridge_events,
    write_bridge_event,
)
from agent_memory.runtime import MemoryRuntime
from agent_memory.transport_ingest import run_transport_event
from agent_memory.vault import ObsidianVaultAdapter


SERVICE_NAME = "agent-memory-hermes-bridge"
HEALTH_VERSION = "r22-stage2"
DEFAULT_PORT = 16001
SECRET_HEADER = "X-Bridge-Secret"
SIGNATURE_HEADER = "X-Hermes-Augmentation-Signature"
HERMES_ID_HEADER = "X-Hermes-ID"
DEFAULT_HERMES_ID = "hermes-default"
SIGNING_SECRET_ENV = "HERMES_SIGNING_SECRET"
HERMES_TRANSPORT = "hermes-bridge"


def _read_secret_env() -> str:
    return (os.environ.get("BRIDGE_SECRET") or "").strip()


def _read_signing_secret_env() -> str:
    return (os.environ.get(SIGNING_SECRET_ENV) or "").strip()


def _auth_disabled_env() -> bool:
    return (os.environ.get("BRIDGE_AUTH_DISABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _compute_hmac_sig(secret: str, augmentation: str) -> str:
    """HMAC-SHA256 hex digest. Case: lowercase hex."""
    return hmac.new(
        secret.encode("utf-8"),
        augmentation.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verify_hmac_sig(secret: str, augmentation: str, provided_hex: str) -> bool:
    if not provided_hex:
        return False
    expected = _compute_hmac_sig(secret, augmentation)
    try:
        return hmac.compare_digest(expected, provided_hex.strip().lower())
    except Exception:  # noqa: BLE001
        return False


def _process_outbox_chat(vault_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Outbox worker processor: 跑 transport_ingest.run_transport_event, 回 dict.

    跟 _handle_chat sync mode 對齊 (hermes_augmentation 應已在 enqueue 前套進 message).
    """
    message = str(payload.get("message", "")).strip()
    if not message:
        return {"ok": False, "error": "empty_message"}
    persona = str(payload.get("persona", "") or "core")
    transport = str(payload.get("transport", "") or HERMES_TRANSPORT)
    channel_id = str(payload.get("channel_id", "") or "hermes-default")
    user_id = str(payload.get("user_id", "") or "hermes-user")
    inbound_payload = {
        "message": message,
        "channel_id": channel_id,
        "user_id": user_id,
    }
    try:
        result = run_transport_event(
            vault_root=Path(vault_root),
            transport=transport,
            payload=inbound_payload,
            explicit_persona=persona,
            context_override=str(payload.get("context") or "").strip() or None,
            session_override=str(payload.get("session") or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "processor_exception", "type": type(exc).__name__, "message": str(exc)[:500]}
    memory_paths_raw = result.get("memory_paths") or {}
    memory_paths = memory_paths_raw if isinstance(memory_paths_raw, dict) else {}
    return {
        "ok": True,
        "persona": persona,
        "reply": str(result.get("response", "")),
        "session_path": str(memory_paths.get("session") or ""),
        "daily_path": str(memory_paths.get("daily") or ""),
        "shared_channel_path": str(memory_paths.get("shared_channel") or ""),
        "hermes_augmentation_applied": bool(payload.get("hermes_augmentation_applied", False)),
    }


class _HermesBridgeServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        vault_root: Path,
        *,
        expected_secret: str,
        auth_disabled: bool,
        hermes_signing_secret: str = "",
        start_worker: bool = True,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.expected_secret = expected_secret
        self.auth_disabled = auth_disabled
        self.hermes_signing_secret = hermes_signing_secret
        self.outbox = BridgeOutbox(self.vault_root)
        self.worker: BridgeOutboxWorker | None = None
        if start_worker:
            self.worker = BridgeOutboxWorker(
                self.outbox,
                processor=lambda payload: _process_outbox_chat(self.vault_root, payload),
            )
            self.worker.start()
        super().__init__(server_address, _HermesBridgeHandler)

    def server_close(self) -> None:
        if self.worker is not None:
            try:
                self.worker.stop(timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
        super().server_close()


class _HermesBridgeHandler(BaseHTTPRequestHandler):
    server: _HermesBridgeServer

    # ─── telemetry wrappers ───

    def do_GET(self) -> None:  # noqa: N802
        self._tx_t0 = time.perf_counter()
        self._tx_status = 500
        self._tx_kind = "unknown"
        self._tx_extra: dict[str, Any] = {}
        try:
            self._dispatch_get()
        finally:
            self._emit_telemetry()

    def do_POST(self) -> None:  # noqa: N802
        self._tx_t0 = time.perf_counter()
        self._tx_status = 500
        self._tx_kind = "unknown"
        self._tx_extra = {}
        try:
            self._dispatch_post()
        finally:
            self._emit_telemetry()

    def _emit_telemetry(self) -> None:
        try:
            latency_ms = (time.perf_counter() - getattr(self, "_tx_t0", time.perf_counter())) * 1000.0
            write_bridge_event(
                self.server.vault_root,
                kind=getattr(self, "_tx_kind", "unknown"),
                status_code=int(getattr(self, "_tx_status", 0)),
                latency_ms=latency_ms,
                hermes_id=self._hermes_id(),
                extra=getattr(self, "_tx_extra", {}) or {},
            )
        except Exception:  # noqa: BLE001
            return

    # ─── dispatch ───

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._tx_kind = "health"
            counts = {}
            try:
                counts = self.server.outbox.counts_by_status()
            except Exception:  # noqa: BLE001
                counts = {}
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": SERVICE_NAME,
                    "version": HEALTH_VERSION,
                    "auth_disabled": self.server.auth_disabled,
                    "signing_required": bool(self.server.hermes_signing_secret),
                    "worker_running": bool(self.server.worker and self.server.worker.is_running()),
                    "outbox_counts": counts,
                    "endpoints": {
                        "health": "GET /health",
                        "retrieve": "POST /retrieve",
                        "chat": "POST /chat (sync) | POST /chat?async=true (enqueue)",
                        "outbox_list": "GET /outbox",
                        "outbox_row": "GET /outbox/{id}",
                    },
                },
            )
        if path == "/outbox":
            self._tx_kind = "outbox_list"
            if not self._check_auth():
                return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            counts = self.server.outbox.counts_by_status()
            dead = self.server.outbox.list_dead(limit=20)
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "counts": counts,
                    "dead_letter": [
                        {
                            "id": int(r["id"]),
                            "hermes_id": str(r.get("hermes_id", "")),
                            "attempts": int(r.get("attempts", 0)),
                            "last_error": str(r.get("last_error", ""))[:300],
                            "created_at": str(r.get("created_at", "")),
                            "updated_at": str(r.get("updated_at", "")),
                        }
                        for r in dead
                    ],
                },
            )
        if path.startswith("/outbox/"):
            self._tx_kind = "outbox_row"
            if not self._check_auth():
                return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            try:
                row_id = int(path.split("/")[-1])
            except (TypeError, ValueError):
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid outbox id"})
            row = self.server.outbox.get_by_id(row_id)
            if row is None:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "outbox_row_not_found", "id": row_id})
            try:
                result_obj = json.loads(row.get("result") or "{}")
            except Exception:  # noqa: BLE001
                result_obj = {}
            return self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "id": int(row["id"]),
                    "hermes_id": str(row.get("hermes_id", "")),
                    "status": str(row.get("status", "")),
                    "attempts": int(row.get("attempts", 0)),
                    "last_error": str(row.get("last_error", ""))[:500],
                    "result": result_obj,
                    "created_at": str(row.get("created_at", "")),
                    "updated_at": str(row.get("updated_at", "")),
                },
            )
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _dispatch_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._check_auth():
            self._tx_kind = self._kind_from_path(path)
            return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        try:
            body = self._read_json()
            if path == "/retrieve":
                self._tx_kind = "retrieve"
                payload_out = self._handle_retrieve(body)
                self._tx_extra["hits_count"] = len(payload_out.get("hits") or [])
                return self._send_json(HTTPStatus.OK, payload_out)
            if path == "/chat":
                async_mode = self._is_async_request(parsed.query, body)
                self._tx_kind = "chat_async" if async_mode else "chat"
                payload_out = self._handle_chat(body, async_mode=async_mode)
                if async_mode:
                    self._tx_extra["outbox_id"] = int(payload_out.get("outbox_id", 0))
                return self._send_json(HTTPStatus.OK, payload_out)
        except ValueError as exc:
            self._tx_kind = self._kind_from_path(path)
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            self._tx_kind = self._kind_from_path(path)
            return self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._tx_kind = self._kind_from_path(path)
            return self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error", "type": type(exc).__name__},
            )
        self._tx_kind = self._kind_from_path(path)
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _kind_from_path(self, path: str) -> str:
        if path == "/retrieve":
            return "retrieve"
        if path == "/chat":
            return "chat"
        if path == "/outbox":
            return "outbox_list"
        if path.startswith("/outbox/"):
            return "outbox_row"
        if path == "/health":
            return "health"
        return "unknown"

    def _is_async_request(self, query_string: str, body: dict[str, Any]) -> bool:
        qs = parse_qs(query_string or "")
        qa = (qs.get("async") or [""])[0].strip().lower()
        if qa in {"1", "true", "yes", "on"}:
            return True
        ba = body.get("async")
        if isinstance(ba, bool):
            return ba
        if isinstance(ba, str) and ba.strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return False

    # ─── auth + signing + namespace helpers ───

    def _check_auth(self) -> bool:
        if self.server.auth_disabled:
            return True
        expected = self.server.expected_secret
        if not expected:
            return False
        provided = self.headers.get(SECRET_HEADER, "").strip()
        return bool(provided) and provided == expected

    def _hermes_id(self) -> str:
        try:
            v = self.headers.get(HERMES_ID_HEADER, "")
        except Exception:  # noqa: BLE001
            return DEFAULT_HERMES_ID
        v = (v or "").strip()
        return v or DEFAULT_HERMES_ID

    def _check_hermes_signature(self, augmentation: str) -> tuple[bool, str]:
        """Verify HMAC-SHA256 on hermes_augmentation field.

        Returns (ok, reason):
            - augmentation 空 → ok=True (沒東西要簽)
            - HERMES_SIGNING_SECRET 沒設 → ok=True (backward compat, warn-only)
            - 設了但 header 缺 → ok=False, reason='missing_signature'
            - 設了但 sig 錯 → ok=False, reason='invalid_signature'
        """
        if not augmentation:
            return True, ""
        secret = self.server.hermes_signing_secret
        if not secret:
            return True, ""
        sig = self.headers.get(SIGNATURE_HEADER, "")
        sig = (sig or "").strip()
        if not sig:
            return False, "missing_signature"
        if not _verify_hmac_sig(secret, augmentation, sig):
            return False, "invalid_signature"
        return True, ""

    # ─── endpoint handlers ───

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
            max_results = 50

        adapter = ObsidianVaultAdapter(self.server.vault_root)
        runtime = MemoryRuntime(adapter)
        hits = runtime.search_manager.search(query=query, max_results=max_results)
        return {
            "ok": True,
            "query": query,
            "max_results": max_results,
            "hermes_id": self._hermes_id(),
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

    def _handle_chat(self, body: dict[str, Any], *, async_mode: bool = False) -> dict[str, Any]:
        persona = str(body.get("persona", "")).strip() or "core"
        message = str(body.get("user_message") or body.get("message") or "").strip()
        if not message:
            raise ValueError("user_message 為空")

        hermes_augmentation = str(body.get("hermes_augmentation", "")).strip()
        if hermes_augmentation:
            sig_ok, sig_reason = self._check_hermes_signature(hermes_augmentation)
            if not sig_ok:
                raise PermissionError(f"hermes_signature_invalid: {sig_reason}")

        hermes_id = self._hermes_id()
        transport = str(body.get("transport", "")).strip() or HERMES_TRANSPORT
        channel_id = str(body.get("channel_id", "")).strip() or "hermes-default"
        user_id = str(body.get("user_id", "")).strip() or "hermes-user"

        # 套 hermes_augmentation 進 message (跟 stage 1 對齊)
        effective_message = message
        if hermes_augmentation:
            effective_message = f"{message}\n\n[hermes_augmentation]\n{hermes_augmentation}"

        if async_mode:
            outbox_payload: dict[str, Any] = {
                "message": effective_message,
                "persona": persona,
                "transport": transport,
                "channel_id": channel_id,
                "user_id": user_id,
                "context": str(body.get("context") or "").strip(),
                "session": str(body.get("session") or "").strip(),
                "hermes_augmentation_applied": bool(hermes_augmentation),
            }
            outbox_id = self.server.outbox.enqueue(hermes_id=hermes_id, payload=outbox_payload)
            return {
                "ok": True,
                "async": True,
                "hermes_id": hermes_id,
                "outbox_id": outbox_id,
                "status": "pending",
                "poll": f"/outbox/{outbox_id}",
                "hermes_augmentation_applied": bool(hermes_augmentation),
            }

        # sync mode (stage 1 + R22.1 mapping)
        payload = {
            "message": effective_message,
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
        # R22.1 C123: transport_ingest 把 session/daily/shared_channel 收在 nested memory_paths
        memory_paths_raw = result.get("memory_paths") or {}
        memory_paths = memory_paths_raw if isinstance(memory_paths_raw, dict) else {}
        return {
            "ok": True,
            "async": False,
            "hermes_id": hermes_id,
            "persona": persona,
            "reply": str(result.get("response", "")),
            "session_path": str(memory_paths.get("session") or ""),
            "daily_path": str(memory_paths.get("daily") or ""),
            "shared_channel_path": str(memory_paths.get("shared_channel") or ""),
            "hermes_augmentation_applied": bool(hermes_augmentation),
        }

    # ─── IO helpers ───

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
        self._tx_status = int(status.value)
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
    hermes_signing_secret: str | None = None,
    start_worker: bool = True,
) -> None:
    secret = expected_secret if expected_secret is not None else _read_secret_env()
    signing = (
        hermes_signing_secret if hermes_signing_secret is not None else _read_signing_secret_env()
    )
    server = _HermesBridgeServer(
        (host, int(port)),
        Path(vault_root),
        expected_secret=secret,
        auth_disabled=auth_disabled or _auth_disabled_env(),
        hermes_signing_secret=signing,
        start_worker=start_worker,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent_Memory hermes bridge service (R22 stage 1+2)"
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
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="don't start outbox worker daemon (sync-only mode)",
    )
    args = parser.parse_args(argv)
    serve_hermes_bridge(
        Path(args.vault_root),
        host=args.host,
        port=args.port,
        auth_disabled=args.auth_disabled,
        start_worker=not args.no_worker,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
