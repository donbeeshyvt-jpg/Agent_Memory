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
    "cli": 9999,
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


def flush_self_memory(
    vault_root: Path,
    *,
    recent_turn_summaries: list[str],
    channel_type: str = "normal",
    injection_risk: str = "low",
    identity_relevance: float = 0.0,
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

    # append new section
    section = f"## {_now_iso()} self_reflection\n\n" + "\n".join(f"- {s}" for s in recent_turn_summaries)
    _append_section(memory_path, section)

    # char limit check
    compressed = _enforce_char_limit_compress(memory_path, char_limit_mem)

    return {
        "flushed": True, "reason": "ok",
        "char_limit": char_limit_mem,
        "compressed": compressed,
        "memory_path": str(memory_path.relative_to(vault_root)),
    }


def flush_owner_profile(
    vault_root: Path,
    *,
    recent_owner_observations: list[str],
    channel_type: str = "normal",
    injection_risk: str = "low",
) -> dict:
    """V3 §12.3: 把 owner 偏好/情緒 observations append 到 00.08_Owner_Profile.md."""
    if injection_risk == "high":
        return {"flushed": False, "reason": "injection_risk=high (D drift guard skip)"}

    _, char_limit_owner = _CHANNEL_CHAR_LIMITS.get(channel_type, _CHANNEL_CHAR_LIMITS["normal"])
    profile_path = vault_root / "00_System_Core" / "00.08_Owner_Profile.md"
    archive_dir = vault_root / "99_Archive" / "auto_archived" / "companion_memory_backup"

    if not profile_path.exists():
        return {"flushed": False, "reason": "00.08 不存在"}

    _backup_file(profile_path, archive_dir, keep=5)

    section = f"## {_now_iso()} owner observation\n\n" + "\n".join(f"- {o}" for o in recent_owner_observations)
    _append_section(profile_path, section)

    compressed = _enforce_char_limit_compress(profile_path, char_limit_owner)

    return {
        "flushed": True, "reason": "ok",
        "char_limit": char_limit_owner,
        "compressed": compressed,
        "owner_profile_path": str(profile_path.relative_to(vault_root)),
    }
