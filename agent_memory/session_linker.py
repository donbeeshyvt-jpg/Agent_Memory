"""Cross-channel session linker — 主動回想跨入口連續性 (R9 C31).

對應 MISSION §3.1 對話驅動 + 使用者 2026-05-17 釐清「不論透過哪個聊天窗都是跟這個
agent 繼續說話他都要懂」.

解決問題:
- 早上 Discord channel A 聊 → 中午 CLI menu [6] 切換 → 下午 Discord channel B
- daily_flush 還沒 compact (5-30 分鐘內對話) 切窗會「失憶」
- 因為 chat_runtime 只 load 當前 session_log + frozen snapshot + RAG retrieve
- RAG retrieve 不會撈到「剛剛 5 分鐘前在別 channel」的 session_log

解法:
- chat_runtime 開頭 call collect_recent_cross_session_context(adapter, persona)
- 掃同 persona 最近 N 分鐘 (預設 30) 內所有 session_log 檔
- 各取 tail 200 字串接成「跨入口近期上下文」段
- 加進 system_prompt 給 LLM 看

設計考量:
- 同 persona 限制: coder 不該看到 steward 的對話 (privacy / persona 邊界)
- 30 分鐘視窗: 平衡「即時連續」vs「不要太多舊資料污染」
- max total 2400 字: 避免 prompt 爆 (對齊 _tail_excerpt 慣例)
- 純程式: 不依賴 LLM (對齊 MISSION §3.4 省 token)
- 排除當前 session: 避免重複載入
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agent_memory.vault import ObsidianVaultAdapter

SESSION_LOG_DIR_RELATIVE = "70_Active_Plans/Session_Logs"
DEFAULT_RECENT_MINUTES = 30
DEFAULT_MAX_TOTAL_CHARS = 2400
DEFAULT_PER_SESSION_TAIL_CHARS = 400


def _persona_id_from_filename(filename: str) -> str:
    """從 session_log 檔名抽 persona_id.

    命名規則: `<persona>__<context>__<session_id>.md`
    """
    stem = filename.removesuffix(".md")
    parts = stem.split("__", 1)
    return parts[0] if parts else ""


def _tail_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


def collect_recent_cross_session_context(
    vault_root: Path,
    *,
    persona_id: str,
    current_session_id: str = "",
    recent_minutes: int = DEFAULT_RECENT_MINUTES,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    per_session_tail_chars: int = DEFAULT_PER_SESSION_TAIL_CHARS,
) -> dict[str, str | list[str]]:
    """掃同 persona 最近 N 分鐘所有 session_log → 串接 tail 給 chat_runtime.

    Args:
        vault_root: vault 根
        persona_id: 當前 persona (只看同 persona 避免越界)
        current_session_id: 當前 session_id (要排除避免重複載入; 空字串=不排除)
        recent_minutes: 視窗 (預設 30 分鐘)
        max_total_chars: 總字數上限 (預設 2400)
        per_session_tail_chars: 每個 session 取 tail 字數 (預設 400)

    Returns:
        {
            "text_block": "## cross-session 1\n...\n## cross-session 2\n...",
            "session_paths": ["70_Active_Plans/Session_Logs/.../X__a__b.md", ...],
            "persona_id": persona_id,
            "recent_minutes": recent_minutes,
        }
        text_block 為空表示沒近期跨 session 內容.
    """

    root = Path(vault_root).expanduser().resolve()
    session_root = root / SESSION_LOG_DIR_RELATIVE
    if not session_root.exists():
        return {"text_block": "", "session_paths": [], "persona_id": persona_id, "recent_minutes": recent_minutes}

    cutoff = datetime.now().astimezone() - timedelta(minutes=recent_minutes)
    # session_log 結構: Session_Logs/<date>/<persona>__<context>__<session>.md
    candidates: list[tuple[Path, datetime]] = []
    for date_dir in session_root.iterdir():
        if not date_dir.is_dir():
            continue
        for log_path in date_dir.glob("*.md"):
            if log_path.name.startswith("_"):
                continue
            # persona filter
            persona_in_file = _persona_id_from_filename(log_path.name)
            if persona_in_file != persona_id:
                continue
            # 排除當前 session
            if current_session_id and current_session_id in log_path.name:
                continue
            # mtime 過濾 cutoff
            try:
                mtime = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone()
            except OSError:
                continue
            if mtime < cutoff:
                continue
            candidates.append((log_path, mtime))

    if not candidates:
        return {"text_block": "", "session_paths": [], "persona_id": persona_id, "recent_minutes": recent_minutes}

    # 按 mtime 降序 (最新的優先)
    candidates.sort(key=lambda x: x[1], reverse=True)

    adapter = ObsidianVaultAdapter(root)
    parts: list[str] = []
    session_paths: list[str] = []
    total_chars = 0
    header = f"以下是同 persona ({persona_id}) 最近 {recent_minutes} 分鐘內的跨入口對話片段，供你延續上下文："
    parts.append(header)
    total_chars += len(header)

    for log_path, mtime in candidates:
        rel = str(log_path.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        tail = _tail_chars(note.body, per_session_tail_chars)
        # 抽 context 段 (檔名中段, 例 `steward__discord-channel-123__sess-456.md` → discord-channel-123)
        stem = log_path.stem
        context_part = stem.split("__")[1] if stem.count("__") >= 2 else "?"
        block = f"\n[{mtime.strftime('%H:%M')}] context=`{context_part}`:\n{tail}\n"

        if total_chars + len(block) > max_total_chars:
            break
        parts.append(block)
        session_paths.append(rel)
        total_chars += len(block)

    if len(session_paths) == 0:
        return {"text_block": "", "session_paths": [], "persona_id": persona_id, "recent_minutes": recent_minutes}

    return {
        "text_block": "\n".join(parts),
        "session_paths": session_paths,
        "persona_id": persona_id,
        "recent_minutes": recent_minutes,
    }
