"""Normalize inbound transport events into unified chat turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_memory.channel_bindings import resolve_channel_persona
from agent_memory.chat_runtime import run_chat_turn
from agent_memory.chat_session import (
    append_chat_turn,
    append_daily_chat_digest,
    append_shared_channel_turn,
    shared_channel_note_path,
    sanitize_component,
    session_note_path,
)
from agent_memory.dialogue_modes import load_dialogue_modes, resolve_dialogue_mode
from agent_memory.llm_client import LLMClient, LLMClientError
from agent_memory.llm_ledger import record_llm_route_event
from agent_memory.local_tools import (
    execute_llm_switch,
    execute_tool_request,
    maybe_parse_llm_switch_request,
    maybe_parse_tool_request,
    render_llm_switch_result,
    render_tool_result,
)
from agent_memory.persona_governance import load_persona_governance, resolve_persona_governance
from agent_memory.profile_scope import runtime_profile_for_persona
from agent_memory.runtime import MemoryRuntime
from agent_memory.transport_profiles import load_transport_profiles, resolve_transport_profile
from agent_memory.types import MemoryType
from agent_memory.vault import ObsidianVaultAdapter


@dataclass(slots=True)
class InboundTurn:
    transport: str
    channel_id: str
    user_id: str
    message: str
    context: str
    session: str


def _get_by_path(payload: Any, path: str) -> Any:
    current = payload
    token = ""
    index_mode = False
    parts: list[str] = []
    for char in path:
        if char == "." and not index_mode:
            if token:
                parts.append(token)
                token = ""
            continue
        if char == "[":
            if token:
                parts.append(token)
                token = ""
            index_mode = True
            continue
        if char == "]" and index_mode:
            if token:
                parts.append(token)
                token = ""
            index_mode = False
            continue
        token += char
    if token:
        parts.append(token)

    for part in parts:
        if isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
            continue
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
            continue
        return None
    return current


def _first_text(payload: dict[str, Any], candidates: list[str]) -> str:
    for key in candidates:
        value = _get_by_path(payload, key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_line(payload: dict[str, Any], profile: dict[str, Any]) -> InboundTurn:
    events = payload.get("events", [])
    event = events[0] if isinstance(events, list) and events else {}
    if not isinstance(event, dict):
        event = {}
    message = ""
    msg = event.get("message", {})
    if isinstance(msg, dict) and str(msg.get("type", "")).lower() == "text":
        message = str(msg.get("text", "")).strip()
    if not message:
        message = _first_text(payload, list(profile.get("message_candidates", [])))

    channel_id = (
        str(_get_by_path(payload, "events[0].source.groupId") or "").strip()
        or str(_get_by_path(payload, "events[0].source.roomId") or "").strip()
        or str(_get_by_path(payload, "events[0].source.userId") or "").strip()
        or str(payload.get("channel_id", "")).strip()
        or "line-default"
    )
    user_id = str(_get_by_path(payload, "events[0].source.userId") or payload.get("user_id", "")).strip()
    transport = "line"
    context = str(profile.get("context_template", "{transport}:{channel_id}")).format(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="line-default"),
        user_id=sanitize_component(user_id, fallback="user"),
    )
    session = str(profile.get("session_template", "{transport}-{channel_id}")).format(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="line-default"),
        user_id=sanitize_component(user_id, fallback="user"),
    )
    return InboundTurn(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="line-default"),
        user_id=sanitize_component(user_id, fallback="user"),
        message=message,
        context=context.strip() or "line:line-default",
        session=session.strip() or "line-line-default",
    )


def _extract_discord(payload: dict[str, Any], profile: dict[str, Any]) -> InboundTurn:
    message = _first_text(payload, list(profile.get("message_candidates", [])))
    channel_id = (
        _first_text(payload, list(profile.get("channel_candidates", [])))
        or str(payload.get("channel_id", "")).strip()
        or "discord-default"
    )
    user_id = _first_text(payload, list(profile.get("user_candidates", []))) or str(payload.get("user_id", "")).strip()
    transport = "discord"
    context = str(profile.get("context_template", "{transport}:{channel_id}")).format(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="discord-default"),
        user_id=sanitize_component(user_id, fallback="user"),
    )
    session = str(profile.get("session_template", "{transport}-{channel_id}")).format(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="discord-default"),
        user_id=sanitize_component(user_id, fallback="user"),
    )
    return InboundTurn(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="discord-default"),
        user_id=sanitize_component(user_id, fallback="user"),
        message=message,
        context=context.strip() or "discord:discord-default",
        session=session.strip() or "discord-discord-default",
    )


def _extract_generic(payload: dict[str, Any], profile: dict[str, Any]) -> InboundTurn:
    transport = sanitize_component(str(profile.get("transport", "web")), fallback="web").lower()
    message = _first_text(payload, list(profile.get("message_candidates", ["message", "text", "content"])))
    channel_id = (
        _first_text(payload, list(profile.get("channel_candidates", ["channel_id", "conversation_id", "thread_id", "user_id"])))
        or "default"
    )
    user_id = _first_text(payload, list(profile.get("user_candidates", ["user_id", "author_id"]))) or "user"
    context = str(profile.get("context_template", "{transport}:{channel_id}")).format(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="default"),
        user_id=sanitize_component(user_id, fallback="user"),
    )
    session = str(profile.get("session_template", "{transport}-{channel_id}")).format(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="default"),
        user_id=sanitize_component(user_id, fallback="user"),
    )
    return InboundTurn(
        transport=transport,
        channel_id=sanitize_component(channel_id, fallback="default"),
        user_id=sanitize_component(user_id, fallback="user"),
        message=message,
        context=context.strip() or f"{transport}:default",
        session=session.strip() or f"{transport}-default",
    )


def parse_inbound_turn(transport: str, payload: dict[str, Any], profiles_config: dict[str, Any]) -> InboundTurn:
    profile = resolve_transport_profile(profiles_config, transport)
    parser = str(profile.get("parser", "generic")).strip().lower()
    if parser == "line_webhook":
        turn = _extract_line(payload, profile)
    elif parser == "discord_message":
        turn = _extract_discord(payload, profile)
    else:
        turn = _extract_generic(payload, profile)

    if not turn.message.strip():
        raise ValueError("事件中未找到可用訊息文字")
    return turn


def _degraded_reply(reason: str) -> str:
    compact = " ".join(str(reason).split())
    if len(compact) > 220:
        compact = compact[:220] + "..."
    return (
        "目前模型連線暫時不可用，我已收到並記錄你的訊息。"
        "請稍後重試，或檢查可用模型/GGUF 路徑/API 金鑰設定。\n\n"
        f"診斷摘要：{compact}"
    )


def _persist_local_response(
    *,
    adapter: ObsidianVaultAdapter,
    runtime: MemoryRuntime,
    persona: str,
    context_id: str,
    session_id: str,
    user_message: str,
    assistant_message: str,
    memory_mode: str,
) -> dict[str, str | None]:
    probe_session_path = session_note_path(
        adapter,
        persona_id=persona,
        context_id=context_id,
        session_id=session_id,
    )
    if not runtime.profile.can_write(probe_session_path):
        raise PermissionError(f"persona={persona} 無法寫入 session 路徑：{probe_session_path}")

    session_path = append_chat_turn(
        adapter,
        persona_id=persona,
        context_id=context_id,
        session_id=session_id,
        user_message=user_message,
        assistant_message=assistant_message,
        now=datetime.now(),
    )
    runtime.search_manager.index_path(session_path)

    daily_path: str | None = None
    if memory_mode == "session_and_daily":
        daily_preview = adapter.resolve_path(MemoryType.SHORT_TERM, datetime.now().strftime("%Y-%m-%d"))
        if runtime.profile.can_write(daily_preview):
            daily_path, _ = append_daily_chat_digest(
                adapter,
                persona_id=persona,
                session_id=session_id,
                user_message=user_message,
                assistant_message=assistant_message,
                now=datetime.now(),
            )
            runtime.search_manager.index_path(daily_path)
    runtime.sync_user_index_views()
    return {
        "session": session_path,
        "daily": daily_path,
    }


def run_transport_event(
    *,
    vault_root: Path,
    transport: str,
    payload: dict[str, Any],
    explicit_persona: str | None = None,
    context_override: str | None = None,
    session_override: str | None = None,
    override_profile: str | None = None,
    override_model: str | None = None,
    temperature: float = 0.2,
    timeout_s: float = 90.0,
    memory_mode: str = "session_and_daily",
    dialogue_mode: str | None = None,
    allow_llm_degraded: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()

    profiles = load_transport_profiles(root)
    profile = resolve_transport_profile(profiles, transport)
    if not bool(profile.get("enabled", True)):
        raise PermissionError(f"transport={transport} 已停用")

    turn = parse_inbound_turn(transport, payload, profiles)
    persona = ""
    if explicit_persona and explicit_persona.strip():
        persona = sanitize_component(explicit_persona, fallback="core").lower()
    elif bool(profile.get("use_binding", True)):
        persona = resolve_channel_persona(
            root,
            transport=turn.transport,
            channel_id=turn.channel_id,
            fallback_persona="core",
        )
    else:
        persona = "core"

    context_value = context_override.strip() if context_override and context_override.strip() else turn.context
    session_value = session_override.strip() if session_override and session_override.strip() else turn.session
    requested_mode = str(dialogue_mode or "").strip()
    if not requested_mode:
        requested_mode = _first_text(
            payload,
            list(profile.get("dialogue_mode_candidates", ["dialogue_mode", "mode", "bridge_mode"])),
        )
    mode_config = load_dialogue_modes(root)
    mode_resolved = resolve_dialogue_mode(
        mode_config,
        persona_id=persona,
        transport=turn.transport,
        requested_mode=requested_mode or None,
    )

    runtime = MemoryRuntime(adapter, profile=runtime_profile_for_persona(adapter, persona))
    client = LLMClient(adapter.vault_root)
    shared_channel_history = ""
    if turn.channel_id:
        try:
            shared_path = shared_channel_note_path(
                adapter,
                transport=turn.transport,
                channel_id=turn.channel_id,
                date_str=datetime.now().strftime("%Y-%m-%d"),
            )
            if runtime.profile.can_read(shared_path):
                shared_note = adapter.read_note(shared_path)
                if shared_note is not None:
                    text = shared_note.body.strip()
                    shared_channel_history = text[-2400:] if len(text) > 2400 else text
        except Exception:  # noqa: BLE001
            shared_channel_history = ""

    # ===== /llm 對話中切模型短路 =====
    llm_switch_response: str | None = None
    llm_switch_payload: dict[str, Any] | None = None
    parsed_llm_switch: dict[str, Any] | None = None
    try:
        parsed_llm_switch = maybe_parse_llm_switch_request(turn.message)
    except Exception as exc:  # noqa: BLE001
        parsed_llm_switch = {"action": "_parse_error", "error": str(exc)}

    if parsed_llm_switch is not None:
        # Gating: write 動作（switch_default / switch_persona）需要 tools_enabled；
        # read 動作（list / show / help）任何 persona 都可用。
        action = parsed_llm_switch.get("action")
        write_actions = {"switch_default", "switch_persona"}
        if action == "_parse_error":
            llm_switch_payload = {"ok": False, "error": parsed_llm_switch.get("error")}
            llm_switch_response = f"[llm:err] {parsed_llm_switch.get('error')}"
        elif action in write_actions:
            governance_for_llm = resolve_persona_governance(load_persona_governance(root), persona_id=persona)
            caps_for_llm = governance_for_llm.get("capabilities", {})
            if not isinstance(caps_for_llm, dict):
                caps_for_llm = {}
            if not bool(caps_for_llm.get("tools_enabled", False)):
                llm_switch_payload = {"ok": False, "error": "tools_disabled_for_persona", "persona": persona}
                llm_switch_response = "[llm:denied] 此角色未啟用工具能力,無法切換模型。請改用 tooling 角色（如 steward）。"
            else:
                try:
                    llm_switch_payload = execute_llm_switch(adapter.vault_root, parsed_llm_switch)
                    llm_switch_response = render_llm_switch_result(llm_switch_payload)
                except Exception as exc:  # noqa: BLE001
                    llm_switch_payload = {"ok": False, "error": str(exc)}
                    llm_switch_response = f"[llm:err] {exc}"
        else:
            # read action — 不需要 tools_enabled
            try:
                llm_switch_payload = execute_llm_switch(adapter.vault_root, parsed_llm_switch)
                llm_switch_response = render_llm_switch_result(llm_switch_payload)
            except Exception as exc:  # noqa: BLE001
                llm_switch_payload = {"ok": False, "error": str(exc)}
                llm_switch_response = f"[llm:err] {exc}"

        memory_paths_llm = _persist_local_response(
            adapter=adapter,
            runtime=runtime,
            persona=persona,
            context_id=context_value,
            session_id=session_value,
            user_message=turn.message,
            assistant_message=str(llm_switch_response or ""),
            memory_mode=memory_mode,
        )
        llm_meta = {
            "profile": "llm_switch",
            "model": "llm_switch",
            "kind": "system_command",
            "base_url": "local://llm-switch",
            "fallback_failures": [],
        }
        route_event_llm = None
        try:
            route_event_llm = record_llm_route_event(
                adapter.vault_root,
                persona_id=persona,
                context_id=context_value,
                session_id=session_value,
                llm=llm_meta,
                memory_paths=memory_paths_llm,
                message=turn.message,
                response=str(llm_switch_response or ""),
                transport=turn.transport,
                channel_id=turn.channel_id,
                user_id=turn.user_id,
            )
        except Exception:  # noqa: BLE001
            route_event_llm = None

        result_llm = {
            "persona": persona,
            "context": context_value,
            "session": session_value,
            "dialogue_mode": {
                "mode": mode_resolved["mode"],
                "label": mode_resolved.get("label", mode_resolved["mode"]),
                "source": mode_resolved.get("source", "global_default"),
            },
            "response": str(llm_switch_response or ""),
            "skills_context": [],
            "llm": llm_meta,
            "llm_route_event": route_event_llm,
            "memory_paths": memory_paths_llm,
            "degraded": False,
            "llm_switch_payload": llm_switch_payload or {},
            "transport": turn.transport,
            "channel_id": turn.channel_id,
            "user_id": turn.user_id,
            "inbound": {
                "context": context_value,
                "session": session_value,
                "message": turn.message,
            },
            "resolved_by_binding": not bool(explicit_persona and explicit_persona.strip()),
        }
        return result_llm

    tool_response: str | None = None
    tool_payload: dict[str, Any] | None = None
    parsed_tool_request: dict[str, Any] | None = None
    try:
        parsed_tool_request = maybe_parse_tool_request(turn.message)
    except Exception as exc:  # noqa: BLE001
        parsed_tool_request = {}
        tool_payload = {
            "ok": False,
            "error": f"invalid_tool_request: {exc}",
        }
        tool_response = f"[tool:error] {exc}"

    if parsed_tool_request is not None:
        governance = resolve_persona_governance(load_persona_governance(root), persona_id=persona)
        caps = governance.get("capabilities", {})
        if not isinstance(caps, dict):
            caps = {}
        if tool_response is None:
            tools_enabled = bool(caps.get("tools_enabled", False))
            if not tools_enabled:
                tool_payload = {
                    "ok": False,
                    "error": "tools_disabled_for_persona",
                    "persona": persona,
                }
                tool_response = "[tool:denied] 此角色未啟用工具能力。請改用 tooling 角色或先 persona-update 開啟工具。"
            else:
                try:
                    tool_payload = execute_tool_request(
                        vault_root=adapter.vault_root,
                        workspace_root=Path.cwd().resolve(),
                        request=parsed_tool_request,
                    )
                    tool_response = render_tool_result(tool_payload)
                except Exception as exc:  # noqa: BLE001
                    tool_payload = {
                        "ok": False,
                        "error": str(exc),
                    }
                    tool_response = f"[tool:error] {exc}"

        memory_paths = _persist_local_response(
            adapter=adapter,
            runtime=runtime,
            persona=persona,
            context_id=context_value,
            session_id=session_value,
            user_message=turn.message,
            assistant_message=str(tool_response or ""),
            memory_mode=memory_mode,
        )
        llm_payload = {
            "profile": "local_tools",
            "model": "local_tools",
            "kind": "tool_executor",
            "base_url": "local://tool-executor",
            "fallback_failures": [],
        }
        route_event = None
        try:
            route_event = record_llm_route_event(
                adapter.vault_root,
                persona_id=persona,
                context_id=context_value,
                session_id=session_value,
                llm=llm_payload,
                memory_paths=memory_paths,
                message=turn.message,
                response=str(tool_response or ""),
                transport=turn.transport,
                channel_id=turn.channel_id,
                user_id=turn.user_id,
            )
        except Exception:  # noqa: BLE001
            route_event = None

        result = {
            "persona": persona,
            "context": context_value,
            "session": session_value,
            "dialogue_mode": {
                "mode": mode_resolved["mode"],
                "label": mode_resolved.get("label", mode_resolved["mode"]),
                "source": mode_resolved.get("source", "global_default"),
            },
            "response": str(tool_response or ""),
            "skills_context": [],
            "llm": llm_payload,
            "llm_route_event": route_event,
            "memory_paths": memory_paths,
            "degraded": False,
            "tool_payload": tool_payload or {},
        }
        result["transport"] = turn.transport
        result["channel_id"] = turn.channel_id
        result["user_id"] = turn.user_id
        result["inbound"] = {
            "context": context_value,
            "session": session_value,
            "message": turn.message,
        }
        shared_channel_path: str | None = None
        if turn.channel_id:
            try:
                shared_channel_path = append_shared_channel_turn(
                    adapter,
                    transport=turn.transport,
                    channel_id=turn.channel_id,
                    persona_id=persona,
                    user_id=turn.user_id,
                    user_message=turn.message,
                    assistant_message=str(result.get("response", "")),
                    now=datetime.now(),
                )
                runtime.search_manager.index_path(shared_channel_path)
            except Exception:  # noqa: BLE001
                shared_channel_path = None
        if isinstance(result.get("memory_paths"), dict):
            result["memory_paths"]["shared_channel"] = shared_channel_path
        else:
            result["memory_paths"] = {
                "shared_channel": shared_channel_path,
            }
        result["resolved_by_binding"] = not bool(explicit_persona and explicit_persona.strip())
        return result

    try:
        result = run_chat_turn(
            adapter=adapter,
            runtime=runtime,
            client=client,
            persona=persona,
            context=context_value,
            session=session_value,
            message=turn.message,
            override_profile=override_profile,
            override_model=override_model,
            temperature=temperature,
            timeout_s=timeout_s,
            memory_mode=memory_mode,
            transport=turn.transport,
            channel_id=turn.channel_id,
            user_id=turn.user_id,
            dialogue_mode=mode_resolved["mode"],
            dialogue_prompt=mode_resolved.get("prompt", ""),
            shared_channel_history=shared_channel_history,
        )
        result["dialogue_mode"] = {
            "mode": mode_resolved["mode"],
            "label": mode_resolved.get("label", mode_resolved["mode"]),
            "source": mode_resolved.get("source", "global_default"),
        }
        result["degraded"] = False
    except LLMClientError as exc:
        if not allow_llm_degraded:
            raise

        degraded_text = _degraded_reply(str(exc))
        probe_session_path = session_note_path(
            adapter,
            persona_id=persona,
            context_id=context_value,
            session_id=session_value,
        )
        if not runtime.profile.can_write(probe_session_path):
            raise PermissionError(f"persona={persona} 無權寫入 session 路徑：{probe_session_path}") from exc

        session_path = append_chat_turn(
            adapter,
            persona_id=persona,
            context_id=context_value,
            session_id=session_value,
            user_message=turn.message,
            assistant_message=degraded_text,
            now=datetime.now(),
        )
        runtime.search_manager.index_path(session_path)

        daily_path: str | None = None
        if memory_mode == "session_and_daily":
            daily_preview = adapter.resolve_path(MemoryType.SHORT_TERM, datetime.now().strftime("%Y-%m-%d"))
            if runtime.profile.can_write(daily_preview):
                daily_path, _ = append_daily_chat_digest(
                    adapter,
                    persona_id=persona,
                    session_id=session_value,
                    user_message=turn.message,
                    assistant_message=degraded_text,
                    now=datetime.now(),
                )
                runtime.search_manager.index_path(daily_path)

        runtime.sync_user_index_views()
        llm_payload = {
            "profile": "unavailable",
            "model": "unavailable",
            "kind": "degraded",
            "base_url": "",
            "fallback_failures": [{"profile": "route", "model": "route", "reason": str(exc)}],
        }
        route_event = None
        try:
            route_event = record_llm_route_event(
                adapter.vault_root,
                persona_id=persona,
                context_id=context_value,
                session_id=session_value,
                llm=llm_payload,
                memory_paths={"session": session_path, "daily": daily_path},
                message=turn.message,
                response=degraded_text,
                transport=turn.transport,
                channel_id=turn.channel_id,
                user_id=turn.user_id,
            )
        except Exception:  # noqa: BLE001
            route_event = None

        result = {
            "persona": persona,
            "context": context_value,
            "session": session_value,
            "dialogue_mode": {
                "mode": mode_resolved["mode"],
                "label": mode_resolved.get("label", mode_resolved["mode"]),
                "source": mode_resolved.get("source", "global_default"),
            },
            "response": degraded_text,
            "skills_context": [],
            "llm": llm_payload,
            "llm_route_event": route_event,
            "memory_paths": {
                "session": session_path,
                "daily": daily_path,
            },
            "degraded": True,
            "degraded_reason": str(exc),
        }

    result["transport"] = turn.transport
    result["channel_id"] = turn.channel_id
    result["user_id"] = turn.user_id
    result["inbound"] = {
        "context": context_value,
        "session": session_value,
        "message": turn.message,
    }
    shared_channel_path: str | None = None
    if turn.channel_id:
        try:
            shared_channel_path = append_shared_channel_turn(
                adapter,
                transport=turn.transport,
                channel_id=turn.channel_id,
                persona_id=persona,
                user_id=turn.user_id,
                user_message=turn.message,
                assistant_message=str(result.get("response", "")),
                now=datetime.now(),
            )
            runtime.search_manager.index_path(shared_channel_path)
        except Exception:  # noqa: BLE001
            shared_channel_path = None
    if isinstance(result.get("memory_paths"), dict):
        result["memory_paths"]["shared_channel"] = shared_channel_path
    else:
        result["memory_paths"] = {
            "shared_channel": shared_channel_path,
        }
    result["resolved_by_binding"] = not bool(explicit_persona and explicit_persona.strip())
    return result
