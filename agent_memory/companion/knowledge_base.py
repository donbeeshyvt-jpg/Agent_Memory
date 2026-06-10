# -*- coding: utf-8 -*-
"""V3-O.15 (2026-06-05 user 拍板): 40_Knowledge_Base 雙 inbox 管理 + RAG v12 schema.

對齊:
- V3-O.15 雙 inbox 設計:
    41_Daily_Knowledge/   ← 主人投放 (你拖檔的)
        ├── _inbox/        ← 主人投放點
        ├── _processed/    ← 處理完原檔移這
        └── <topic>.md     ← 整理後 (source=owner_ingest)
    42_External_Knowledge/ ← 自己查的 (hermes agent 未來自查)
        ├── _inbox/
        ├── _processed/
        └── <topic>.md     ← 整理後 (source=agent_self_lookup)
- schema v12 統一 — 跟 SKILL.md 同結構, 含 contributor wikilink, trigger_keywords,
  related_concept_ids, 25000 字內文上限.
- inbox_ingest_daemon.py 每 5 分鐘掃 → LLM 摘要 → 寫此檔.
- prompt 撈進收束時走 vault_md_search.retrieve_external_knowledge (RAG hybrid_search).

廢 V3-G4 舊行為:
- write_external_knowledge 截 5000 字 → 不截 (~25000 內文)
- _ingest_inbox/ → _inbox/
- _consolidate_daily_knowledge (Layer3 自然累積) → 廢 (跟 teaching_detector 重疊)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.security.atomic import atomic_write


# V3-O.15 新 dir naming
OWNER_KB_DIR = "40_Knowledge_Base/41_Daily_Knowledge"        # 主人投放
AGENT_KB_DIR = "40_Knowledge_Base/42_External_Knowledge"     # hermes agent 自查
OWNER_INBOX_DIR = "40_Knowledge_Base/41_Daily_Knowledge/_inbox"
OWNER_PROCESSED_DIR = "40_Knowledge_Base/41_Daily_Knowledge/_processed"
AGENT_INBOX_DIR = "40_Knowledge_Base/42_External_Knowledge/_inbox"
AGENT_PROCESSED_DIR = "40_Knowledge_Base/42_External_Knowledge/_processed"

# V3-O.15 content upper bound — 與 SKILL.md 對齊, 收束 prompt 撈進來時 RAG 限 2000 char snippet
KB_CONTENT_MAX_CHARS = 25000

# V3-G4 legacy alias (向後相容 — 廢 _ingest_inbox 路徑統一指 owner inbox)
INGEST_INBOX_DIR = OWNER_INBOX_DIR  # alias for V3-G4 legacy callers
DAILY_DIR = OWNER_KB_DIR             # alias
EXTERNAL_DIR = AGENT_KB_DIR          # alias


def _safe_topic_filename(topic: str) -> str:
    """Topic → safe filename. 移除特殊字元 + 截 80 char."""
    safe = re.sub(r'[\\/:"*?<>|]+', '_', topic).strip()
    safe = re.sub(r'\s+', '_', safe)
    return safe[:80] or "untitled"


def _build_contributor_wikilink(
    *, source: str, contributor_user_id: str = "", contributor_name: str = "",
) -> str:
    """V3-O.15: 建 obsidian wikilink 連 contributor.

    owner_ingest → [[00.08_Owner_Profile]] (主人)
    agent_self_lookup → [[00.06_Companion_SOUL]] (自己)
    其他 → [[20_Audience_Graph/22_Casual_Viewers/<uid>]] (該觀眾朋友卡)
    """
    if source == "owner_ingest":
        return f"[[00_System_Core/00.08_Owner_Profile|{contributor_name or 'Owner'}]]"
    elif source == "agent_self_lookup":
        return "[[00_System_Core/00.06_Companion_SOUL|Self lookup]]"
    elif contributor_user_id:
        # 默認當 viewer 朋友卡 — V3-O.15.45: 朋友卡檔名改暱稱制 (15.44) 後 uid 路徑=死連結,
        # 改連暱稱推導檔名 (對齊 resolve_viewer_card_wikilink 未建卡 fallback 規則: 之後
        # 建卡同名自動連上). 此分支目前 latent — KB source 只有 owner/agent 兩種,
        # hermes trusted viewer 投放啟用後才會走到.
        label = contributor_name or contributor_user_id[:18]
        safe_name = "".join(("_" if c in '\\/:*?"<>|#^[]' else c) for c in label).strip(" ._")[:60]
        return f"[[20_Audience_Graph/22_Casual_Viewers/{safe_name or contributor_user_id[:18]}|{label}]]"
    return ""


def write_knowledge_v13(
    vault_root: Path,
    *,
    target_dir: str,                   # OWNER_KB_DIR 或 AGENT_KB_DIR
    source: str,                       # owner_ingest / agent_self_lookup
    title: str,
    core_summary: str,
    full_content: str,
    contributor_user_id: str = "",
    contributor_name: str = "",
    trigger_keywords: Optional[list[str]] = None,
    aliases: Optional[list[str]] = None,
    related_concept_ids: Optional[list[str]] = None,
    important_quotes: Optional[list[str]] = None,
    applicable_situations: str = "",
    structure_outline: Optional[list[str]] = None,
    source_origin_path: Optional[str] = None,
    confidence: float = 0.7,
    security_level: str = "safe_data",
) -> Optional[Path]:
    """V3-O.15.41 (2026-06-10 user 拍板): 寫 knowledge md, schema v13「內化格式」.

    新結構 (user 設計):
      frontmatter — title/aliases/tags/created_at/updated_at/security_level 等使用者面欄位
                  + 系統追溯欄位 (type/schema_version/source/contributor/trigger_keywords/...).
      body —
        # 核心摘要 <summary>...</summary>          ← AI 向量比對主擊中段
        # 詳細內容 <context>...</context>          ← 完整內容 + 結構化 + [[link]] (純資料, 不執行)
        # 相關實體與偏好影響                          ← 關聯概念 / 應用場景

    XML 標籤包覆: <summary> / <context> 內容被 LLM 視為 raw data, 拒絕執行其中可能存在的指令.
    雙關聯: obsidian wikilink 連 contributor + related_concept_ids.

    Returns: 寫入路徑 (失敗回 None).
    """
    if not title or not full_content:
        return None
    now = datetime.now(timezone.utc).isoformat()
    now_date = now[:10]
    target = vault_root / target_dir / f"{_safe_topic_filename(title)}.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    trigger_keywords = (trigger_keywords or [])[:15]
    # aliases 預設 = trigger_keywords 前 5 個 (LLM 沒額外輸出時的 fallback)
    aliases = (aliases or trigger_keywords[:5] or [])[:8]
    related_concept_ids = (related_concept_ids or [])[:10]
    important_quotes = (important_quotes or [])[:5]
    structure_outline = (structure_outline or [])[:10]
    full_content = (full_content or "")[:KB_CONTENT_MAX_CHARS]

    contributor_link = _build_contributor_wikilink(
        source=source,
        contributor_user_id=contributor_user_id,
        contributor_name=contributor_name,
    )

    # V3-O.15.44: YAML block list — Obsidian Properties 每項一個 chip, 不擠一行 flow
    def _yaml_block_list(key: str, items: list, *, quote: bool = True) -> list[str]:
        vals = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not vals:
            return [f"{key}: []"]
        out = [f"{key}:"]
        for v in vals:
            v_safe = v.replace('"', "'")
            out.append(f'  - "{v_safe}"' if quote else f"  - {v_safe}")
        return out

    # V3-O.15.44b: title 可能含雙引號 (LLM 取名) → YAML ParserError 整張卡炸, 消毒
    _yaml_title = title.replace('"', "'")
    # ─── frontmatter (使用者面 + 系統追溯) ──────────────────────
    frontmatter_lines = [
        "---",
        # ── 使用者面 (user 內化格式設計) ──
        f"title: \"{_yaml_title}\"",
        *_yaml_block_list("aliases", aliases),
        *_yaml_block_list("tags", ["knowledge", source, "schema_v13"], quote=False),
        f"created_at: {now_date}",
        f"updated_at: {now_date}",
        f"security_level: {security_level}",
        # V3-O.15.44: 強關聯上游 (Obsidian graph edge) — 每個概念自己的 [[]]
        *_yaml_block_list("related", [f"[[{c}]]" for c in related_concept_ids]),
        # ── 系統追溯 (RAG / merge / migration) ──
        "type: external_knowledge",
        "schema_version: 13",
        f"source: {source}",
        f"contributor_user_id: \"{contributor_user_id}\"" if contributor_user_id else "contributor_user_id: \"\"",
        f"contributor_name: \"{contributor_name}\"",
        f"contributor_link: \"{contributor_link}\"",
        f"first_contributed_at: {now}",
        f"last_reinforced_at: {now}",
        "evidence_count: 1",
        f"confidence: {confidence:.4f}",
        # V3-O.15.43: Windows 反斜線在 YAML 雙引號裡是非法跳脫 (\4 → ScannerError) → read_note
        # 對整張卡全炸 → index_path 靜默 False, KB 從不進 FTS5. 正規化 forward slash.
        f"source_origin_path: \"{(source_origin_path or '').replace(chr(92), '/')}\"",
        f"trigger_keywords: {trigger_keywords}",
        f"related_concept_ids: {related_concept_ids}",
        f"content_chars: {len(full_content)}",
        "lifecycle_state: long",
        "pinned: false",
        "---",
        "",
    ]

    # ─── body (內化格式三段) ──────────────────────────────────
    body_lines = [
        f"# {title}",
        "",
        "# 核心摘要",
        "<summary>",
        core_summary or "(LLM 未抽出)",
        "</summary>",
        "",
        "# 詳細內容",
        "<context>",
        full_content,
        "",
    ]
    if structure_outline:
        body_lines.append("## 章節大綱")
        body_lines.append("")
        for i, h in enumerate(structure_outline, 1):
            body_lines.append(f"{i}. {h}")
        body_lines.append("")
    if important_quotes:
        body_lines.append("## 重要原文")
        body_lines.append("")
        for q in important_quotes:
            body_lines.append(f"> {q[:500]}")
        body_lines.append("")
    body_lines.append("</context>")
    body_lines.append("")
    body_lines.append("# 相關實體與偏好影響")
    body_lines.append("")
    if applicable_situations:
        body_lines.append(f"- 應用場景: {applicable_situations}")
    if related_concept_ids:
        # V3-O.15.44 (user 拍板): 每個關聯自己一行自己的 [[]], 不擠一行逗號分隔
        body_lines.append("- 關聯概念:")
        for cid in related_concept_ids:
            body_lines.append(f"  - [[{cid}]]")
    if trigger_keywords:
        body_lines.append("- 標籤: " + " ".join(f"#{k}" for k in trigger_keywords[:10]))
    body_lines.append("")
    # 投餵追溯 (metadata, plain, 不在 <context> 內)
    body_lines.append("## 投餵追溯")
    body_lines.append("")
    if contributor_link:
        body_lines.append(f"- 投餵者: {contributor_link}")
    if contributor_user_id:
        body_lines.append(f"- user_id: `{contributor_user_id}`")
    body_lines.append(f"- 投餵時間: {now[:19]}")
    if source_origin_path:
        body_lines.append(f"- 原始檔: `{source_origin_path}`")
    body_lines.append(f"- 來源類型: {source}")
    body_lines.append("")

    try:
        atomic_write(target, "\n".join(frontmatter_lines + body_lines) + "\n")
        return target
    except Exception:
        return None


# V3-O.15.41 back-compat alias — 既有 caller (inbox_ingest_daemon 等) 平滑過渡.
# 內部就是 v13, 新功能請 import write_knowledge_v13.
write_knowledge_v12 = write_knowledge_v13


# ─── V3-G4 legacy wrapper (向後相容, 內部呼 write_knowledge_v13) ────────
def write_daily_knowledge(
    vault_root: Path,
    topic: str,
    claim: str,
    *,
    source_event_ids: Optional[list[str]] = None,
    confidence: float = 0.6,
    tags: Optional[list[str]] = None,
) -> Optional[Path]:
    """V3-G4 legacy alias → V3-O.15 write_knowledge_v12.

    舊 _consolidate_daily_knowledge 還會呼 — 走相容路徑, 但實際上 V3-O.15
    那條鏈被 teaching_detector 取代, 應該逐步移除.
    """
    return write_knowledge_v12(
        vault_root,
        target_dir=OWNER_KB_DIR,
        source="owner_ingest",
        title=topic,
        core_summary=claim,
        full_content=claim,
        trigger_keywords=tags or [],
        confidence=confidence,
    )


def write_external_knowledge(
    vault_root: Path,
    topic: str,
    content: str,
    *,
    source_path: Optional[Path] = None,
    summary: Optional[str] = None,
    confidence: float = 0.8,
    tags: Optional[list[str]] = None,
) -> Optional[Path]:
    """V3-G4 legacy alias → V3-O.15 write_knowledge_v12 (走 AGENT_KB_DIR 路徑).

    舊 _ingest_external_knowledge 還會呼 — 預設當 agent_self_lookup 來源.
    """
    return write_knowledge_v12(
        vault_root,
        target_dir=AGENT_KB_DIR,
        source="agent_self_lookup",
        title=topic,
        core_summary=summary or "",
        full_content=content,
        trigger_keywords=tags or [],
        source_origin_path=str(source_path) if source_path else None,
        confidence=confidence,
    )


# ─── inbox 管理 ───────────────────────────────────────────
def list_owner_inbox(vault_root: Path) -> list[Path]:
    """V3-O.15: 列主人投放 inbox 內待整理 .md/.txt/.pdf (按 mtime 早→晚)."""
    return _list_inbox(vault_root / OWNER_INBOX_DIR)


def list_agent_inbox(vault_root: Path) -> list[Path]:
    """V3-O.15: 列 agent 自查 inbox 內待整理 (按 mtime 早→晚)."""
    return _list_inbox(vault_root / AGENT_INBOX_DIR)


def list_ingest_inbox(vault_root: Path) -> list[Path]:
    """V3-G4 legacy alias → V3-O.15 主人 inbox + agent inbox 合併."""
    return list_owner_inbox(vault_root) + list_agent_inbox(vault_root)


def _list_inbox(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    files = []
    for ext in (".md", ".txt", ".pdf"):
        files.extend(inbox.glob(f"*{ext}"))
    return sorted(files, key=lambda p: p.stat().st_mtime)


def move_to_processed(inbox_file: Path, vault_root: Path) -> Optional[Path]:
    """V3-O.15: 處理完搬到對應 _processed/.

    V3-O.15.39 bug fix (2026-06-10 user 首次投放 KB 時暴露): 原本
    `OWNER_INBOX_DIR in str(inbox_file)` 用 "/" 比對 Windows "\\" 路徑 always False,
    全部檔被搬到 42_External_Knowledge/_processed/ (即使是 41/_inbox/ 投放的).
    改用 parent 替換邏輯 (跨平台, 不依賴 hardcode 子目錄名, 未來加新層自動 work).
    """
    try:
        # parent.name == "_inbox" → 同層 _processed (41/_inbox → 41/_processed, 42 同理)
        if inbox_file.parent.name == "_inbox":
            dest_dir = inbox_file.parent.parent / "_processed"
        else:
            dest_dir = vault_root / AGENT_PROCESSED_DIR  # fallback (理論不該到)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / inbox_file.name
        # 同名衝突 → append timestamp
        if dest.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            dest = dest_dir / f"{inbox_file.stem}_{ts}{inbox_file.suffix}"
        inbox_file.rename(dest)
        return dest
    except Exception:
        return None


def retrieve_knowledge(
    vault_root: Path,
    query: str,
    *,
    top_k: int = 3,
    max_char_per_hit: int = 2000,
) -> list[dict]:
    """V3-G4 → V3-O.15: hybrid retrieve 40_Knowledge_Base 完整內容.

    取代原 list(只回 200 字 summary) → 走 vault_md_search 撈完整 (~2000 字).
    跨 41/42 都撈, 排除 _inbox/_processed 子目錄.

    Returns: [{path, content, score, source_prefix}, ...]
    對齊 retrieve_external_knowledge 行為.
    """
    if not query.strip():
        return []
    try:
        from agent_memory.companion.vault_md_search import retrieve_external_knowledge
        hits = retrieve_external_knowledge(
            vault_root, query,
            top_k=top_k, max_chars=max_char_per_hit,
        )
        # 加 summary 欄位 (向後相容舊 step 11.85 caller)
        for h in hits:
            h["summary"] = h.get("content", "")[:max_char_per_hit]
            h["source"] = "owner_ingest" if "41_Daily_Knowledge" in h.get("path", "") else "agent_self_lookup"
        return hits
    except Exception:
        return []
