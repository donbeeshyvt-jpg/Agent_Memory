"""Memory capture — 軌道 B 記憶提醒意圖偵測 + 短記憶寫入 (R16 C69).

對齊 MISSION 主軸:
  - §3.1 全對話驅動: 使用者講「幫我記得 X」不用 menu / 不用指定路徑
  - §3.3 雙向投餵: 記憶提醒是對話形式的「使用者投餵」延伸 (跟 menu [M] 同性質)
  - §3.5 自我進化: capture 進 Manual_Inputs/captures/ 自然走 R7 curator 升 Concept
  - §5.2 安全邊界不破: 跟 tools_enabled 完全獨立, 不靠 [TOOL]memory.add 那條路

對應 V2_Round15_memory_capture_設計 §4-§5:
  - 軌道 A 一般對話 → 不觸發
  - 軌道 B 記憶提醒 → 本模組接住 (regex 雙詞綁定)
  - 軌道 C 真實寫檔 → 原 governance + R14.x T7.2 攔截

意圖偵測規則 (對齊 R14.4 C60 拍板「不收裸 keyword 必綁動詞」):
  - 雙詞綁定: (幫|請|麻煩)(我)?(記得|記住|記一下|寫下|提醒)
  - 變體: (提醒我|提醒你|提醒一下) / (我想記|我要記|我要寫下)
  - 反例 (不該觸發): 「我會記得吃飯」「自己記得」(R14.4 C60 教訓)

寫入點: 10_Permanent/Manual_Inputs/captures/<YYYY-MM-DD>_<slug>.md
  - 子目錄 captures/ 跟 menu [M] 手動投餵分開
  - frontmatter source=USER + tags 含 chat_capture (區分 menu vs 對話)
  - etl_status=INTERNALISED + lifecycle_state=LONG (對齊既有 Manual_Inputs 範式)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent_memory.types import (
    EtlStatus,
    Frontmatter,
    LifecycleState,
    MemoryNote,
    MemorySource,
    MemoryType,
    SecurityLevel,
)


# ─── 意圖偵測 ────────────────────────────────────────────────────────────────

# 雙詞綁定 — 對齊 R14.4 C60「不收裸 keyword 必綁動詞」
# alt 1: (幫|請|麻煩) + (可選 我) + (記得/記住/記一下/寫下/提醒/記下)
# alt 2: (提醒我|提醒你|提醒一下) — 純動詞片語
# alt 3: (我想記|我要記|我要寫下|我要你記|我要請你) — 第一人稱主動
_INTENT_PATTERNS = re.compile(
    r"(幫|請|麻煩)(我|你|您)?\s*(記得|記住|記一下|記下|寫下|提醒)"
    r"|"
    r"(提醒我|提醒你|提醒一下|提醒妳)"
    r"|"
    r"(我想記|我要記|我要寫下|我要你記住|我要請你記)"
)


@dataclass(slots=True)
class MemoryCaptureResult:
    """Capture detection + write 結果, 給 chat_runtime payload 用."""

    detected: bool = False
    saved: bool = False
    path: str | None = None
    summary: str | None = None  # 抓到的記憶提醒摘要 (前 80 字)
    reason: str = ""  # detected=False 時的原因 (例: "regex_no_match")
    matched_keyword: str | None = None  # regex 命中的 keyword
    error: str | None = None  # saved=False 但 detected=True 時的錯誤訊息


def detect_memory_capture_intent(message: str) -> MemoryCaptureResult:
    """從使用者訊息偵測軌道 B 記憶提醒意圖.

    Args:
        message: 使用者本回合輸入文字.

    Returns:
        MemoryCaptureResult — detected=True 時含 matched_keyword + summary.

    對齊 R14.4 C60 規矩: 精準度優先於召回率. 寧可漏抓不確定 case 也不誤殺
    一般對話. 例如「我會記得吃飯」「自己記得」**不該** 命中.
    """
    raw = (message or "").strip()
    if not raw:
        return MemoryCaptureResult(detected=False, reason="empty_message")

    match = _INTENT_PATTERNS.search(raw)
    if not match:
        return MemoryCaptureResult(detected=False, reason="regex_no_match")

    matched = match.group(0)
    # summary: 整段訊息前 80 字 (去多餘空白), 給 frontmatter + body 用
    summary = " ".join(raw.split())[:80]

    return MemoryCaptureResult(
        detected=True,
        matched_keyword=matched,
        summary=summary,
    )


# ─── 寫入 ────────────────────────────────────────────────────────────────────

# 對齊 V2_Round15 §5.2 寫入點: 10_Permanent/Manual_Inputs/captures/
# 子目錄 captures/ 跟 menu [M] 手動投餵 (Manual_Inputs/ 根) 分開
_CAPTURE_DIR = "10_Permanent/Manual_Inputs/captures"

# slug 字元白名單 (避免檔名跨平台 issue)
_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9一-鿿_-]+")


def _slug_from_summary(summary: str, *, max_len: int = 30) -> str:
    """從 summary 抽合法 slug 給檔名用 (CJK + a-zA-Z0-9 + _ -)."""
    cleaned = _SLUG_INVALID.sub("_", str(summary or "").strip())
    cleaned = cleaned.strip("_-")[:max_len] or "capture"
    return cleaned


def _build_capture_path(detected_at: datetime, summary: str) -> str:
    """組合 capture 落點檔名: captures/<YYYY-MM-DD>_<slug>.md."""
    date_part = detected_at.strftime("%Y-%m-%d")
    slug = _slug_from_summary(summary)
    return f"{_CAPTURE_DIR}/{date_part}_{slug}.md"


def _build_capture_body(
    *,
    user_message: str,
    matched_keyword: str,
    persona_id: str,
    context_id: str,
    session_id: str,
    detected_at: datetime,
) -> str:
    """Markdown body — 含原話 + 偵測 keyword + session 追蹤 metadata."""
    ts_iso = detected_at.isoformat()
    return (
        f"# Chat Memory Capture\n\n"
        f"> R16 軌道 B 記憶提醒意圖 — 對話中自動接住, 不需使用者指定路徑.\n"
        f"> 對應 V2_Round15 規格 §4.2 + MISSION §3.3 雙向投餵.\n\n"
        f"## 使用者原話\n\n"
        f"```\n{user_message.strip()}\n```\n\n"
        f"## 偵測 metadata\n\n"
        f"- 命中 keyword: `{matched_keyword}`\n"
        f"- 偵測時間: {ts_iso}\n"
        f"- persona: `{persona_id}`\n"
        f"- context: `{context_id}`\n"
        f"- session: `{session_id}`\n"
    )


def record_memory_capture(
    *,
    adapter: Any,  # ObsidianVaultAdapter (lazy typed)
    user_message: str,
    detection: MemoryCaptureResult,
    persona_id: str,
    context_id: str,
    session_id: str,
    now: datetime | None = None,
) -> MemoryCaptureResult:
    """把偵測到的記憶提醒寫入 Manual_Inputs/captures/.

    Args:
        adapter: ObsidianVaultAdapter — 走既有 write_note 管線 (含 atomic + index).
        user_message: 使用者本回合完整訊息.
        detection: detect_memory_capture_intent() 結果, 必須 detected=True.
        persona_id / context_id / session_id: chat turn metadata, 寫進 body 追蹤.
        now: 偵測時間 (None = datetime.now UTC).

    Returns:
        新 MemoryCaptureResult — saved=True/False, path=寫入路徑 (或 None 若失敗).

    錯誤: 若 detection.detected=False 直接 raise ValueError (caller bug).
    其他錯誤 (write_note IO / lock / encoding) 包進 result.error, 不 raise
    給 chat 流程繼續跑.
    """
    if not detection.detected:
        raise ValueError("detection.detected must be True before record_memory_capture")

    detected_at = now or datetime.now(timezone.utc)
    path = _build_capture_path(detected_at, detection.summary or "")
    matched_kw = detection.matched_keyword or "?"

    body = _build_capture_body(
        user_message=user_message,
        matched_keyword=matched_kw,
        persona_id=persona_id,
        context_id=context_id,
        session_id=session_id,
        detected_at=detected_at,
    )

    # frontmatter 對齊既有 Manual_Inputs 範式 (USER source + INTERNALISED + LONG)
    # 跟 menu [M] 區分: tags 加 chat_capture + extras["capture_kind"]="chat_capture"
    aliases_pool: list[str] = []
    if detection.summary:
        # 取前 20 字當 alias 給 GraphRAG entity link 用
        aliases_pool = [detection.summary[:20]]

    fm = Frontmatter(
        type=MemoryType.USER_PROFILE,
        source=MemorySource.USER,
        tags=["manual_input", "chat_capture", "memory_reminder"],
        aliases=aliases_pool,
        etl_status=EtlStatus.INTERNALISED,  # 對齊 §5.4 「使用者投餵 = 直接永久記憶」
        security_level=SecurityLevel.SAFE_DATA,
        lifecycle_state=LifecycleState.LONG,  # Manual_Inputs/ 都是 long 區
        pinned=False,  # 不釘 — 允許 R7 curator 自然降級/升 Concept
        extras={
            "capture_kind": "chat_capture",
            "capture_keyword": matched_kw,
            "capture_persona": persona_id,
            "capture_context": context_id,
            "capture_session": session_id,
        },
    )

    note = MemoryNote(path=path, frontmatter=fm, body=body)

    try:
        adapter.write_note(note)
    except Exception as exc:  # noqa: BLE001 — 不阻擋 chat 流程
        return MemoryCaptureResult(
            detected=True,
            saved=False,
            path=None,
            summary=detection.summary,
            matched_keyword=matched_kw,
            reason="write_failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    return MemoryCaptureResult(
        detected=True,
        saved=True,
        path=path,
        summary=detection.summary,
        matched_keyword=matched_kw,
    )


# ─── public API ──────────────────────────────────────────────────────────────

__all__ = [
    "MemoryCaptureResult",
    "detect_memory_capture_intent",
    "record_memory_capture",
]
