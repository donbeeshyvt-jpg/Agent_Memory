"""Multi-user profile namespace — R18 C84 (Path A).

對齊 V2_Round15_memory_capture_設計 §9「多 user identity (USER_<id>.md 拆分)」
+ MISSION §3.3 雙向投餵 (使用者投餵每人獨立) + §3.1 全對話驅動 (Discord user_id
自動拆 namespace, 不需 menu).

設計拍板 (使用者 2026-05-23 確認):
  - 識別方式: Discord user_id 自動分 (CLI 可帶 --user-id alice 模擬)
  - 路徑: `10_Permanent/Profiles/<id>/USER.md` (namespace 子腦)
  - 共享 vs 私有邊界:
    * 共享 (vault namespace): Concepts/, Facts/, MEMORY.md (跨 user 都看得到)
    * 私有 (per user): Profiles/<id>/USER.md + Profiles/<id>/captures/
    * 過渡 (channel 內): shared-channel session log (cross-session linker R9)
  - 跟既有 USER.md 相容: 保留 USER.md 為「default user / 單人模式」入口,
    多人模式建 USER_<id>.md (不破壞 R7-R17 既有測試)

提供 API:
  - normalize_user_id(raw) -> str        — 標準化 user_id (Discord ID 或 alias)
  - user_profile_path(user_id) -> str    — 回傳該 user 的 USER.md vault 相對路徑
  - user_captures_dir(user_id) -> str    — 回傳該 user 的 captures/ vault 相對路徑
  - ensure_user_profile(adapter, user_id) -> str — 自動建 Profiles/<id>/USER.md
  - is_default_user(user_id) -> bool     — 判斷是否為「default user / 單人模式」
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Lazy imports inside functions to avoid circular deps with vault.obsidian

# ─── 常數 ─────────────────────────────────────────────────────────────────────

PROFILES_DIR_ROOT = "10_Permanent/Profiles"

# default user 標記 (單人模式 / 沒 user_id 來源時 fallback)
# 「default」= 既有 USER.md 用法; 多用戶模式才會建 USER_<id>.md
DEFAULT_USER_ID = "default"

# user_id 字元白名單 (避免檔名跨平台 issue + path traversal)
# 允許: a-zA-Z 0-9 _ - . CJK
_USER_ID_INVALID = re.compile(r"[^a-zA-Z0-9一-鿿_.\-]+")
_USER_ID_MAX_LEN = 40


# ─── public API ───────────────────────────────────────────────────────────────


# default 用戶的等價 aliases (對齊既有 CLI / transport 預設行為).
# 這些 alias normalize 後都走 default namespace (Profiles/USER.md), 不 fork 子目錄.
_DEFAULT_USER_ALIASES = {"default", "cli-user", "cli_user", "smoke-suite", "smoke"}


def normalize_user_id(raw: str | None) -> str:
    """標準化 user_id, 對齊既有 sanitize_component pattern.

    Args:
        raw: 原始 user_id (Discord ID / CLI alias / None)

    Returns:
        標準化後的 user_id, 空輸入 / 純非法字元 → DEFAULT_USER_ID.
        既有 CLI/smoke default aliases ("cli-user" / "smoke-suite" 等)
        也 normalize 到 DEFAULT_USER_ID (相容既有 R7-R17 行為).

    例:
        normalize_user_id("alice")             → "alice"
        normalize_user_id("123456789012345")   → "123456789012345" (Discord snowflake ID)
        normalize_user_id("alice@example.com") → "alice_example.com"
        normalize_user_id("阿凱")               → "阿凱"
        normalize_user_id(None)                → "default"
        normalize_user_id("")                  → "default"
        normalize_user_id("cli-user")          → "default"  (既有 CLI 預設, 不 fork)
        normalize_user_id("smoke-suite")       → "default"  (e2e smoke 預設)
        normalize_user_id("../../etc")         → "etc"  (path traversal 防禦)
    """
    if not raw or not str(raw).strip():
        return DEFAULT_USER_ID
    text = str(raw).strip()
    # 既有 CLI/smoke default aliases (case-insensitive) → default
    if text.lower() in _DEFAULT_USER_ALIASES:
        return DEFAULT_USER_ID
    # 拆掉 path traversal: .. / / \ 等
    text = text.replace("\\", "/").split("/")[-1]
    # invalid char → _
    cleaned = _USER_ID_INVALID.sub("_", text).strip("_-.")
    if not cleaned:
        return DEFAULT_USER_ID
    if len(cleaned) > _USER_ID_MAX_LEN:
        cleaned = cleaned[:_USER_ID_MAX_LEN]
    return cleaned


def is_default_user(user_id: str | None) -> bool:
    """判斷是否為 default user (單人模式).

    對齊既有 USER.md 直接放 Profiles/ 根 (不拆 namespace), 預設模式.
    多用戶模式 (Discord 來 user_id) 走 Profiles/<id>/USER.md 子目錄.
    """
    norm = normalize_user_id(user_id)
    return norm == DEFAULT_USER_ID


def user_profile_path(user_id: str | None) -> str:
    """回傳該 user 的 USER.md vault 相對路徑.

    對齊使用者拍板 #2: namespace 子腦 (10_Permanent/Profiles/<id>/USER.md).
    default user 保留既有 Profiles/USER.md 路徑 (對齊拍板 #4 USER.md 保留).
    """
    norm = normalize_user_id(user_id)
    if norm == DEFAULT_USER_ID:
        return f"{PROFILES_DIR_ROOT}/USER.md"
    return f"{PROFILES_DIR_ROOT}/{norm}/USER.md"


def user_captures_dir(user_id: str | None) -> str:
    """回傳該 user 的 captures/ vault 相對路徑 (R16 memory_capture 多用戶版).

    對齊使用者拍板 #3 嚴格私有: 每 user 的 capture 不該洩漏給同 channel 他人.
    default user 走既有 10_Permanent/Manual_Inputs/captures/ 路徑.
    """
    norm = normalize_user_id(user_id)
    if norm == DEFAULT_USER_ID:
        return "10_Permanent/Manual_Inputs/captures"
    return f"{PROFILES_DIR_ROOT}/{norm}/captures"


def ensure_user_profile(adapter: Any, user_id: str | None) -> str:
    """自動建立該 user 的 USER.md (如果不存在).

    Returns: 該 user 的 USER.md vault 相對路徑.

    default user (單人模式) 不在這建 — adapter.ensure_skeleton 已建 USER.md.
    多用戶 (Profiles/<id>/USER.md) 在這 lazy 建.
    """
    norm = normalize_user_id(user_id)
    if norm == DEFAULT_USER_ID:
        # default user — 既有 Profiles/USER.md 由 ensure_skeleton 建, 不重建
        return user_profile_path(norm)

    target_path = user_profile_path(norm)
    # 若已存在不重建
    existing = adapter.read_note(target_path)
    if existing is not None:
        return target_path

    # lazy import 避循環
    from agent_memory.types import (
        EtlStatus,
        Frontmatter,
        LifecycleState,
        MemoryNote,
        MemorySource,
        MemoryType,
    )

    now_iso = ""  # Frontmatter created/updated 預設 datetime.now(utc), 不需手填

    note = MemoryNote(
        path=target_path,
        frontmatter=Frontmatter(
            type=MemoryType.USER_PROFILE,
            source=MemorySource.USER,
            tags=["profile", "user_namespace", f"user:{norm}"],
            aliases=[norm],
            etl_status=EtlStatus.INTERNALISED,
            lifecycle_state=LifecycleState.LONG,
            pinned=True,  # user profile 是 baseline, 不該被自動降級
            extras={
                "user_id": norm,
                "namespace_kind": "multi_user",
                "created_by": "user_profile.ensure_user_profile",
            },
        ),
        body=(
            f"# USER ({norm})\n\n"
            f"> 使用者個人檔 (multi-user namespace, user_id=`{norm}`).\n"
            f"> 對齊 R18 Path A 多角色管家延伸 + V2_Round15 §9 規格.\n"
            f"> 此檔由系統自動建, 使用者可填個人偏好 / 身份 / 注意事項.\n\n"
            f"## 個人簡介\n\n"
            f"- 偏好稱呼：（請填寫, 或系統會從對話自動更新）\n"
            f"- 主要身份／角色：（請填寫）\n"
            f"- 使用語言：繁體中文\n\n"
            f"## 偏好設定\n\n"
            f"- 回覆語氣：（例如：精簡、技術導向、可執行）\n"
            f"- 偏好工具：（例如：CLI / Discord）\n\n"
            f"## 個人記憶區\n\n"
            f"- 個人 capture (`Profiles/{norm}/captures/`) 嚴格私有, 不跟其他 user 共享.\n"
            f"- 共享記憶區 (`10_Permanent/Concepts/Facts/MEMORY.md`) 跨 user 共用.\n"
        ),
    )
    adapter.write_note(note)
    return target_path


__all__ = [
    "DEFAULT_USER_ID",
    "PROFILES_DIR_ROOT",
    "normalize_user_id",
    "is_default_user",
    "user_profile_path",
    "user_captures_dir",
    "ensure_user_profile",
]
