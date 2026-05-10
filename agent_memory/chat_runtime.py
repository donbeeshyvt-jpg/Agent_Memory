"""Shared chat turn execution for CLI, APIs, and transport adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_memory.chat_session import append_chat_turn, append_daily_chat_digest, session_note_path
from agent_memory.llm_client import LLMClient
from agent_memory.llm_ledger import record_llm_route_event
from agent_memory.runtime import MemoryRuntime
from agent_memory.skill_library import build_skill_prompt_context, record_skill_usage
from agent_memory.types import MemoryType
from agent_memory.vault import ObsidianVaultAdapter


def _tail_excerpt(text: str, *, max_chars: int = 3000) -> str:
    compact = text.strip()
    if len(compact) <= max_chars:
        return compact
    return compact[-max_chars:]


def _safe_snapshot(runtime: MemoryRuntime) -> str:
    try:
        return runtime.frozen_snapshot()
    except Exception:  # noqa: BLE001
        return "<USER_PROFILE_SNAPSHOT>\n(missing)\n</USER_PROFILE_SNAPSHOT>\n\n<AGENT_MEMORY_SNAPSHOT>\n(missing)\n</AGENT_MEMORY_SNAPSHOT>\n"


def run_chat_turn(
    *,
    adapter: ObsidianVaultAdapter,
    runtime: MemoryRuntime,
    client: LLMClient,
    persona: str,
    context: str,
    session: str,
    message: str,
    override_profile: str | None = None,
    override_model: str | None = None,
    temperature: float = 0.2,
    timeout_s: float = 90.0,
    memory_mode: str = "session_and_daily",
    transport: str = "",
    channel_id: str = "",
    user_id: str = "",
    dialogue_mode: str = "standard",
    dialogue_prompt: str = "",
    shared_channel_history: str = "",
) -> dict[str, Any]:
    hist_path = session_note_path(
        adapter,
        persona_id=persona,
        context_id=context,
        session_id=session,
    )
    hist_note = adapter.read_note(hist_path) if runtime.profile.can_read(hist_path) else None
    history_tail = _tail_excerpt(hist_note.body if hist_note else "", max_chars=2400)
    skill_context = ""
    selected_skills: list[dict[str, Any]] = []
    try:
        skill_context, selected_skills = build_skill_prompt_context(
            adapter.vault_root,
            persona_id=persona,
            query=message,
            max_results=4,
        )
    except Exception:  # noqa: BLE001
        skill_context = ""
        selected_skills = []
    snapshot = _safe_snapshot(runtime)
    system_prompt = (
        "你是 Agent_Memory 的對話核心。請用繁體中文回覆，內容要可執行、可追蹤。"
        "你正在使用本地/外部可路由模型，並依照提供的記憶快照回答。\n\n"
        "若資訊不足以安全執行，先向使用者提問，不要自行臆測。"
        "若任務涉及多人協作，優先拆成清單並明確標示責任角色。\n\n"
        "以下是凍結快照（不可改寫其內容）：\n"
        f"{snapshot}\n"
    )
    mode_id = str(dialogue_mode or "standard").strip().lower() or "standard"
    mode_prompt = str(dialogue_prompt or "").strip()
    system_prompt += f"\n目前對話模式：{mode_id}\n"
    if mode_prompt:
        system_prompt += f"模式規則：{mode_prompt}\n"
    if skill_context:
        system_prompt += "\n" + skill_context + "\n"
    if history_tail:
        system_prompt += "\n以下是本 session 最近對話摘錄（供延續語境）：\n" + history_tail + "\n"
    shared_history = _tail_excerpt(str(shared_channel_history or ""), max_chars=2400)
    if shared_history:
        system_prompt += "\n以下是共通頻道近期摘錄（跨角色共享）：\n" + shared_history + "\n"

    llm_result = client.generate(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        persona_id=persona,
        override_profile=override_profile,
        override_model=override_model,
        temperature=float(temperature),
        timeout_s=float(timeout_s),
    )

    response_text = llm_result.content.strip()
    if not runtime.profile.can_write(hist_path):
        raise PermissionError(f"persona={persona} 無權寫入 session 路徑：{hist_path}")

    session_path = append_chat_turn(
        adapter,
        persona_id=persona,
        context_id=context,
        session_id=session,
        user_message=message,
        assistant_message=response_text,
    )
    runtime.search_manager.index_path(session_path)

    daily_path = None
    if memory_mode == "session_and_daily":
        daily_preview = adapter.resolve_path(MemoryType.SHORT_TERM, datetime.now().strftime("%Y-%m-%d"))
        if not runtime.profile.can_write(daily_preview):
            raise PermissionError(f"persona={persona} 無權寫入 daily 路徑：{daily_preview}")
        daily_path, _ = append_daily_chat_digest(
            adapter,
            persona_id=persona,
            session_id=session,
            user_message=message,
            assistant_message=response_text,
        )
        runtime.search_manager.index_path(daily_path)

    for item in selected_skills:
        sid = str(item.get("skill_id", "")).strip()
        if not sid:
            continue
        try:
            record_skill_usage(
                adapter.vault_root,
                persona_id=persona,
                skill_id=sid,
                scope=str(item.get("scope", "auto")),
                operator=persona,
                success=None,
                note="auto_context_in_chat",
            )
        except Exception:  # noqa: BLE001
            continue

    runtime.sync_user_index_views()
    llm_payload = {
        "profile": llm_result.profile,
        "model": llm_result.model,
        "kind": llm_result.provider_kind,
        "base_url": llm_result.base_url,
        "fallback_failures": [
            {"profile": f.profile, "model": f.model, "reason": f.reason}
            for f in llm_result.attempts
        ],
    }
    route_event = None
    try:
        route_event = record_llm_route_event(
            adapter.vault_root,
            persona_id=persona,
            context_id=context,
            session_id=session,
            llm=llm_payload,
            memory_paths={"session": session_path, "daily": daily_path},
            message=message,
            response=response_text,
            transport=transport,
            channel_id=channel_id,
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        route_event = None

    return {
        "persona": persona,
        "context": context,
        "session": session,
        "dialogue_mode": mode_id,
        "response": response_text,
        "skills_context": selected_skills,
        "llm": llm_payload,
        "llm_route_event": route_event,
        "memory_paths": {
            "session": session_path,
            "daily": daily_path,
        },
    }
