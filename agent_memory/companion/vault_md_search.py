# -*- coding: utf-8 -*-
"""V3-O.14 統一 vault md 搜尋設施 — 把 40_KB 的 RAG 推廣到全 vault.

對齊:
- 原 knowledge_base.py:retrieve_knowledge 只覆蓋 40_Knowledge_Base.
- 新需求 (user 2026-06-05): 50_Skills_Tools / 60_Preference_Memory / 90_Daily_Journal /
  30_Emotional_State 都該走 hybrid_search (FTS5 + dense vector), 不再只給 50 字摘要.
- MISSION §3.4 RAG + .DB 雙寫 — 此設施給 prompt assembly 撈到完整 md content.

設計:
- 一個 generic `retrieve_md_by_prefix(query, source_path_prefix, top_k, max_chars)`
- N 個 thin wrapper: retrieve_skills / retrieve_daily_journal / retrieve_preferences_md / ...
- fallback: 純 substring match (V2 search infrastructure 沒接時)
- score: 主要靠 SearchManager.hybrid_search (BM25 + dense embedding)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def retrieve_md_by_prefix(
    vault_root: Path,
    query: str,
    *,
    source_path_prefix: str,
    top_k: int = 3,
    max_chars_per_hit: int = 600,
    exclude_subdirs: Optional[list[str]] = None,
) -> list[dict]:
    """V3-O.14: hybrid retrieve vault 內指定 prefix 的 md.

    Args:
        vault_root: vault root path
        query: search query (current user message / topic / concept)
        source_path_prefix: e.g. "50_Skills_Tools", "60_Preference_Memory", "40_Knowledge_Base"
        top_k: 撈幾筆
        max_chars_per_hit: 每筆最多回多少字
        exclude_subdirs: 跳過的子目錄 (相對 prefix), e.g. ["_ingest_inbox", "_processed"]

    Returns: [{path, content, score, source_prefix}, ...]
    """
    if not query or not query.strip() or not source_path_prefix:
        return []
    exclude_subdirs = exclude_subdirs or []

    # 嘗試走 V2 SearchManager hybrid_search (FTS5 + dense)
    try:
        from agent_memory.search.manager import SearchManager
        from agent_memory.config import load_settings
        settings = load_settings(vault_root)
        manager = SearchManager(vault_root, settings)
        # 撈寬鬆一點再 filter
        results = manager.hybrid_search(query, top_k=top_k * 3)
        hits = []
        for r in results:
            path_str = str(getattr(r, "path", ""))
            if source_path_prefix not in path_str:
                continue
            if any(skip in path_str for skip in exclude_subdirs):
                continue
            # 讀完整 md (strip frontmatter, 截 max_chars)
            full = _read_md_body(vault_root / path_str, max_chars_per_hit)
            if not full:
                continue
            hits.append({
                "path": path_str,
                "content": full,
                "score": float(getattr(r, "score", 0.0)),
                "source_prefix": source_path_prefix,
            })
            if len(hits) >= top_k:
                break
        if hits:
            return hits
    except Exception:
        pass

    # fallback: substring 全掃
    return _fallback_substring_search(
        vault_root, query, source_path_prefix,
        top_k=top_k, max_chars_per_hit=max_chars_per_hit,
        exclude_subdirs=exclude_subdirs,
    )


def _read_md_body(p: Path, max_chars: int) -> str:
    """讀 md, strip frontmatter, 截 max_chars."""
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            text = text[end + 5:]
    return text[:max_chars].strip()


def _fallback_substring_search(
    vault_root: Path,
    query: str,
    source_path_prefix: str,
    *,
    top_k: int,
    max_chars_per_hit: int,
    exclude_subdirs: list[str],
) -> list[dict]:
    """No-V2-search fallback: 純 substring 全掃."""
    base = vault_root / source_path_prefix
    if not base.exists():
        return []
    hits = []
    query_lower = query.lower()
    # 拆 query 成多個 keyword (空白/標點分隔), 任一命中算 partial match
    keywords = [k for k in _tokenize_query(query_lower) if len(k) >= 2]
    for md in base.rglob("*.md"):
        if md.name.startswith("_"):
            continue
        # exclude_subdirs check
        rel = str(md.relative_to(vault_root))
        if any(skip in rel for skip in exclude_subdirs):
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        content_lower = content.lower()
        # 計算 score: 主 query 完整命中 +2, 個別 keyword 命中 +0.5
        score = 0.0
        if query_lower in content_lower:
            score += 2.0
        for kw in keywords:
            if kw in content_lower:
                score += 0.5
        if score == 0:
            continue
        body = _read_md_body(md, max_chars_per_hit)
        hits.append({
            "path": rel,
            "content": body,
            "score": score,
            "source_prefix": source_path_prefix,
        })
    return sorted(hits, key=lambda h: -h["score"])[:top_k]


def _tokenize_query(s: str) -> list[str]:
    """簡單拆字: 空白 / 中文一字一 token / 標點移除."""
    import re as _re
    s = _re.sub(r"[\s\.,!?;:。，！？；：、\"'\(\)（）\[\]【】]+", " ", s)
    tokens = []
    for chunk in s.split():
        # 短英文/數字 token 整段, 中文混雜時拆 1-字 + 2-字 token
        if all(ord(c) < 128 for c in chunk):
            tokens.append(chunk)
        else:
            # 中文 2-gram
            for i in range(len(chunk)):
                if i + 2 <= len(chunk):
                    tokens.append(chunk[i:i+2])
    return tokens


# ─── thin wrappers per source ───────────────────────────────────────────

def retrieve_skills(
    vault_root: Path, query: str, *, top_k: int = 3, max_chars: int = 2000,
) -> list[dict]:
    """V3-O.14 C3 + V3-O.15: 撈 50_Skills_Tools 相關完整內容.

    給 memory_router L3 用 — 取代 list_recent_skills_summary 那 50 字 truncate.
    V3-O.15 (2026-06-05): max_chars 800→2000 — schema v12 內文可達 25000, 撈 2000 字夠用.
    """
    return retrieve_md_by_prefix(
        vault_root, query,
        source_path_prefix="50_Skills_Tools",
        top_k=top_k,
        max_chars_per_hit=max_chars,
    )


def retrieve_external_knowledge(
    vault_root: Path, query: str, *, top_k: int = 3, max_chars: int = 2000,
) -> list[dict]:
    """V3-O.15: 撈 40_Knowledge_Base 完整內容 (跨 41_Owner_Provided + 42_Self_Lookup).

    取代原 knowledge_base.retrieve_knowledge (限 40_KB 200 字 fallback).
    走統一 hybrid_search, max_chars 2000 對齊 SKILL.
    """
    return retrieve_md_by_prefix(
        vault_root, query,
        source_path_prefix="40_Knowledge_Base",
        top_k=top_k,
        max_chars_per_hit=max_chars,
        exclude_subdirs=["_inbox", "_processed", "_ingest_inbox"],  # 跳處理中/原檔
    )


def retrieve_friend_cards(
    vault_root: Path, query: str, *, top_k: int = 3, max_chars: int = 5000,
) -> list[dict]:
    """V3-O.15.6 (2026-06-06 user 拍板): 撈 20_Audience_Graph 朋友卡 RAG.

    任何對話都 RAG, 不限 owner. 撈到的整張卡 (含 frontmatter + highlight + 彙整 + 反思
    + 偏好觀察) 進 prompt, 給 LLM 「查回來的記憶」用.

    user 拍板: 「當朋友被叫到時可以撈到該卡片, 真撈到該卡片就可以看跟他的對話大綱, 他可
    能就可以回答那位朋友的問題. 整張卡近來沒關係, 因為收束 prompt 大容量」.

    撈範圍:
    - 22_Casual_Viewers/ ✓ 主要
    - 21_VIP_Viewers/ ✓
    - 排除 23_Inside_Jokes/ (跨 viewer 共用的, 不是個人卡)
    """
    return retrieve_md_by_prefix(
        vault_root, query,
        source_path_prefix="20_Audience_Graph",
        top_k=top_k,
        max_chars_per_hit=max_chars,
        exclude_subdirs=["23_Inside_Jokes"],
    )


def retrieve_daily_journal(
    vault_root: Path, query: str = "", *, top_k: int = 2, max_chars: int = 400,
) -> list[dict]:
    """V3-O.14 audit 補洞: 撈 90_Daily_Journal 給 prompt.

    query 空時 → 撈最近的 (走 fallback 路徑按 mtime 排).
    """
    if query.strip():
        return retrieve_md_by_prefix(
            vault_root, query,
            source_path_prefix="90_Daily_Journal",
            top_k=top_k,
            max_chars_per_hit=max_chars,
        )
    # query 空時, 走最近 mtime 路徑
    base = vault_root / "90_Daily_Journal"
    if not base.exists():
        return []
    files = sorted(base.rglob("*.md"), key=lambda p: -p.stat().st_mtime)[:top_k]
    return [
        {
            "path": str(f.relative_to(vault_root)),
            "content": _read_md_body(f, max_chars),
            "score": 0.5,
            "source_prefix": "90_Daily_Journal",
        }
        for f in files if not f.name.startswith("_")
    ]


def retrieve_preferences_md(
    vault_root: Path, query: str, *, top_k: int = 3, max_chars: int = 400,
) -> list[dict]:
    """V3-O.14 audit 補洞: 撈 60_Preference_Memory md (不只 DB)."""
    return retrieve_md_by_prefix(
        vault_root, query,
        source_path_prefix="60_Preference_Memory",
        top_k=top_k,
        max_chars_per_hit=max_chars,
    )


def retrieve_emotional_state(
    vault_root: Path, query: str = "", *, top_k: int = 2, max_chars: int = 300,
) -> list[dict]:
    """V3-O.14 audit 補洞: 撈 30_Emotional_State 給 prompt.

    優先 33_Trait_Evolution + 34_Mood_Diary (32_Appraisal_Events 量太大會雜訊).
    """
    hits = []
    # 33_Trait_Evolution (按 mtime)
    trait_dir = vault_root / "30_Emotional_State" / "33_Trait_Evolution"
    if trait_dir.exists():
        for f in sorted(trait_dir.rglob("*.md"), key=lambda p: -p.stat().st_mtime)[:1]:
            hits.append({
                "path": str(f.relative_to(vault_root)),
                "content": _read_md_body(f, max_chars),
                "score": 0.8,
                "source_prefix": "33_Trait_Evolution",
            })
    # 34_Mood_Diary (按 mtime)
    mood_dir = vault_root / "30_Emotional_State" / "34_Mood_Diary"
    if mood_dir.exists():
        for f in sorted(mood_dir.rglob("*.md"), key=lambda p: -p.stat().st_mtime)[:1]:
            hits.append({
                "path": str(f.relative_to(vault_root)),
                "content": _read_md_body(f, max_chars),
                "score": 0.7,
                "source_prefix": "34_Mood_Diary",
            })
    return hits[:top_k]
