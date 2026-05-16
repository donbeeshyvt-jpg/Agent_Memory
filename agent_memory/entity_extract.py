"""Entity extraction for memory promotion (R7 C17).

Round 7 升中期門檻是「同一 entity 在 daily_flush / session_log 出現 N1=2 次跨 2 session」。
此模組負責從 note body 抽 entity_id 列表。

策略 (V2_Round7 §4.1):
- 優先: wikilinks `[[xxx]]` 內容 → 用該字串做 entity_id (slug normalize)
- 退化: keyword fingerprint — 用簡單名詞 pattern (中文/英文) 抽前 N 個 unique 名詞 +
  sha hash 構成 entity_id (給沒 wikilinks 的 daily_flush 一個穩定 id)

不做 LLM extraction (避免依賴 + 算力, 留 Round 8 候選).
"""

from __future__ import annotations

import hashlib
import re

# Wikilinks: [[Page]] / [[Page#section]] / [[Page|alias]] 都只取 Page
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:#[^\]]*)?(?:\|[^\]]*)?\]\]")

# Slug normalize: 保留中文 + 英數 + dash/underscore，其他換 -
_SLUG_RE = re.compile(r"[^\w一-鿿\-]+", re.UNICODE)

# 簡單關鍵字 pattern — 中文 2-8 字 / 英文 2-30 字
_KEYWORD_RE = re.compile(r"[一-鿿]{2,8}|[A-Za-z][A-Za-z0-9_-]{1,29}", re.UNICODE)

# Stopwords — 中文常見虛詞 / 代名詞 + 英文 stopwords (避免「我們/the/and」被抽出)
_STOPWORDS_ZH: frozenset[str] = frozenset({
    "我們", "我", "你", "他", "她", "它", "是", "的", "了", "在", "和",
    "與", "及", "以", "把", "被", "為", "對", "向", "從", "於", "之", "也",
    "都", "就", "還", "又", "再", "請", "謝謝", "可以", "需要", "可能", "如果",
    "因為", "所以", "但是", "或者", "然後", "現在", "今天", "昨天", "明天",
    "這個", "那個", "這些", "那些", "什麼", "怎麼", "為什麼", "如何", "已經",
})
_STOPWORDS_EN: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "i", "you", "he", "she", "it", "we", "they", "this",
    "that", "these", "those", "as", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "about", "into", "through", "during", "before",
    "after", "above", "below", "between", "under", "again", "further",
})


def normalize_entity_id(raw: str, *, fallback: str = "entity") -> str:
    """Normalize raw text → slug-safe entity_id (保留中文 + 英數)."""

    cleaned = _SLUG_RE.sub("-", raw.strip().lower())
    cleaned = cleaned.strip("-")
    # 避免過長 (Obsidian filename 限制) — 截到 60 字
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rstrip("-")
    return cleaned or fallback


def extract_wikilinks(text: str) -> list[str]:
    """Extract wikilinks (`[[xxx]]` / `[[xxx|alias]]` / `[[xxx#section]]`) from markdown."""

    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text) if m.group(1).strip()]


def extract_keywords(text: str, *, top_n: int = 3) -> list[str]:
    """Fallback: extract first N unique nouns from text (中文 2-8 / 英文 2-30)."""

    seen: set[str] = set()
    result: list[str] = []
    for m in _KEYWORD_RE.finditer(text):
        word = m.group(0)
        wl = word.lower()
        if wl in _STOPWORDS_EN or word in _STOPWORDS_ZH:
            continue
        if wl in seen:
            continue
        seen.add(wl)
        result.append(word)
        if len(result) >= top_n:
            break
    return result


def keyword_fingerprint(keywords: list[str]) -> str:
    """Build fingerprint hash from keyword list (給沒 wikilinks 的 fallback entity_id)."""

    if not keywords:
        return ""
    joined = "|".join(sorted(k.lower() for k in keywords))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def extract_entities_from_text(text: str, *, max_entities: int = 10) -> list[str]:
    """High-level: 抽 entities from text. Wikilinks-first, keyword fingerprint 退化.

    Return: list of normalized entity_id (slug-safe), 去重保序, 最多 max_entities 個.
    """

    seen: set[str] = set()
    result: list[str] = []

    # 1) Wikilinks-first
    for w in extract_wikilinks(text):
        eid = normalize_entity_id(w)
        if eid and eid not in seen:
            seen.add(eid)
            result.append(eid)
            if len(result) >= max_entities:
                return result

    # 2) Keyword fingerprint 退化 (只在完全沒 wikilinks 時走, 避免噪音)
    if not result:
        keywords = extract_keywords(text, top_n=3)
        if keywords:
            fp = keyword_fingerprint(keywords)
            if fp:
                first_norm = normalize_entity_id(keywords[0])
                fallback_id = f"{first_norm}-{fp[:8]}" if first_norm and first_norm != "entity" else f"entity-{fp[:8]}"
                result.append(fallback_id)

    return result
