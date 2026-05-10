"""Local dashboard/API server for multi-transport agent control."""

from __future__ import annotations

import json
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_memory.channel_bindings import (
    bind_channel_persona,
    list_channel_bindings,
    unbind_channel,
)
from agent_memory.chat_session import sanitize_component
from agent_memory.llm_routing import load_llm_router_config, resolve_llm_route
from agent_memory.dialogue_modes import load_dialogue_modes
from agent_memory.persona_factory import list_personas
from agent_memory.persona_governance import load_persona_governance, resolve_persona_governance
from agent_memory.profile_scope import load_yaml_object
from agent_memory.transport_ingest import run_transport_event
from agent_memory.transport_profiles import load_transport_profiles, resolve_transport_profile
from agent_memory.vault import ObsidianVaultAdapter


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


def _recent_markdown_files(vault_root: Path, relative_dir: str, *, limit: int = 20) -> list[dict[str, str]]:
    target = (vault_root / relative_dir).resolve()
    if not target.exists():
        return []
    files = [
        p
        for p in target.rglob("*.md")
        if p.is_file() and p.name != "_DIR_INFO.md"
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    rows: list[dict[str, str]] = []
    for path in files[:limit]:
        rel = str(path.relative_to(vault_root)).replace("\\", "/")
        ts = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        rows.append({"path": rel, "updated_at": ts})
    return rows


def build_dashboard_state(vault_root: Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()

    manifest = load_yaml_object(str(adapter.absolute_path("00_System/08_Runtime_Profiles/brain_manifest.yaml")))
    registry = list_personas(vault_root=root)
    governance_cfg = load_persona_governance(root)
    governance_summary: dict[str, Any] = {}
    personas_map = registry.get("personas", {})
    if isinstance(personas_map, dict):
        for pid in sorted(personas_map):
            governance_summary[pid] = resolve_persona_governance(governance_cfg, persona_id=str(pid))
    bindings = list_channel_bindings(root)
    llm_cfg = load_llm_router_config(root)
    llm_core = resolve_llm_route(llm_cfg, persona_id="core")
    dialogue_cfg = load_dialogue_modes(root)
    dialogue_modes = dialogue_cfg.get("modes", {})
    if not isinstance(dialogue_modes, dict):
        dialogue_modes = {}
    dialogue_summary: dict[str, dict[str, str]] = {}
    for mode_id in sorted(dialogue_modes):
        mode_payload = dialogue_modes.get(mode_id, {})
        if not isinstance(mode_payload, dict):
            mode_payload = {}
        dialogue_summary[mode_id] = {
            "label": str(mode_payload.get("label", mode_id)),
            "prompt": str(mode_payload.get("prompt", "")),
        }
    transport_cfg = load_transport_profiles(root)
    transports = transport_cfg.get("transports", {})
    if not isinstance(transports, dict):
        transports = {}
    transport_summary: dict[str, dict[str, Any]] = {}
    for name in sorted(transports):
        profile = resolve_transport_profile(transport_cfg, str(name))
        transport_summary[str(name)] = {
            "enabled": bool(profile.get("enabled", True)),
            "parser": str(profile.get("parser", "generic")),
            "use_binding": bool(profile.get("use_binding", True)),
        }
    recent_sessions = _recent_markdown_files(root, "70_Active_Plans/Session_Logs", limit=15)
    recent_daily = _recent_markdown_files(root, "11_AI_Mirror/ingestion_logs/daily_flush", limit=7)
    recent_events = _recent_markdown_files(root, "11_AI_Mirror/ingestion_logs", limit=10)

    return {
        "vault_root": str(root),
        "manifest": manifest,
        "personas": registry,
        "governance": governance_summary,
        "bindings": bindings,
        "transports": transport_summary,
        "dialogue_modes": dialogue_summary,
        "llm_core_route": llm_core,
        "recent_sessions": recent_sessions,
        "recent_daily_flush": recent_daily,
        "recent_ingestion_logs": recent_events,
    }


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <title>Agent Memory Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #0f1115; color: #e5e7eb; }
    h1, h2 { margin: 10px 0; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .card { border: 1px solid #2d3748; border-radius: 10px; padding: 12px; background: #161a23; }
    label { display: block; margin: 8px 0 4px; font-size: 13px; color: #cbd5e1; }
    input, textarea, button, select {
      width: 100%; box-sizing: border-box; border-radius: 6px; border: 1px solid #334155;
      background: #0b1220; color: #f8fafc; padding: 8px;
    }
    button { cursor: pointer; background: #1d4ed8; border: none; margin-top: 8px; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; background: #0b1220; padding: 8px; border-radius: 6px; }
    ul { padding-left: 18px; }
    .mono { font-family: Consolas, monospace; font-size: 12px; }
  </style>
</head>
<body>
  <h1>Agent Memory 控制台</h1>
  <p class="mono" id="vault">載入中...</p>
  <div class="grid">
    <div class="card">
      <h2>聊天測試（任一媒介）</h2>
      <label>Transport</label><input id="transport" value="web" />
      <label>Channel ID</label><input id="channelId" value="dashboard-main" />
      <label>Persona（可留空，用綁定）</label><input id="persona" placeholder="core" />
      <label>Dialogue Mode（可留空）</label><input id="dialogueMode" placeholder="standard / coach / strategist / executor" />
      <label>Session（可留空）</label><input id="session" placeholder="web-dashboard-main" />
      <label>訊息</label><textarea id="message" rows="4">請回覆：管理介面測試成功</textarea>
      <button id="sendBtn">送出聊天</button>
      <pre id="chatResult"></pre>
    </div>
    <div class="card">
      <h2>Channel 綁定 Persona</h2>
      <label>Transport</label><input id="bindTransport" value="web" />
      <label>Channel ID</label><input id="bindChannelId" value="dashboard-main" />
      <label>Persona</label><input id="bindPersona" value="core" />
      <button id="bindBtn">綁定</button>
      <button id="unbindBtn" style="background:#374151;">解除綁定</button>
      <pre id="bindResult"></pre>
    </div>
  </div>

  <div class="grid" style="margin-top:16px;">
    <div class="card">
      <h2>人格狀態</h2>
      <pre id="personas"></pre>
    </div>
    <div class="card">
      <h2>Channel 綁定狀態</h2>
      <pre id="bindings"></pre>
    </div>
  </div>

  <div class="grid" style="margin-top:16px;">
    <div class="card">
      <h2>Transport Profiles</h2>
      <pre id="transports"></pre>
    </div>
    <div class="card">
      <h2>LLM Core Route</h2>
      <pre id="llmCore"></pre>
    </div>
    <div class="card">
      <h2>Dialogue Modes</h2>
      <pre id="dialogueModes"></pre>
    </div>
  </div>

  <div class="grid" style="margin-top:16px;">
    <div class="card">
      <h2>最近 Session 記憶</h2>
      <ul id="sessions"></ul>
    </div>
    <div class="card">
      <h2>最近 Daily Flush</h2>
      <ul id="daily"></ul>
    </div>
  </div>

  <script>
    async function api(path, method='GET', body=null) {
      const options = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) options.body = JSON.stringify(body);
      const res = await fetch(path, options);
      return await res.json();
    }
    function setList(id, rows) {
      const ul = document.getElementById(id);
      ul.innerHTML = '';
      rows.forEach(r => {
        const li = document.createElement('li');
        li.textContent = `${r.path} (${r.updated_at})`;
        ul.appendChild(li);
      });
    }
    async function refreshState() {
      const state = await api('/api/state');
      document.getElementById('vault').textContent = `Vault: ${state.vault_root}`;
      document.getElementById('personas').textContent = JSON.stringify(state.personas, null, 2);
      document.getElementById('bindings').textContent = JSON.stringify(state.bindings, null, 2);
      document.getElementById('transports').textContent = JSON.stringify(state.transports, null, 2);
      document.getElementById('llmCore').textContent = JSON.stringify(state.llm_core_route, null, 2);
      document.getElementById('dialogueModes').textContent = JSON.stringify(state.dialogue_modes, null, 2);
      setList('sessions', state.recent_sessions || []);
      setList('daily', state.recent_daily_flush || []);
    }
    document.getElementById('sendBtn').onclick = async () => {
      const payload = {
        transport: document.getElementById('transport').value,
        channel_id: document.getElementById('channelId').value,
        persona: document.getElementById('persona').value,
        dialogue_mode: document.getElementById('dialogueMode').value,
        session: document.getElementById('session').value,
        message: document.getElementById('message').value
      };
      const result = await api('/api/chat', 'POST', payload);
      document.getElementById('chatResult').textContent = JSON.stringify(result, null, 2);
      await refreshState();
    };
    document.getElementById('bindBtn').onclick = async () => {
      const payload = {
        transport: document.getElementById('bindTransport').value,
        channel_id: document.getElementById('bindChannelId').value,
        persona: document.getElementById('bindPersona').value
      };
      const result = await api('/api/channel-bind', 'POST', payload);
      document.getElementById('bindResult').textContent = JSON.stringify(result, null, 2);
      await refreshState();
    };
    document.getElementById('unbindBtn').onclick = async () => {
      const payload = {
        transport: document.getElementById('bindTransport').value,
        channel_id: document.getElementById('bindChannelId').value
      };
      const result = await api('/api/channel-unbind', 'POST', payload);
      document.getElementById('bindResult').textContent = JSON.stringify(result, null, 2);
      await refreshState();
    };
    refreshState();
    setInterval(refreshState, 10000);
  </script>
</body>
</html>
"""


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve()
        super().__init__(server_address, _DashboardHandler)


class _DashboardHandler(BaseHTTPRequestHandler):
    server: _DashboardServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._send_html(_dashboard_html())
        if parsed.path == "/api/state":
            return self._send_json(HTTPStatus.OK, build_dashboard_state(self.server.vault_root))
        if parsed.path == "/api/transports":
            state = build_dashboard_state(self.server.vault_root)
            return self._send_json(HTTPStatus.OK, state.get("transports", {}))
        if parsed.path == "/api/chat":
            query = parse_qs(parsed.query)
            return self._send_json(HTTPStatus.OK, {"hint": "Use POST /api/chat", "query": query})
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json()
        try:
            if parsed.path.startswith("/webhook/"):
                transport = parsed.path.removeprefix("/webhook/").strip("/")
                return self._send_json(HTTPStatus.OK, self._handle_webhook(transport, body))
            if parsed.path == "/api/chat":
                return self._send_json(HTTPStatus.OK, self._handle_chat(body))
            if parsed.path == "/api/channel-bind":
                return self._send_json(HTTPStatus.OK, self._handle_channel_bind(body))
            if parsed.path == "/api/channel-unbind":
                return self._send_json(HTTPStatus.OK, self._handle_channel_unbind(body))
        except Exception as exc:  # noqa: BLE001
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        message = str(body.get("message", "")).strip()
        if not message:
            raise ValueError("message 不可為空")

        transport = sanitize_component(str(body.get("transport", "web")), fallback="web").lower()
        channel_id_raw = str(body.get("channel_id", "")).strip() or "dashboard-main"
        channel_id = sanitize_component(channel_id_raw, fallback="dashboard-main").lower()
        explicit_persona = str(body.get("persona", "")).strip() or None
        memory_mode = str(body.get("memory_mode", "session_and_daily")).strip() or "session_and_daily"
        payload = {
            "message": message,
            "channel_id": channel_id,
            "user_id": str(body.get("user_id", "dashboard-user")).strip() or "dashboard-user",
        }
        return run_transport_event(
            vault_root=self.server.vault_root,
            transport=transport,
            payload=payload,
            explicit_persona=explicit_persona,
            context_override=str(body.get("context", "")).strip() or None,
            session_override=str(body.get("session", "")).strip() or None,
            override_profile=str(body.get("override_profile", "")).strip() or None,
            override_model=str(body.get("override_model", "")).strip() or None,
            temperature=float(body.get("temperature", 0.2)),
            timeout_s=float(body.get("timeout", 90.0)),
            memory_mode=memory_mode,
            dialogue_mode=str(body.get("dialogue_mode", body.get("mode", ""))).strip() or None,
            allow_llm_degraded=_coerce_bool(body.get("allow_llm_degraded"), True),
        )

    def _handle_webhook(self, transport: str, body: dict[str, Any]) -> dict[str, Any]:
        transport_name = sanitize_component(transport, fallback="web").lower()
        return run_transport_event(
            vault_root=self.server.vault_root,
            transport=transport_name,
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

    def _handle_channel_bind(self, body: dict[str, Any]) -> dict[str, Any]:
        transport = str(body.get("transport", "")).strip()
        channel_id = str(body.get("channel_id", "")).strip()
        persona = str(body.get("persona", "")).strip()
        if not transport or not channel_id or not persona:
            raise ValueError("transport/channel_id/persona 不可為空")
        path, key = bind_channel_persona(
            self.server.vault_root,
            transport=transport,
            channel_id=channel_id,
            persona_id=persona,
            operator="dashboard",
        )
        return {"ok": True, "bindings_path": str(path), "key": key}

    def _handle_channel_unbind(self, body: dict[str, Any]) -> dict[str, Any]:
        transport = str(body.get("transport", "")).strip()
        channel_id = str(body.get("channel_id", "")).strip()
        if not transport or not channel_id:
            raise ValueError("transport/channel_id 不可為空")
        path, key, removed = unbind_channel(self.server.vault_root, transport=transport, channel_id=channel_id)
        return {"ok": True, "bindings_path": str(path), "key": key, "removed": removed}

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

    def _send_html(self, content: str) -> None:
        raw = content.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Keep terminal output clean.
        return


def serve_dashboard(vault_root: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = _DashboardServer((host, int(port)), Path(vault_root))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
