"""V3 C16 Obsidian Watcher — 雙向同步 + 人類優先衝突解決.

對齊 V3 §19 + D-V3-10 + D12 拍板.

監測 vault/ .md mtime 變動 → re-index sqlite-fts5.
衝突: 使用者編輯 timestamp > AI 寫入 → 採使用者版本 (人類優先).

Phase 2 MVP: incremental scan (檢查 mtime), Phase 3 改 watchdog 套件 background thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class WatcherState:
    """V3 §19: 監測 .md mtime 累積."""

    last_scan_at: float = 0.0
    file_mtimes: dict[str, float] = field(default_factory=dict)  # path → mtime


@dataclass(slots=True)
class WatcherScanResult:
    new_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    scan_duration_ms: float = 0.0


def scan_vault_incremental(
    vault_root: Path,
    state: WatcherState,
    *, watched_subdirs: Optional[list[str]] = None,
) -> WatcherScanResult:
    """V3 §19: incremental scan — 比 mtime 找新/改/刪.

    Args:
        vault_root: vault 根目錄
        state: 上次 scan 狀態 (in-memory or 從 db 載入)
        watched_subdirs: 監測子目錄 (None = 全 vault)

    Returns: WatcherScanResult
    """
    start = time.time()
    result = WatcherScanResult()
    watched = watched_subdirs or [
        "00_System_Core", "20_Audience_Graph", "30_Emotional_State",
        "40_Knowledge_Base", "60_Preference_Memory", "70_Persona_Versions",
    ]

    current_files: dict[str, float] = {}
    for sub in watched:
        sub_dir = vault_root / sub
        if not sub_dir.exists():
            continue
        for md in sub_dir.rglob("*.md"):
            try:
                rel = str(md.relative_to(vault_root))
                current_files[rel] = md.stat().st_mtime
            except Exception:
                continue

    # 比對
    for rel, mtime in current_files.items():
        if rel not in state.file_mtimes:
            result.new_files.append(rel)
        elif state.file_mtimes[rel] != mtime:
            result.modified_files.append(rel)

    # 找刪除
    for rel in state.file_mtimes:
        if rel not in current_files:
            result.deleted_files.append(rel)

    # 更新 state
    state.file_mtimes = current_files
    state.last_scan_at = time.time()
    result.scan_duration_ms = (time.time() - start) * 1000
    return result


def resolve_conflict(
    *, user_mtime: float, ai_mtime: float, prefer_user: bool = True,
) -> str:
    """V3 §19 衝突解決: 人類優先 (D12 + D-V3-10).

    Returns: 'user' 或 'ai'
    """
    if prefer_user:
        # 使用者 timestamp 新 → 採使用者
        if user_mtime >= ai_mtime:
            return "user"
        return "ai"
    return "user" if user_mtime > ai_mtime else "ai"


# 簡易 reindex hook (V3 Phase 2 不接真實 sqlite-fts5, 留 Phase 3 整合)
def reindex_changed_files(
    vault_root: Path, changed_paths: list[str],
) -> dict:
    """V3 §19: incremental re-index. Phase 2 stub, Phase 3 接 search.manager."""
    # Phase 2 純 marker (記錄要 re-index 的 paths)
    return {
        "queued_for_reindex": len(changed_paths),
        "paths": changed_paths[:10],  # sample 10 個
        "note": "Phase 3 接 search.manager.index_path() 真實 re-index",
    }
