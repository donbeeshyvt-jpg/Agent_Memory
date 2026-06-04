"""V3-O.13 #CR3 — 自編 catchphrase 重複偵測 + LLM 改寫.

問題: LLM 自編一個句尾招牌 (例「我要拿澆花壺敲你」), 進 raw_events 後下輪
prompt 看到自己用過 → 模仿 → N 輪後變固定招牌句, 觀眾覺得單調.

解法:
  1. detect_repeated_ending(): 撈最近 N 條 bot reply 結尾 12-15 字, 算 N-gram overlap.
     如果當前 reply 結尾跟過去結尾 fuzzy 相似 (Jaccard ≥ 0.5) 次數 ≥ THRESH (預設 3) → 重複.
  2. rewrite_repeated_ending(): call sub_task LLM (auxiliary="catchphrase_rewrite",
     deepseek-v4-flash) 改寫該結尾, 保留角色語氣 + 意圖, 換表達方式.

整合: companion_chat_runtime step16.6 之後, final_response 寫進 raw_events 前.
不阻塞 critical path: detect 是 ms 級 (sqlite 查 + Python set ops), rewrite 才 call LLM
(只在重複 ≥3 次才觸發, 大多 turn 不會走到).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional


# ── 參數 ────────────────────────────────────────────
LOOKBACK_BOT_REPLIES = 5        # 撈最近幾條 bot reply 比對
ENDING_TAIL_CHARS = 30          # 結尾抽前/後幾字當比對對象
NGRAM_SIZE = 3                  # n-gram 長度 (中文 3-gram)
JACCARD_THRESHOLD = 0.5         # n-gram Jaccard 相似度門檻
REPEAT_THRESHOLD = 3            # 重複次數門檻 (≥ 觸發改寫)


def _normalize_for_compare(s: str) -> str:
    """正規化文字用於比對: 去空白 / 標點 / 表情符號."""
    # 去常見標點 + 表情符號 + 空白, 留中英文跟數字
    return re.sub(r"[\s\W_]+", "", s or "")


def _ngrams(s: str, n: int = NGRAM_SIZE) -> set[str]:
    """中文 n-gram set (n 連續字元)."""
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度."""
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


def _extract_ending(text: str, tail_chars: int = ENDING_TAIL_CHARS) -> str:
    """抽 reply 結尾 N 字 (去尾部標點 / 表情符號)."""
    if not text:
        return ""
    # 去尾部表情符號 / 標點 (回到實質結尾句)
    stripped = re.sub(r"[\s\W_]+$", "", text)
    return stripped[-tail_chars:] if stripped else ""


def detect_repeated_ending(
    vault_root: Path,
    current_reply: str,
    user_id: str,
    *,
    lookback: int = LOOKBACK_BOT_REPLIES,
    jaccard_threshold: float = JACCARD_THRESHOLD,
) -> list[str]:
    """偵測 current_reply 結尾是否跟最近 N 條 bot reply 結尾重複.

    Returns:
        重複的歷史 ending 清單 (空 list = 不重複, 多筆 = 重複次數).
    """
    if not current_reply or not vault_root:
        return []
    curr_ending = _extract_ending(current_reply)
    if len(curr_ending) < 6:  # 太短不算重複
        return []
    curr_norm = _normalize_for_compare(curr_ending)
    curr_grams = _ngrams(curr_norm)
    if not curr_grams:
        return []

    db = Path(vault_root) / ".ai" / "companion.db"
    if not db.exists():
        return []

    repeats: list[str] = []
    try:
        conn = sqlite3.connect(str(db))
        cur = conn.cursor()
        cur.execute(
            "SELECT content FROM raw_events WHERE actor='bot' AND user_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (str(user_id), int(lookback)),
        )
        for (content,) in cur.fetchall():
            past_ending = _extract_ending(content or "")
            if len(past_ending) < 6:
                continue
            past_norm = _normalize_for_compare(past_ending)
            past_grams = _ngrams(past_norm)
            if _jaccard(curr_grams, past_grams) >= jaccard_threshold:
                repeats.append(past_ending)
        conn.close()
    except Exception:
        return []
    return repeats


def rewrite_repeated_ending(
    vault_root: Path,
    current_reply: str,
    repeats: list[str],
) -> str:
    """call sub_task LLM 改寫 current_reply 結尾, 保留語氣 + 意圖, 換表達.

    若 LLM 失敗 → 原樣 return (degraded mode, 不破壞 reply).
    """
    if not current_reply or not repeats:
        return current_reply
    try:
        from agent_memory.llm_text_helpers import call_llm_for_text
    except Exception:
        return current_reply

    past_list = "\n".join(f"- {p}" for p in repeats[:5])
    prompt = (
        "以下是 AI 夥伴最新一次 reply (繁體中文):\n"
        f"「{current_reply}」\n\n"
        "這個 reply 結尾跟過去幾次 reply 結尾重複用了類似招牌句:\n"
        f"{past_list}\n\n"
        "請改寫【整段 reply】, 保留:\n"
        "- 原本的對話內容與語意 (回應 user 的話)\n"
        "- 角色語氣 (溫柔可愛 / 偶爾毒舌)\n"
        "- 結尾的威脅/玩鬧意圖 (如果原本有)\n"
        "但結尾的招牌句必須換個說法表達, 用幽默或新點子, 不能再用同樣詞組.\n"
        "只輸出改寫後的完整 reply 文字, 不要解釋, 不要加引號或標題.\n"
    )
    try:
        rewritten = call_llm_for_text(
            Path(vault_root),
            prompt,
            persona_id="companion",
            temperature=0.7,
            timeout_s=8.0,
            auxiliary="catchphrase_rewrite",
        )
        rewritten = (rewritten or "").strip()
        # 安全檢查: 改寫後不能完全空 / 不能差太多 (避免 LLM 完全偏題)
        if not rewritten or len(rewritten) < max(10, len(current_reply) // 4):
            return current_reply
        return rewritten
    except Exception:
        return current_reply


def maybe_rewrite_if_repeated(
    vault_root: Path,
    current_reply: str,
    user_id: str,
    *,
    repeat_threshold: int = REPEAT_THRESHOLD,
) -> str:
    """高層 API: 偵測重複, 達 threshold 才 call LLM 改寫. 不阻塞 nominal case.

    回傳: 改寫後 reply (or 原樣 if 不重複 / 改寫失敗).
    """
    if not current_reply:
        return current_reply
    try:
        repeats = detect_repeated_ending(vault_root, current_reply, user_id)
        if len(repeats) >= repeat_threshold:
            return rewrite_repeated_ending(vault_root, current_reply, repeats)
    except Exception:
        pass
    return current_reply
