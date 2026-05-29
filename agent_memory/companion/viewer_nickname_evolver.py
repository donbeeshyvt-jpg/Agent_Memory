"""V3-O.10 #14 — Viewer nickname evolver.

對齊 owner_aliases.py 設計，抓 viewer 對話自報暱稱：
  「我叫 X」「叫我 X」「我是 X」→ 更新 DB users.display_name + nickname_history
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# 自報 pattern (中英文)
_SELF_REPORT_PATTERNS = (
    re.compile(r"(?:我叫|叫我|我是|我的名字是)\s*([^\s　,，。.!?！？:：、'\"`<>(){}\[\]【】「」]{2,20})"),
    re.compile(r"(?i)\b(?:call\s*me|my\s*name\s*is|i\s*am|i'm)\s+([A-Za-z][A-Za-z0-9_\-]{1,30})\b"),
)

# 排除問句 pattern (避免「你叫什麼名字？」被誤抓)
_QUESTION_PATTERN = re.compile(r"[?？]")
_QUESTION_WORDS = ("什麼", "誰", "哪", "嗎", "呢", "吧", "啊")

_MIN_NAME_LEN = 2


def _is_question(text: str) -> bool:
    if _QUESTION_PATTERN.search(text):
        return True
    return any(w in text for w in _QUESTION_WORDS)


def extract_self_report_nickname(message: str) -> Optional[str]:
    """從訊息抓自報暱稱，回傳候選 or None."""
    if _is_question(message):
        return None
    for pat in _SELF_REPORT_PATTERNS:
        m = pat.search(message)
        if m:
            name = m.group(1).strip()
            if len(name) >= _MIN_NAME_LEN:
                return name
    return None


def maybe_update_nickname(
    vault_root: Path,
    user_id: str,
    message: str,
) -> Optional[str]:
    """偵測 viewer 自報暱稱，若有則更新 DB users.display_name + nickname_history.

    Returns: 新暱稱 or None（未偵測到或 user_id 為空）
    """
    if not user_id or user_id == "anonymous":
        return None

    nickname = extract_self_report_nickname(message)
    if not nickname:
        return None

    now = datetime.now(timezone.utc).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            row = conn.execute(
                "SELECT display_name, nickname_history FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None

            # 讀舊 history
            history_raw = row["nickname_history"] if "nickname_history" in row.keys() else None
            try:
                history: list = json.loads(history_raw) if history_raw else []
            except Exception:
                history = []

            # append 新紀錄
            history.append({"name": nickname, "at": now})
            if len(history) > 20:
                history = history[-20:]

            # 更新 display_name + history
            try:
                conn.execute(
                    "UPDATE users SET display_name=?, nickname_history=? WHERE user_id=?",
                    (nickname, json.dumps(history, ensure_ascii=False), user_id),
                )
            except Exception:
                # nickname_history 欄不存在 (舊 schema) → 只更新 display_name
                conn.execute(
                    "UPDATE users SET display_name=? WHERE user_id=?",
                    (nickname, user_id),
                )
            conn.commit()
    except Exception:
        return None

    return nickname
