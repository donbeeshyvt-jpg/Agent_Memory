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
from agent_memory.security.scanner import scan_incoming_user_text
from agent_memory.transport_profiles import load_transport_profiles, resolve_transport_profile
from agent_memory.types import MemoryType
from agent_memory.vault import ObsidianVaultAdapter
from agent_memory.vault.obsidian import read_brain_type


@dataclass(slots=True)
class InboundTurn:
    transport: str
    channel_id: str
    user_id: str
    message: str
    context: str
    session: str
    # V3-O.6 #4+#5: Discord display_name 從 relay payload 帶上來
    #   #4 owner turn → companion_chat_runtime 自學進 .ai/owner_aliases.json
    #   #5 split-by-display-name 開時, user_id 已是 synth (e.g. "ai-viewer-tako_yaki_8"),
    #      display_name 就是 prefix 本身, 給下游知道
    display_name: str = ""


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
    # V3-O.6 #4+#5: display_name 從 relay 撈 (relay 已 inject author.display_name)
    display_name = _first_text(
        payload,
        list(profile.get("display_name_candidates", ["author.display_name", "user.display_name"])),
    )
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
        display_name=display_name,
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


def _map_companion_channel_type(transport: str, payload: dict[str, Any]) -> str:
    """V3-D-DC1: transport → companion channel_type 4 種對映."""
    t = (transport or "").lower()
    if t in ("cli", "repl"):
        return "cli"
    # discord DM 看 payload 是否有 guild_id (DM 沒)
    if t == "discord":
        guild_id = payload.get("guild_id") or _get_by_path(payload, "context.guild_id")
        if not guild_id:
            return "dm"
        # 直播 channel? 假設 channel_type 從 payload "channel_kind" 或預設 public_text_channel
        kind = payload.get("channel_kind") or _get_by_path(payload, "context.channel_kind")
        if kind in ("public_stream", "stream", "live"):
            return "public_stream"
        return "public_text_channel"
    return "public_text_channel"


def _check_is_owner(vault_root: Path, user_id: str, transport: str = "") -> bool:
    """owner 判定: companion_config.yaml (優先) → DB owner_state (fallback).

    yaml 可直接編輯生效; DB 是 setup_companion_vault.py 寫入的 legacy 備援.
    """
    if not user_id:
        return False
    # 1. companion_config.yaml — 各平台 owner user_id
    try:
        from agent_memory.companion.companion_config import get_owner_user_id_for_transport
        yaml_owner_id = get_owner_user_id_for_transport(vault_root, transport)
        if yaml_owner_id and yaml_owner_id == user_id:
            return True
    except Exception:
        pass
    # 2. DB owner_state fallback (setup_companion_vault.py 寫入的)
    try:
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(vault_root) as conn:
            row = conn.execute(
                "SELECT owner_user_id FROM owner_state LIMIT 1"
            ).fetchone()
        if row and row["owner_user_id"] == user_id:
            return True
    except Exception:
        pass
    return False


def _append_companion_session_log(vault_root: Path, user_id: str, message: str, response_text: str, channel_type: str) -> None:
    """V3-D-DC1: 對齊 §5.1 10_Working_Memory/11_Session_Logs/ markdown append (Phase 1 minimal)."""
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    log_dir = vault_root / "10_Working_Memory" / "11_Session_Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"live-{day}_{channel_type}.md"
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"\n## {ts} {user_id} ({channel_type})\n\n**user**: {message}\n\n**bot**: {response_text}\n"
    if not path.exists():
        path.write_text(f"---\ntype: session_log\nschema_version: 10\nday: {day}\nchannel_type: {channel_type}\n---\n\n# Live Session — {day}\n", encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _handle_hermes_ingest(vault_root: Path, turn: "InboundTurn") -> dict[str, Any]:
    """hermes 送來的資料 → 直接寫 40_Knowledge_Base/_ingest_inbox, 跳過 22-step.

    不計入親密度/情緒. curator L4 7d 會自動 LLM 摘要此 inbox.
    """
    from datetime import datetime, timezone
    from agent_memory.companion.knowledge_base import INGEST_INBOX_DIR
    from agent_memory.security.atomic import atomic_write

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"hermes_{ts}_{(turn.user_id or 'unknown')[:12]}"
    inbox_dir = vault_root / INGEST_INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"{filename}.md"
    content = (
        "---\n"
        "source: hermes\n"
        f"user_id: {turn.user_id}\n"
        f"transport: {turn.transport}\n"
        f"received_at: {datetime.now(timezone.utc).isoformat()}\n"
        "---\n\n"
        f"{turn.message}\n"
    )
    ack = "(文獻已收到，排入知識整理佇列)"
    try:
        atomic_write(path, content)
    except Exception as exc:
        ack = f"(收取失敗: {type(exc).__name__})"

    return {
        "transport": turn.transport,
        "channel_id": turn.channel_id,
        "user_id": turn.user_id,
        "response": ack,
        "brain_type": "companion",
        "persona": "companion",
        "is_hermes": True,
        "routed_to": str(path),
        "degraded": False,
    }


def _get_aggregator_for(vault_root: Path):
    """V3-O.11 階段2: 從 companion_config.yaml 讀彙整門檻, 取 StreamAggregator 單例。"""
    from agent_memory.companion.stream_aggregator import get_stream_aggregator
    # V3-O.11+ user 2026-06-02: quiet 6→7 給連發更多空間, hard_cap 30→28 控制體感上限
    qw, thr, mx, cap = 7.0, 5, 10, 28.0
    try:
        import yaml as _y
        p = vault_root / "00_System_Core" / "companion_config.yaml"
        if p.exists():
            cc = _y.safe_load(p.read_text(encoding="utf-8")) or {}
            sm = (((cc.get("channels", {}) or {}).get("discord", {}) or {}).get("stream_mode", {}) or {})
            agg_cfg = sm.get("aggregation", {}) or {}
            qw = float(agg_cfg.get("quiet_window_s", 6.0))
            thr = int(agg_cfg.get("meaningful_flush_threshold", 5))
            mx = int(agg_cfg.get("max_meaningful", 10))
            cap = float(agg_cfg.get("hard_cap_s", 30.0))
    except Exception:
        pass
    return get_stream_aggregator(
        vault_root, quiet_window_s=qw, meaningful_flush_threshold=thr,
        max_meaningful=mx, hard_cap_s=cap,
    )


def _generate_aggregated_reply(vault_root: Path, msgs: list, session_id: str) -> str:
    """⚠️ DEPRECATED (V3-O.11+ user 2026-06-02 統一路徑 C 廢除): 改用 _check_and_flush_aggregator
    內直接跑 run_companion_chat_turn 完整 22-step. 此函數短 prompt (~130 tok, 沒 SOUL/ROLE LOCK)
    導致 bot 自由發揮戲劇腔. 保留 placeholder 避免外部 import 破裂, 內部直接 return "".

    舊版設計留存供參考 (line 後續為原邏輯, 不會被呼叫)。
    """
    return ""  # ⚠️ DEPRECATED, 不再呼叫
    if not msgs:
        return ""
    lines = [f"{m.display_name}: {(m.content or '')[:200]}" for m in msgs]
    block = "\n".join(lines)
    cards = []
    for uid in dict.fromkeys(m.user_id for m in msgs):
        try:
            from agent_memory.companion.audience_writer import load_viewer_profile_md
            c = load_viewer_profile_md(vault_root, uid)
            if c:
                cards.append(c[:1500])
        except Exception:
            pass
    cards_block = "\n\n".join(cards) if cards else "(無朋友卡, 多為初識)"
    prompt = (
        "這是一場多人對話。以下是各位發言者的朋友卡, 以及他們剛剛說的話。\n\n"
        f"=== 發言者朋友卡 ===\n{cards_block}\n\n"
        f"=== 本批發言 (按時序) ===\n{block}\n\n"
        "請用你自己的方式統一回應這批訊息: 依每個人的話 + 你跟他的關係重要性, "
        "自主選擇『綜合回答』或『稍微個別回答』。直接對全場說話, 不必逐一點名所有人。"
    )
    try:
        from agent_memory.llm_text_helpers import call_llm_for_text
        # auxiliary=None → 走 global_default (main_chat 出口模型, 預設 deepseek)
        result = call_llm_for_text(
            vault_root, prompt, persona_id="companion",
            temperature=0.7, timeout_s=60.0, auxiliary=None,
        )
        return (result or "").strip()
    except Exception:
        return ""


def _check_and_flush_aggregator(vault_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """V3-O.11+ user 2026-06-02 統一路徑 C: 列隊出口走 run_companion_chat_turn 完整 22-step.

    舊版走 `_generate_aggregated_reply` 短 prompt (~130 tok, 沒 SOUL/ROLE LOCK/memory/dialogue),
    導致 deepseek 自由發揮戲劇腔/簡體字/旁白. 現改成統一過 22-step pipeline:
      - batch 內每則訊息進來時已 record_only=True 跑 22-step 算 state 寫 db (state 累積在 db)
      - flush 時用「最後一筆 (優先 owner)」當代表, 構造 batch_msg 含整批訊息
      - 跑 record_only=False → 走 _build_companion_system_prompt 完整 12 段 packet
        (含 SOUL/ROLE LOCK/memory/dialogue/朋友卡/owner_profile)
      - bot 看到所有 context, 不再「白紙化」自由發揮
    """
    base = {"brain_type": "companion", "persona": "companion", "aggregated": False, "response": ""}
    try:
        _agg = _get_aggregator_for(vault_root)
        _channel_id = str(payload.get("channel_id") or "default")
        # V3-O.11+ user 2026-06-02: force_flush 跳過 should_flush window check
        # (給 @bot in-turn 立刻 flush — bot @ 進來時連帶 queue 內所有訊息一起 batch 回應, 不再分兩條獨立 path)
        _force = bool(payload.get("_force_flush", False))
        if not _force and not _agg.should_flush(_channel_id):
            return base
        msgs = _agg.drain(_channel_id)
        if not msgs:
            return base
        session_id = str(payload.get("session_id") or "aggregated")
        channel_type = str(payload.get("channel_type") or "public_text_channel")
        transport = str(payload.get("transport") or "discord")

        # 構造 batch user_message: 列出所有 N 則訊息 with (主人/觀眾) tag
        batch_lines: list[str] = []
        last_owner_uid = ""
        last_owner_display = ""
        for i, m in enumerate(msgs, 1):
            who = (m.display_name or m.user_id or "?")[:20]
            is_owner_m = _check_is_owner(vault_root, m.user_id, transport=transport)
            tag = "(主人)" if is_owner_m else "(觀眾)"
            batch_lines.append(f"  [{i}] {who} {tag}: {(m.content or '')[:200]}")
            if is_owner_m:
                last_owner_uid = m.user_id
                last_owner_display = m.display_name or ""

        batch_msg = (
            f"[本輪列隊彙整 — 共 {len(msgs)} 則訊息]\n"
            + "\n".join(batch_lines) + "\n\n"
            + "請統一回應這批訊息。"
        )

        # 用最後一筆 owner (有就優先) 當代表, 否則用最後一筆訊息
        rep_uid = last_owner_uid or msgs[-1].user_id
        rep_display = last_owner_display or (msgs[-1].display_name or "")
        is_owner = bool(last_owner_uid)

        # 構造 ChatRequest 跑完整 22-step (含 step15 LLM 出口 + 完整 12 段 packet)
        from agent_memory.companion.companion_chat_runtime import (
            run_companion_chat_turn, ChatRequest, append_bot_reply_event,
        )
        _bl: dict = {}
        try:
            from agent_memory.companion.personality_switcher import get_current_baselines
            _bl = get_current_baselines(vault_root) or {}
        except Exception:
            _bl = {}

        req = ChatRequest(
            user_id=str(rep_uid),
            session_id=session_id,
            channel_id=str(payload.get("channel_id") or ""),
            channel_type=channel_type,
            message=batch_msg,
            is_owner=is_owner,
            display_name=rep_display,
        )
        resp = run_companion_chat_turn(
            req, vault_root,
            record_only=False,  # 彙整出口 = 真正跑 LLM
            persona_baseline_balance=float(_bl.get("baseline_balance", 0.3)),
            persona_baseline_silence=float(_bl.get("baseline_silence_intolerance", 0.5)),
            persona_baseline_curiosity=float(_bl.get("curiosity_urge", 0.5)),
            persona_baseline_topic=float(_bl.get("topic_drive", 0.5)),
            persona_baseline_engagement=float(_bl.get("engagement_seeking", 0.5)),
        )
        unified = (resp.response_text or "").strip()

        if unified:
            try:
                # V3-O.11+ user 2026-06-02 dedup fix: rep_uid 的 bot reply 已由
                # run_companion_chat_turn step17_memory_write_db 寫進去, 這裡只給「其他 user」
                # (多 user 場景 viewer A/B 也要看到 bot 回他們的話) 補寫, 避免雙寫.
                user_ids = list(dict.fromkeys(m.user_id for m in msgs))
                extra_ids = [u for u in user_ids if str(u) != str(rep_uid)]
                if extra_ids:
                    append_bot_reply_event(vault_root, extra_ids, session_id, unified, channel_type=channel_type)
            except Exception:
                pass
            # BUG-2 fix: 寫 markdown session log (owner 下個 turn 讀歷史能看到彙整回覆)
            try:
                msgs_concat = " ｜ ".join(
                    f"{(m.display_name or m.user_id or '?')[:20]}: {(m.content or '')[:80]}"
                    for m in msgs
                )
                _append_companion_session_log(
                    vault_root, "彙整", msgs_concat, unified, channel_type,
                )
            except Exception:
                pass
        return {"brain_type": "companion", "persona": "companion", "aggregated": True,
                "response": unified, "channel_id": payload.get("channel_id", ""),
                "speaker_count": len(set(m.user_id for m in msgs))}
    except Exception as _e:
        return {"brain_type": "companion", "persona": "companion", "aggregated": False,
                "response": "", "error": str(_e)[:120]}


def _run_companion_transport_event(
    *, vault_root: Path, transport: str, payload: dict[str, Any],
    explicit_persona: str | None = None,
    context_override: str | None = None, session_override: str | None = None,
    allow_llm_degraded: bool = False,
) -> dict[str, Any]:
    """V3-D-DC1: companion brain_type 走 V3 22-step pipeline.

    對齊 V3 §3.4 strategy pattern + §4.1 Mode A standalone.
    Phase 1 stub LLM 內建在 companion_chat_runtime, 不接 LLMClient.
    """
    from agent_memory.companion.companion_chat_runtime import (
        run_companion_chat_turn, ChatRequest,
    )

    # V3-O.11 階段2: relay 背景 task 送來的「彙整 flush 檢查」(非 user 訊息) → 檢查+生成統一回覆
    if payload.get("_aggregator_flush_check"):
        return _check_and_flush_aggregator(vault_root, payload)

    profiles = load_transport_profiles(vault_root)
    turn = parse_inbound_turn(transport, payload, profiles)
    injection_scan = scan_incoming_user_text(turn.message)

    # hermes 送來 → 直接進 40_Knowledge_Base, 不走聊天管道
    try:
        from agent_memory.companion.companion_config import is_hermes_sender
        if is_hermes_sender(vault_root, turn.user_id):
            return _handle_hermes_ingest(vault_root, turn)
    except Exception:
        pass

    is_owner = _check_is_owner(vault_root, turn.user_id, transport=turn.transport)
    channel_type = _map_companion_channel_type(turn.transport, payload)

    req = ChatRequest(
        user_id=turn.user_id or "anonymous",
        session_id=session_override or turn.session or f"{turn.transport}-{turn.channel_id}",
        channel_id=turn.channel_id or "default",
        channel_type=channel_type,
        message=turn.message,
        is_owner=is_owner,
        concurrent_viewers=int(payload.get("concurrent_viewers", 0) or 0),
        idle_seconds=float(payload.get("idle_seconds", 0.0) or 0.0),
        chat_velocity=float(payload.get("chat_velocity", 0.5) or 0.5),
        display_name=turn.display_name or "",
    )

    # V3-O.10 ISSUE-3: 傳 effective baseline (含 dynamic_baseline_overlay 演化) 進 pipeline.
    # 不傳的話 run_companion_chat_turn 用 default 0.3/0.5, runtime modifier 算
    # (silence>0.6→"不想冷場") 用死值, overlay 演化對 modifier 無效.
    _bl: dict = {}
    try:
        from agent_memory.companion.personality_switcher import get_current_baselines
        _bl = get_current_baselines(vault_root) or {}
    except Exception:
        _bl = {}

    # V3-O.11+ (user 2026-06-02): 全員預設進佇列統一彙整(含 owner); 唯一例外 = 被 @mention 才即時單回
    # ⚠️ 2026-06-02 修法 B revert: @bot 進 queue + force_flush 邏輯讓 sub_task 跑 2x 加速 llama-cpp-python
    # CUDA pool corruption (連發 2 句就卡). 回到原邏輯: @bot 即時出口, 沒 @ 進 queue.
    is_mention = bool(payload.get("is_mention", False))
    resp = run_companion_chat_turn(
        req, vault_root,
        record_only=(not is_mention),
        persona_baseline_balance=float(_bl.get("baseline_balance", 0.3)),
        persona_baseline_silence=float(_bl.get("baseline_silence_intolerance", 0.5)),
        persona_baseline_curiosity=float(_bl.get("curiosity_urge", 0.5)),
        persona_baseline_topic=float(_bl.get("topic_drive", 0.5)),
        persona_baseline_engagement=float(_bl.get("engagement_seeking", 0.5)),
    )

    # 不 @ 進 aggregator queue, 等 background polling flush
    aggregation_held = False
    if not is_mention:
        try:
            _agg = _get_aggregator_for(vault_root)
            _agg.add_message(req.channel_id, req.user_id, turn.display_name or "", req.message)
            aggregation_held = True
        except Exception:
            aggregation_held = False
        resp.response_text = ""  # held, 等彙整 flush 統一發頻道

    # 寫 markdown session log (companion 10_Working_Memory/11_Session_Logs/)
    # V3-O.11+ BUG-2 fix (user 2026-06-02): held 時不寫(等彙整 flush 統一寫),
    # 否則 normal turn 寫進 bot="" 空白行 + 彙整另寫一行 → owner 讀到「有問無答」
    if not aggregation_held:
        try:
            _append_companion_session_log(
                vault_root, req.user_id, req.message, resp.response_text, channel_type,
            )
        except Exception:
            pass  # 不破整個 transport

    result: dict[str, Any] = {
        "transport": turn.transport,
        "channel_id": turn.channel_id,
        "user_id": turn.user_id,
        "persona": "companion",
        "brain_type": "companion",
        "response": resp.response_text,
        "aggregation_held": aggregation_held,
        "decision": resp.decision,
        "affect_state": resp.affect_state,
        "emotion_state": resp.emotion_state,
        "balance_state": resp.balance_state,
        "intimacy": resp.intimacy,
        "og_blocked": resp.og_blocked,
        "og_rule_triggered": resp.og_rule_triggered,
        "scanner_hits_count": resp.scanner_hits_count,
        "injection_risk": resp.injection_risk,
        "pipeline_steps_done": resp.pipeline_steps_done,
        "trace_id": resp.trace_id,
        "channel_type": channel_type,
        "is_owner": is_owner,
        "degraded": False,
    }
    if injection_scan.get("detected"):
        result["security_scan"] = injection_scan
    return result


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

    # V3-D-DC1 brain_type dispatcher (對齊 V3 §3.4 strategy pattern)
    # companion vault → 走 V3 22-step pipeline; steward → 既有 V2 chat_runtime
    brain_type = read_brain_type(root)
    if brain_type == "companion":
        return _run_companion_transport_event(
            vault_root=root, transport=transport, payload=payload,
            explicit_persona=explicit_persona,
            context_override=context_override, session_override=session_override,
            allow_llm_degraded=allow_llm_degraded,
        )

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

    # Phase A C5: 掃 incoming user text 找 indirect prompt injection.
    # 不 block 對話, 只 log + 在 response footer 加警示 (避免假陽性影響使用者體驗).
    injection_scan = scan_incoming_user_text(turn.message)

    # Phase A C4 (A.6): 下載 + extract Discord attachments, prepend 到 turn.message
    # 用 <attachment> XML 標籤防 prompt injection (對齊 C5 的 <context> 包裝設計)
    attachment_ingest_results: list[dict[str, Any]] = []
    raw_attachments = payload.get("attachments") if isinstance(payload, dict) else None
    if isinstance(raw_attachments, list) and raw_attachments:
        try:
            from agent_memory.attachment_ingest import ingest_attachments_for_turn
            # 判斷當前模型是否 vision-capable (粗略: gemini-2.5* / 含 vision 字樣)
            model_hint = (override_model or "").lower()
            vision_capable = ("gemini" in model_hint) or ("vision" in model_hint)
            xml_blocks, ingest_results = ingest_attachments_for_turn(
                attachments=raw_attachments,
                vault_root=root,
                channel_id=turn.channel_id,
                vision_capable=vision_capable,
            )
            attachment_ingest_results = ingest_results
            if xml_blocks:
                # 把附件 XML block prepend 到 user message
                # 同時把使用者原文用 <user_message> 包起來 (防 prompt injection: 區分附件資料 vs 使用者指令)
                augmented = (
                    xml_blocks
                    + "\n\n<user_message>\n"
                    + turn.message
                    + "\n</user_message>"
                )
                turn.message = augmented
        except Exception:  # noqa: BLE001
            # attachment 失敗不阻擋主流程, 只記錄
            attachment_ingest_results = [{"ok": False, "note": "attachment pipeline exception", "kind": "error"}]

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
                    # R19 P1-b C92 + R19.2 C100: 預切從 8000 → 32768 (4 倍).
                    # Codex 第 32 輪 5 persona × 30 turn shared_channel log ~90000 chars,
                    # 8000 只 cover 最後 ~13 turn 把 Turn 1/2 head 切走 → _two_sided_excerpt
                    # 找不到 head turn marker fallback 退回單純末尾切片 (has_t1/has_t2=false).
                    # 32768 chars 足以容納 30 turn × 5 persona, head 2 turn 仍在原料內,
                    # 真正注入 prompt 仍由 chat_runtime 切到 SHARED_HISTORY_CAP=3000.
                    # 階層式 LLM 摘要更聰明做法留 R20+/V3.
                    shared_channel_history = text[-32768:] if len(text) > 32768 else text
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
        if attachment_ingest_results:
            result["attachment_ingest"] = [
                {k: v for k, v in r.items() if k != "text"}  # 不回 raw text, 只回 metadata
                for r in attachment_ingest_results
            ]
        # Phase A C5: 注入掃描結果回報
        if injection_scan.get("detected"):
            result["security_scan"] = injection_scan
            warn = "\n\n---\n⚠ [security scan] 偵測到可疑輸入 pattern: " + "; ".join(injection_scan.get("reasons", []))
            warn += "\n     管家照常回覆但已記錄. 若是無心使用語句可忽略此警示."
            result["response"] = str(result.get("response", "")) + warn
        # Phase A C15: auto_evolve 已在 run_chat_turn 內處理 (避免 double-count counter)
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
