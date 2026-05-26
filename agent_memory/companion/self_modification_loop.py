"""V3 C11b Self-Modification Loop — 夥伴自寫 00.07/00.08.

對齊 V3 §12 + D-V3-26 + D-V3-50 (char_limit 4000/2000) + hermes flush_min_turns.

每 N turn (channel-aware) 觸發 self_reflection:
- 抓近 N turn raw_events + 對話總結
- 對 self_memory: 「這 N turn 我學到什麼 about myself?」→ append 00.07_Companion_MEMORY.md
- 對 owner_profile: 「主人在這 N turn 表現什麼偏好/情緒?」→ append 00.08_Owner_Profile.md
- char_limit 達標 → LLM 壓縮 (Phase 1 stub: 純截尾保 head + tail)

Drift Guard (§12.4):
- injection_risk=high → 該 turn 不寫
- identity_relevance>0.75 → SOUL 候選不直接 active
- char_limit 壓縮必保留: 紅線 / safety_rules / owner_user_id / 極端情緒

Channel-aware (§12.3, D-V3-50/D21-V3):
- public_stream: flush=30, char_limit (MEM/OWNER)=4000/2000
- public_text_channel: 6, 2200/1375
- dm: 10, 3000/1800
- cli: 不限
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.security.atomic import atomic_write


_CHANNEL_FLUSH_MIN_TURNS = {
    "public_stream": 30,
    "public_text_channel": 6,
    "dm": 10,
    "cli": 10**9,  # cli 實質「不限」(對齊 D21-V3 拍板)
    "normal": 10,
}

_CHANNEL_CHAR_LIMITS = {
    # (memory_char_limit, owner_profile_char_limit)
    "public_stream": (4000, 2000),
    "public_text_channel": (2200, 1375),
    "dm": (3000, 1800),
    "cli": (9999999, 9999999),
    "normal": (3000, 1800),
}

# 紅線 — 壓縮時必保留 (在檔內以這些 prefix 出現的段)
_PRESERVE_PREFIXES = (
    "## 紅線", "## Safety Rules", "## Hard Rules",
    "primary_owner_user_id:", "schema_version:",
)


@dataclass(slots=True)
class FlushDecision:
    should_flush: bool = False
    reason: str = ""
    flush_min_turns: int = 6


def should_flush(turn_count: int, channel_type: str) -> FlushDecision:
    """V3 §12.3 + D21-V3: channel-aware flush 判定."""
    min_t = _CHANNEL_FLUSH_MIN_TURNS.get(channel_type, 10)
    if turn_count >= min_t:
        return FlushDecision(should_flush=True, reason=f"turn_count {turn_count} >= min {min_t}", flush_min_turns=min_t)
    return FlushDecision(should_flush=False, reason=f"not yet ({turn_count}/{min_t})", flush_min_turns=min_t)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_section(file_path: Path, new_section: str) -> None:
    """Atomic append to .md file (no read-modify-write race)."""
    if not file_path.exists():
        return
    existing = file_path.read_text(encoding="utf-8")
    new_content = existing.rstrip() + "\n\n" + new_section + "\n"
    atomic_write(file_path, new_content)


def _backup_file(file_path: Path, archive_dir: Path, *, keep: int = 5) -> None:
    """V3 §12.4: backup 上一版到 99_Archive/auto_archived/companion_memory_backup/ (keep=5 對齊 hermes)."""
    if not file_path.exists():
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"{file_path.stem}_{timestamp}{file_path.suffix}"
    shutil.copy2(file_path, archive_dir / backup_name)
    # 保留最近 N 份
    backups = sorted(archive_dir.glob(f"{file_path.stem}_*{file_path.suffix}"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except Exception:
            pass


def _enforce_char_limit_compress(file_path: Path, limit: int) -> bool:
    """V3 §12.4 + D-V3-50: 超過 char_limit 時壓縮舊段, 保留紅線/safety/owner_id 行.

    Phase 1 MVP 策略 (Phase 3 改 LLM 壓縮):
    - 抓 frontmatter (--- ... ---)
    - 抓所有以 _PRESERVE_PREFIXES 開頭的段
    - 保 frontmatter + preserved + 最後 limit/2 char 的內容
    - 中間漏掉的部分加「<-- 已壓縮 N char old -->」標記

    Returns: True 若有壓縮, False 沒.
    """
    if not file_path.exists():
        return False
    text = file_path.read_text(encoding="utf-8")
    if len(text) <= limit:
        return False

    # 抓 frontmatter
    front = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            front = text[: end + 5]
            body = text[end + 5 :]

    # 抓 preserved sections (簡單 line-level)
    preserved_lines: list[str] = []
    for line in body.split("\n"):
        if any(line.startswith(p) for p in _PRESERVE_PREFIXES):
            preserved_lines.append(line)

    # 保 tail (限後半 limit/2)
    tail_budget = max(200, limit // 2)
    tail = body[-tail_budget:]

    compressed_body = (
        ("\n".join(preserved_lines) + "\n\n" if preserved_lines else "")
        + f"<!-- 已壓縮: 舊段約 {len(body) - len(tail) - sum(len(l) for l in preserved_lines)} char -->\n"
        + tail
    )
    new_text = front + compressed_body
    atomic_write(file_path, new_text)
    return True


def _llm_enabled_for_flush() -> bool:
    """V3-E1 Bug 6: 判斷 self-mod flush 是否走 LLM 整理.

    - env AGENT_MEMORY_COMPANION_LLM_FORCE_STUB=1 → stub (壓測用)
    - 無 API key → stub
    - 都有 → LLM 整理
    """
    if os.getenv("AGENT_MEMORY_COMPANION_LLM_FORCE_STUB", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    return any(os.getenv(k, "").strip() for k in (
        "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
    ))


def _load_recent_raw_events(
    vault_root: Path, user_id: str, session_id: str, *, limit: int = 20,
) -> list[dict]:
    """V3-E1 Bug 6+7: 撈該 user_id+session 最近 raw_events (含 user+bot) 給 LLM 整理用."""
    try:
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT actor, content, injection_risk, created_at FROM raw_events "
                "WHERE user_id=? AND session_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, session_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def _llm_summarize_self_memory(
    vault_root: Path, user_id: str, session_id: str, existing_tail: str,
) -> str:
    """V3-E1 Bug 6: 用 LLM 把近 N raw_events 整理成「我學到了什麼」.

    對齊 V3 §12 + hermes MEMORY.md self-reflection 概念.
    """
    from agent_memory.llm_text_helpers import call_llm_for_text

    raw_turns = _load_recent_raw_events(vault_root, user_id, session_id, limit=20)
    if not raw_turns:
        raise RuntimeError("no raw_events to summarize")
    raw_block = "\n".join(
        f"  [{r['actor']}] {r['content'][:140]}" for r in raw_turns
    )
    prompt = (
        "你是 V3 夥伴大腦 (像會成長的孩子, 不是 AI 助手).\n"
        "請整理你剛剛的對話成「我學到了什麼」自我反思 note. 第一人稱「我」, "
        "3-5 條 markdown bullet, 簡短具體, 不要流水帳.\n\n"
        "重點:\n"
        "- 我學到 about 自己 (情緒 / 邊界 / 反應 pattern)\n"
        "- 觀眾或 owner 教了我什麼\n"
        "- 哪些情境我下次要注意\n\n"
        f"最近互動:\n{raw_block}\n\n"
        f"既有筆記末段 (避免重複):\n{existing_tail[-500:] if existing_tail else '(無)'}\n\n"
        "請直接輸出 markdown bullet, 不要前後說明."
    )
    return call_llm_for_text(
        vault_root, prompt, persona_id="companion",
        temperature=0.4, timeout_s=30.0,
        auxiliary="companion_self_reflection",
    )


def _llm_summarize_owner_profile(
    vault_root: Path, user_id: str, session_id: str, existing_tail: str,
) -> str:
    """V3-E1 Bug 7: 用 LLM 把 owner 近 N raw_events 整理成「主人偏好觀察」.

    對齊 V3 §12 + hermes USER.md.
    """
    from agent_memory.llm_text_helpers import call_llm_for_text

    raw_turns = _load_recent_raw_events(vault_root, user_id, session_id, limit=20)
    owner_msgs = [r for r in raw_turns if r["actor"] == "user"]
    if not owner_msgs:
        raise RuntimeError("no owner messages to summarize")
    raw_block = "\n".join(f"  {r['content'][:200]}" for r in owner_msgs)
    prompt = (
        "你是 V3 夥伴大腦. 整理你對「主人/中之人」的觀察成 profile.\n"
        "對齊 hermes USER.md 風格: 不是流水帳, 是歸納主人的偏好 / 雷點 / 對話風格 / 關係定位.\n\n"
        "請用 markdown 寫 3-5 條觀察 bullet, 第三人稱「主人」, 簡短具體.\n"
        "重點:\n"
        "- 主人的對話風格 (短/長 / 語氣 / 用詞)\n"
        "- 主人提到的偏好 (喜歡什麼 / 雷什麼)\n"
        "- 主人對我的關係定位 (爸爸 / 創造者 / 老師 / ...)\n"
        "- 我下次該怎麼跟主人互動\n\n"
        f"主人最近說的話:\n{raw_block}\n\n"
        f"既有 profile 末段:\n{existing_tail[-500:] if existing_tail else '(無)'}\n\n"
        "請直接輸出 markdown bullet, 不要前後說明."
    )
    return call_llm_for_text(
        vault_root, prompt, persona_id="companion",
        temperature=0.4, timeout_s=30.0,
        auxiliary="companion_owner_profile",
    )


def flush_self_memory(
    vault_root: Path,
    *,
    recent_turn_summaries: list[str],
    channel_type: str = "normal",
    injection_risk: str = "low",
    identity_relevance: float = 0.0,
    user_id: str = "",
    session_id: str = "",
) -> dict:
    """V3 §12.3: 主入口 — 把 recent turn 整理 append 到 00.07_Companion_MEMORY.md.

    Drift Guard:
    - injection_risk=high → skip (return reason)
    - identity_relevance>0.75 → 改寫到候選 (Phase 1 跳過, 留 Phase 3)
    """
    if injection_risk == "high":
        return {"flushed": False, "reason": "injection_risk=high (D drift guard skip)"}

    char_limit_mem, _ = _CHANNEL_CHAR_LIMITS.get(channel_type, _CHANNEL_CHAR_LIMITS["normal"])
    memory_path = vault_root / "00_System_Core" / "00.07_Companion_MEMORY.md"
    archive_dir = vault_root / "99_Archive" / "auto_archived" / "companion_memory_backup"

    if not memory_path.exists():
        return {"flushed": False, "reason": "00.07 不存在 (需先 bootstrap)"}

    # backup 前版
    _backup_file(memory_path, archive_dir, keep=5)

    # V3-E1 Bug 6: 優先試 LLM 整理, fail fallback raw append
    section = None
    llm_used = False
    if _llm_enabled_for_flush() and user_id and session_id:
        try:
            existing = memory_path.read_text(encoding="utf-8")
            summary = _llm_summarize_self_memory(vault_root, user_id, session_id, existing)
            if summary.strip():
                section = f"## {_now_iso()} self_reflection (LLM)\n\n{summary.strip()}"
                llm_used = True
        except Exception as exc:
            try:
                import sys as _sys
                print(f"[V3 self-mod LLM FAIL] {type(exc).__name__}: {str(exc)[:160]}", file=_sys.stderr)
            except Exception:
                pass
            section = None
    if section is None:
        # raw fallback (對齊 Phase 1 行為)
        section = f"## {_now_iso()} self_reflection\n\n" + "\n".join(
            f"- {s}" for s in recent_turn_summaries
        )
    _append_section(memory_path, section)

    # char limit check
    compressed = _enforce_char_limit_compress(memory_path, char_limit_mem)

    return {
        "flushed": True, "reason": "ok",
        "char_limit": char_limit_mem,
        "compressed": compressed,
        "llm_used": llm_used,
        "memory_path": str(memory_path.relative_to(vault_root)),
    }


def flush_owner_profile(
    vault_root: Path,
    *,
    recent_owner_observations: list[str],
    channel_type: str = "normal",
    injection_risk: str = "low",
    user_id: str = "",
    session_id: str = "",
) -> dict:
    """V3 §12.3 + V3-E1 Bug 7: owner profile 接 LLM 整理."""
    if injection_risk == "high":
        return {"flushed": False, "reason": "injection_risk=high (D drift guard skip)"}

    _, char_limit_owner = _CHANNEL_CHAR_LIMITS.get(channel_type, _CHANNEL_CHAR_LIMITS["normal"])
    profile_path = vault_root / "00_System_Core" / "00.08_Owner_Profile.md"
    archive_dir = vault_root / "99_Archive" / "auto_archived" / "companion_memory_backup"

    if not profile_path.exists():
        return {"flushed": False, "reason": "00.08 不存在"}

    _backup_file(profile_path, archive_dir, keep=5)

    # V3-E1 Bug 7: LLM 整理 owner profile
    section = None
    llm_used = False
    if _llm_enabled_for_flush() and user_id and session_id:
        try:
            existing = profile_path.read_text(encoding="utf-8")
            summary = _llm_summarize_owner_profile(vault_root, user_id, session_id, existing)
            if summary.strip():
                section = f"## {_now_iso()} owner observation (LLM)\n\n{summary.strip()}"
                llm_used = True
        except Exception as exc:
            try:
                import sys as _sys
                print(f"[V3 owner-profile LLM FAIL] {type(exc).__name__}: {str(exc)[:160]}", file=_sys.stderr)
            except Exception:
                pass
            section = None
    if section is None:
        section = f"## {_now_iso()} owner observation\n\n" + "\n".join(
            f"- {o}" for o in recent_owner_observations
        )
    _append_section(profile_path, section)

    compressed = _enforce_char_limit_compress(profile_path, char_limit_owner)

    return {
        "flushed": True, "reason": "ok",
        "char_limit": char_limit_owner,
        "compressed": compressed,
        "llm_used": llm_used,
        "owner_profile_path": str(profile_path.relative_to(vault_root)),
    }
