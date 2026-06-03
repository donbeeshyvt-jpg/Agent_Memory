# -*- coding: utf-8 -*-
"""V3-H5 殘-11 H8 Inside Jokes (user 2026-05-27): 偵測 + 寫入 + 撈 + 注入機制.

對齊:
- V3 §29.8 H8 Associative Callback
- V3 §13 Memory Router L4 (Inside Jokes 撈)
- V3 §5 vault 20_Audience_Graph/23_Inside_Jokes/

設計:
- detect_inside_jokes_for_user(vault_root, user_id, window_days=7): 偵測 keyword 重複 ≥ 3 次
- write_inside_joke_md(vault_root, keyword, user_id, ...): 寫 23_Inside_Jokes/<kw>_<uid>.md
- list_active_inside_jokes(vault_root, user_id, intimacy_threshold=0.4): 撈該 user 的 active jokes
- maybe_inject_inside_joke(response_text, jokes, rng): 10% 機率對符合條件注入

觸發點:
- curator L3 24h medium 跑 _detect_inside_jokes_pass (V3-H5 加在 companion_curator)
- chat_runtime Step 16.5 (Output Governor 後) 對 playfulness>0.5 + intim ≥ 0.4 注入
"""
from __future__ import annotations

import random
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db
from agent_memory.security.atomic import atomic_write


INSIDE_JOKE_DIR = "20_Audience_Graph/23_Inside_Jokes"


# 簡單 keyword 偵測規範 (V3 §29.8 H8)
_KEYWORD_MIN_LEN = 3   # 至少 3 chars (避免「啊」「嗯」等)
_KEYWORD_MAX_LEN = 12  # 至多 12 chars (避免整句重複)
_KEYWORD_MIN_COUNT = 3  # 至少出現 3 次
_STOPWORDS = {
    "你", "我", "他", "她", "的", "了", "嗎", "啊", "呢", "吧", "哦", "喔", "嗯", "哈",
    "你好", "再見", "謝謝", "對", "不對", "是", "不是", "好", "不好", "可以", "不可以",
    "system", "prompt", "user", "bot", "assistant",
}


def _safe_filename(s: str) -> str:
    safe = re.sub(r'[\\/:"*?<>|]+', '_', s).strip()
    safe = re.sub(r'\s+', '_', safe)
    return safe[:60] or "untitled"


def _extract_candidate_keywords(text: str) -> list[str]:
    """V3-H5: 從 text 抓候選 keyword (n-gram 簡化版)."""
    if not text:
        return []
    # 簡單分詞: 中文 + 英文連續字串
    # 對中文用 2-4 字 sliding window, 對英文用 word
    candidates = set()
    # 英文 word
    for match in re.findall(r'[a-zA-Z]{4,15}', text):
        if match.lower() not in _STOPWORDS:
            candidates.add(match.lower())
    # 中文 2-4 字 sliding window
    chinese_only = re.sub(r'[^一-鿿]+', ' ', text)
    for chunk in chinese_only.split():
        for n in range(_KEYWORD_MIN_LEN, _KEYWORD_MAX_LEN + 1):
            for i in range(len(chunk) - n + 1):
                sub = chunk[i:i+n]
                if sub not in _STOPWORDS and len(sub) >= _KEYWORD_MIN_LEN:
                    candidates.add(sub)
    return list(candidates)


def detect_inside_jokes_for_user(
    vault_root: Path, user_id: str, *, window_days: int = 7,
) -> list[dict]:
    """V3-H5: 偵測該 user 過去 N 天內重複出現 ≥ 3 次的 keyword.

    Returns: [{keyword, count, first_seen, last_seen}, ...] 排序 by count DESC.
    """
    if not user_id or user_id == "anonymous":
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    try:
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT content, created_at FROM raw_events "
                "WHERE user_id=? AND created_at > ? AND actor IN ('user','bot') "
                "ORDER BY created_at ASC",
                (user_id, cutoff),
            ).fetchall()
    except Exception:
        return []
    if not rows:
        return []

    # 聚合所有 keyword
    keyword_counter: Counter = Counter()
    keyword_first_seen: dict[str, str] = {}
    keyword_last_seen: dict[str, str] = {}
    for r in rows:
        content = r["content"] or ""
        ts = r["created_at"] or ""
        kws = _extract_candidate_keywords(content)
        for kw in kws:
            keyword_counter[kw] += 1
            if kw not in keyword_first_seen:
                keyword_first_seen[kw] = ts
            keyword_last_seen[kw] = ts

    # 過濾 ≥ MIN_COUNT
    result = []
    for kw, count in keyword_counter.most_common(20):  # top 20
        if count < _KEYWORD_MIN_COUNT:
            break
        result.append({
            "keyword": kw,
            "count": count,
            "first_seen": keyword_first_seen.get(kw, ""),
            "last_seen": keyword_last_seen.get(kw, ""),
        })
    return result


def write_inside_joke_md(
    vault_root: Path, *, keyword: str, user_id: str,
    use_count: int = 0,
    intimacy_threshold: float = 0.4,
    first_seen_at: str = "", last_used_at: str = "",
) -> Optional[Path]:
    """V3-H5: 寫 20_Audience_Graph/23_Inside_Jokes/<keyword>_<user>.md."""
    if not keyword or not user_id:
        return None
    safe_kw = _safe_filename(keyword)
    safe_uid = _safe_filename(user_id)[:30]
    target = vault_root / INSIDE_JOKE_DIR / f"{safe_kw}_{safe_uid}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "---",
        "type: inside_joke",
        "schema_version: 10",
        f"joke_keyword: {keyword}",
        f"user_id: {user_id}",
        f"intimacy_threshold: {intimacy_threshold:.2f}",
        f"use_count: {use_count}",
        f"first_seen_at: {first_seen_at or now}",
        f"last_used_at: {last_used_at or now}",
        "lifecycle_state: active",
        f"created_at: {now}",
        f"updated_at: {now}",
        "---",
        f"# Inside Joke — {keyword} (與 {user_id[:16]})",
        "",
        f"- 出現次數: {use_count}",
        f"- 親密門檻: {intimacy_threshold:.2f} (該 user intim ≥ 此才用)",
        "",
        "## 觸發策略",
        "",
        "- 當 playfulness > 0.5 + 該 user intim ≥ threshold → 10% 機率注入回應",
        "- 對齊 V3 §29.8 H8 + Memory Router L4",
        "",
        f"*Auto-generated by inside_joke_writer.py V3-H5 ({now[:19]})*",
    ]
    try:
        atomic_write(target, "\n".join(lines) + "\n")
        return target
    except Exception:
        return None


def list_active_inside_jokes(
    vault_root: Path, user_id: str, *, intimacy_score: float = 0.0,
) -> list[dict]:
    """V3-H5: 撈該 user 的 active inside jokes (對 intimacy >= threshold)."""
    if not user_id or user_id == "anonymous":
        return []
    base = vault_root / INSIDE_JOKE_DIR
    if not base.exists():
        return []
    safe_uid_pattern = _safe_filename(user_id)[:30]
    jokes = []
    for md in base.glob(f"*_{safe_uid_pattern}.md"):
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # 簡單 parse frontmatter
        kw_match = re.search(r"joke_keyword:\s*(.+)", content)
        thr_match = re.search(r"intimacy_threshold:\s*([\d.]+)", content)
        if not kw_match:
            continue
        keyword = kw_match.group(1).strip()
        threshold = float(thr_match.group(1)) if thr_match else 0.4
        if intimacy_score >= threshold:
            jokes.append({"keyword": keyword, "threshold": threshold, "path": str(md)})
    return jokes


def maybe_inject_inside_joke(
    response_text: str, jokes: list[dict], *,
    playfulness: float = 0.0, intimacy_score: float = 0.0,
    rng: Optional[random.Random] = None,
) -> str:
    """V3-H5: 對 playfulness>0.5 + intim ≥ 0.4 + random 10% 注入 inside joke.

    對齊 V3 §29.8 H8 + verbal_tic injection pattern.

    V3-O.12 #G6b (2026-06-03): user 觀察「(還記得我們的 X 哏嗎)」固定樣板太死 +
    inside_joke detector 把系統包裝詞 (列隊彙整) 誤判為哏 → 暫關注入. 待 G5 升級
    用 LLM 動態生成 callback 句式 (取代固定「(還記得 X 哏嗎)」格式) 再 re-enable.
    detect / write md 仍正常跑 (累積 inside_jokes 候選, 為 G5 LLM 生成提供 context).
    """
    return response_text
    # ↓ 以下原邏輯保留以便 G5 階段快速 re-enable + 改 prompt
    if not response_text or not jokes:
        return response_text
    if playfulness <= 0.5 or intimacy_score < 0.4:
        return response_text
    rng = rng or random.Random()
    # 10% 機率注入
    if rng.random() > 0.10:
        return response_text
    chosen = rng.choice(jokes)
    keyword = chosen.get("keyword", "")
    if not keyword:
        return response_text
    # 簡單注入 (response 末段加「(joke callback)」hint)
    return f"{response_text} (還記得我們的 {keyword} 哏嗎)"
