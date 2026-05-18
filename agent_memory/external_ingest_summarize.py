"""External ingest → Concepts 自動 summarize — R10 C38: 文獻吸收致用管道.

對應 MISSION §3.6「文獻吸收致用」核心承諾:
- Discord 拖檔 → C4 attachment_ingest 已落 11_AI_Mirror/external_ingest/
- Web 抓取 → web_research 已落 11_AI_Mirror/external_ingest/
- agent 對話可以主動 `[TOOL]memory.add` 升 Concepts (C3 已有)
- 但「自動 summarize → Concepts」管道一直缺 (MISSION §6 跑空風險清單) — 本 module 補完

對應 MISSION §5.4 LLM 介入時機:
- A. Sleep cycle (週) — curator weekly_deep 跑一次, 不阻擋對話
- 不會在 retrieve-time call LLM (紅線)

設計重點 (對齊 R9 C27/C28/C30 pattern):
- mock_response 給 e2e (跳真 LLM)
- LLM 不可用時 fallback (try/except)
- 每輪上限 max_files 控 cost
- .ai/external_ingest_state.json 持久化已處理檔, cooldown_days 內不重做
- 落地檔 10_Permanent/Concepts/ingested_<slug>_<date>.md
  - frontmatter pinned=false (使用者後續可改可標 pinned)
  - tags=[ingested, external, <llm-suggested>]
  - extras.source_path = 原始 11_AI_Mirror 路徑 (給 trace)
- 圖檔 / 二進位先 skip (C39 multipart vision 解了再加進來)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import (
    EtlStatus,
    Frontmatter,
    LifecycleState,
    MemoryNote,
    MemorySource,
    MemoryType,
    SecurityLevel,
)
from agent_memory.vault import ObsidianVaultAdapter

EXTERNAL_INGEST_DIR = "11_AI_Mirror/external_ingest"
CONCEPTS_DIR = "10_Permanent/Concepts"
STATE_RELATIVE_PATH = ".ai/external_ingest_state.json"

DEFAULT_MAX_FILES_PER_RUN = 5
DEFAULT_COOLDOWN_DAYS = 30
DEFAULT_MAX_TEXT_CHARS = 20_000  # 餵 LLM 上限 (avoid token blowup)

# 沿用 attachment_ingest 的副檔名分類 (lazy import 避循環)
_TEXT_EXTS_FALLBACK: frozenset[str] = frozenset({
    ".md", ".txt", ".rst", ".markdown",
    ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".xml", ".html", ".htm",
    ".ini", ".cfg", ".conf", ".env", ".properties",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rb", ".go", ".rs", ".java", ".cpp", ".c", ".h",
    ".cs", ".swift", ".kt", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".bat", ".cmd", ".sql", ".css", ".scss", ".less",
    ".log",
})

_SLUG_RE = re.compile(r"[^\w一-鿿\-]+", re.UNICODE)

# 不可見字元 — 對齊 security/scanner.py 偵測表, embed 進 Concept body 前一律 strip
# 避免外部檔的 BOM / ZWSP / RLO 觸發 scanner 把寫入擋掉
_INVISIBLE_CHARS_RE = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")


def _strip_invisible(text: str) -> str:
    return _INVISIBLE_CHARS_RE.sub("", text)


def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _safe_parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _slugify(text: str, *, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "untitled"
    return s[:max_len]


# ─── State IO ────────────────────────────────────────────────────────────────


def load_state(vault_root: Path) -> dict[str, Any]:
    """`.ai/external_ingest_state.json` schema:

    {
        "version": 1,
        "entries": {
            "<rel_source_path>": {
                "summarized_at": "ISO",
                "concept_path": "10_Permanent/Concepts/...",
                "mock_used": bool,
                "skipped_reason": "..." (only when skipped, not summarized)
            },
            ...
        }
    }
    """
    root = Path(vault_root).expanduser().resolve()
    p = root / STATE_RELATIVE_PATH
    if not p.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
        return {"version": 1, "entries": {}}
    except Exception:  # noqa: BLE001
        return {"version": 1, "entries": {}}


def save_state(vault_root: Path, state: dict[str, Any]) -> None:
    root = Path(vault_root).expanduser().resolve()
    p = root / STATE_RELATIVE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p, timeout=5.0):
        atomic_write(p, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


# ─── Scan external_ingest ────────────────────────────────────────────────────


def scan_external_ingest(vault_root: Path) -> list[dict[str, Any]]:
    """掃 11_AI_Mirror/external_ingest/ 抓 candidate file list.

    回每個檔的 {source_path, ext, size, kind: 'text'/'pdf'/'image'/'binary'}
    純列, 不讀內容.
    """
    root = Path(vault_root).expanduser().resolve()
    base = root / EXTERNAL_INGEST_DIR
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith("_"):
            continue
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        ext = p.suffix.lower()
        if ext in _TEXT_EXTS_FALLBACK:
            kind = "text"
        elif ext == ".pdf":
            kind = "pdf"
        elif ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            kind = "image"
        else:
            kind = "binary"
        out.append({
            "source_path": rel,
            "ext": ext,
            "size": p.stat().st_size,
            "kind": kind,
        })
    return out


def _read_source_text(vault_root: Path, source_path: str, kind: str, *, max_chars: int) -> tuple[bool, str, str]:
    """讀檔抽文字. 回 (ok, text, note).

    text: max_chars 後截斷.
    note: 'pdf_no_pypdf' / 'image_skipped' / 'binary_skipped' / 'read_error' / 'ok' / 'truncated'.
    """
    root = Path(vault_root).expanduser().resolve()
    path = root / source_path
    if not path.exists() or not path.is_file():
        return False, "", "not_found"
    if kind == "text":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            return False, "", f"read_error: {exc}"
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return True, text, "truncated" if truncated else "ok"
    if kind == "pdf":
        from agent_memory.attachment_ingest import _extract_pdf_text  # lazy
        ok, text, note = _extract_pdf_text(path)
        if not ok:
            return False, "", note or "pdf_extract_failed"
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return True, text, "truncated" if truncated else "ok"
    if kind == "image":
        return False, "", "image_skipped (留 C39 multipart vision)"
    return False, "", "binary_skipped (unsupported ext)"


# ─── LLM prompt + call ──────────────────────────────────────────────────────


def _build_summarize_prompt(source_path: str, text: str) -> str:
    """LLM prompt — 求 title / summary / key_concepts / tags JSON."""
    sample = text if len(text) <= 6000 else text[:6000] + "\n...(以下截斷)"
    return f"""你是 Agent_Memory 第二大腦助手 (V2 R10 C38). 使用者投了一份外部文獻進 vault, 需要你幫忙整理成可長期保存的「概念筆記」.

來源路徑 (相對 vault):
{source_path}

文獻內容 (擷取前段):
\"\"\"
{sample}
\"\"\"

請回 JSON, 結構如下 (其他欄位都不要):
{{
  "title": "短標題 (15 字內, 用文獻內容核心歸納, 中英文皆可)",
  "summary": "200-400 字摘要, 提煉核心觀點. 用繁中, 簡潔, 可 RAG 檢索的描述句",
  "key_concepts": ["概念 1", "概念 2", "..."],
  "tags": ["建議 tag 1", "建議 tag 2", "..."],
  "wikilinks_suggested": ["[[相關概念 A]]", "[[相關概念 B]]"]
}}

注意:
- 摘要要能讓未來 RAG 檢索命中 — 提煉名詞 + 動作
- key_concepts 限 3-7 個, 是文獻內提到的具體概念名
- tags 限 3-6 個, lowercase, 用底線 (例 machine_learning / rag / persona)
- wikilinks_suggested 是「你覺得這文獻該跟哪些概念互聯」的建議, 限 0-5 個
- 只回 JSON, 不要包 markdown code block, 不要多餘文字
"""


def _default_call_llm(prompt: str) -> dict[str, Any]:
    """Real LLM call — lazy import LLMClient. 不可用會拋 Exception."""
    from agent_memory.llm_client import LLMClient  # lazy

    client = LLMClient()
    result = client.generate(
        prompt=prompt,
        temperature=0.2,
        timeout_s=90.0,
        max_tokens=1500,
    )
    text = result.content.strip() if hasattr(result, "content") else str(result)
    # 抽 JSON (LLM 可能包 ```json ... ```)
    if "```" in text:
        for p in text.split("```"):
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                text = p
                break
    return json.loads(text)


# ─── Write Concept .md ──────────────────────────────────────────────────────


def _write_concept_note(
    vault_root: Path,
    *,
    source_path: str,
    llm_output: dict[str, Any],
    extracted_excerpt: str,
) -> str:
    """寫 10_Permanent/Concepts/ingested_<slug>_<date>.md, 回 vault-relative path."""
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)

    title = _strip_invisible(str(llm_output.get("title", "")).strip()) or Path(source_path).stem
    summary = _strip_invisible(str(llm_output.get("summary", "")).strip())
    key_concepts = [_strip_invisible(str(x)) for x in llm_output.get("key_concepts", []) if x]
    llm_tags = [_strip_invisible(str(x).strip().lower()) for x in llm_output.get("tags", []) if x]
    wikilinks = [_strip_invisible(str(x)) for x in llm_output.get("wikilinks_suggested", []) if x]

    today = _now_local().date().isoformat()
    slug = _slugify(title)
    target = f"{CONCEPTS_DIR}/ingested_{slug}_{today}.md"
    # 防衝 — 同日同 slug 加 -N
    candidate = target
    n = 1
    while adapter.read_note(candidate) is not None:
        candidate = f"{CONCEPTS_DIR}/ingested_{slug}_{today}-{n}.md"
        n += 1
    target = candidate

    # body
    body_lines: list[str] = []
    body_lines.append(f"# {title}")
    body_lines.append("")
    body_lines.append(f"> 自動由 R10 C38 LLM summarize 產出 ({_now_local_iso()})")
    body_lines.append(f"> 來源: `{source_path}`")
    body_lines.append("")
    body_lines.append("## 摘要")
    body_lines.append("")
    body_lines.append(summary or "_(LLM 未產 summary)_")
    body_lines.append("")
    if key_concepts:
        body_lines.append("## 核心概念")
        body_lines.append("")
        for kc in key_concepts:
            body_lines.append(f"- {kc}")
        body_lines.append("")
    if wikilinks:
        body_lines.append("## 建議互聯")
        body_lines.append("")
        for wl in wikilinks:
            body_lines.append(f"- {wl}")
        body_lines.append("")
    # 原文摘錄 (給後續 reflect 用) — strip 不可見字元避觸 scanner
    if extracted_excerpt:
        body_lines.append("## 原文摘錄 (前 2000 字)")
        body_lines.append("")
        body_lines.append("```")
        body_lines.append(_strip_invisible(extracted_excerpt[:2000]))
        body_lines.append("```")
        body_lines.append("")

    # frontmatter via Frontmatter / MemoryNote
    base_tags = ["ingested", "external"]
    seen = set(base_tags)
    for t in llm_tags:
        if t and t not in seen:
            base_tags.append(t)
            seen.add(t)

    stem = Path(source_path).stem
    aliases = [stem] if stem and stem != title else []
    aliases.append(title)  # 留 title 作 alias 也方便 RAG 命中

    fm = Frontmatter(
        type=MemoryType.CONCEPT,
        source=MemorySource.PROMOTION,
        tags=base_tags,
        aliases=aliases,
        agent="external-ingest-summarize-c38",
        schema_version=3,
        lifecycle_state=LifecycleState.LONG,
        mention_count=0,
        last_activity_at=_now_local_iso(),
        pinned=False,
        ai_ready=True,
        etl_status=EtlStatus.INTERNALISED,
        security_level=SecurityLevel.SAFE_DATA,
        extras={
            "source_path": source_path,
            "ingested_at": _now_local_iso(),
            "ingest_method": "llm_summarize_c38",
            "llm_title": title,
        },
    )
    note = MemoryNote(
        path=target,
        body="\n".join(body_lines),
        frontmatter=fm,
    )
    adapter.write_note(note)
    return target


# ─── Main entry ──────────────────────────────────────────────────────────────


def summarize_external_ingest(
    vault_root: Path,
    *,
    mock_response: Optional[dict[str, Any]] = None,
    max_files: int = DEFAULT_MAX_FILES_PER_RUN,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    """R10 C38 主入口 — 掃 11_AI_Mirror/external_ingest/ → LLM 摘 → 寫 Concepts/.

    Args:
        vault_root: vault 根
        mock_response: e2e 用 — dict 直接當每個檔的 LLM output (跳真 call)
                       共用一個 mock 給所有 candidate (e2e 簡化)
        max_files: 本輪最多處理幾個檔 (控 LLM cost)
        cooldown_days: 已處理檔幾天內不重做
        max_text_chars: 餵 LLM 的內容上限 (避免 token 爆)

    Returns dict:
        {
            "scanned_files": int,
            "candidates": [...],
            "summarized": [{source_path, concept_path, mock_used}, ...],
            "skipped": [{source_path, reason}, ...],
            "errors": [{source_path, error}, ...],
            "llm_called": bool,
            "mock_used": bool,
        }
    """
    root = Path(vault_root).expanduser().resolve()
    started_at = _now_local()
    result: dict[str, Any] = {
        "scanned_files": 0,
        "candidates": [],
        "summarized": [],
        "skipped": [],
        "errors": [],
        "llm_called": False,
        "mock_used": mock_response is not None,
        "started_at": started_at.isoformat(),
    }

    all_files = scan_external_ingest(root)
    result["scanned_files"] = len(all_files)
    if not all_files:
        return result

    state = load_state(root)
    entries = state.setdefault("entries", {})
    cutoff = started_at - timedelta(days=cooldown_days)

    candidates: list[dict[str, Any]] = []
    for f in all_files:
        sp = f["source_path"]
        if f["kind"] in ("image", "binary"):
            result["skipped"].append({"source_path": sp, "reason": f"unsupported_kind: {f['kind']}"})
            continue
        prev = entries.get(sp)
        if prev:
            done_at = _safe_parse_iso(prev.get("summarized_at"))
            if done_at and done_at.astimezone() >= cutoff:
                result["skipped"].append({"source_path": sp, "reason": "in_cooldown"})
                continue
        candidates.append(f)
        if len(candidates) >= max_files:
            break

    result["candidates"] = [c["source_path"] for c in candidates]

    if not candidates:
        return result

    for f in candidates:
        sp = f["source_path"]
        ok, text, note = _read_source_text(root, sp, f["kind"], max_chars=max_text_chars)
        if not ok:
            result["errors"].append({"source_path": sp, "error": f"read_failed: {note}"})
            # 標進 state 但 skipped, 避免下次再 retry 永遠失敗的檔
            entries[sp] = {
                "summarized_at": _now_local_iso(),
                "concept_path": None,
                "mock_used": False,
                "skipped_reason": f"read_failed: {note}",
            }
            continue

        if mock_response is not None:
            llm_output = mock_response
        else:
            try:
                prompt = _build_summarize_prompt(sp, text)
                llm_output = _default_call_llm(prompt)
                result["llm_called"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"source_path": sp, "error": f"llm_call_failed: {exc}"})
                # 不寫 state — 下週 retry
                continue

        if not isinstance(llm_output, dict):
            result["errors"].append({"source_path": sp, "error": "llm_output_not_dict"})
            continue

        try:
            concept_path = _write_concept_note(
                root,
                source_path=sp,
                llm_output=llm_output,
                extracted_excerpt=text,
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"source_path": sp, "error": f"write_failed: {exc}"})
            continue

        entries[sp] = {
            "summarized_at": _now_local_iso(),
            "concept_path": concept_path,
            "mock_used": mock_response is not None,
        }
        result["summarized"].append({
            "source_path": sp,
            "concept_path": concept_path,
            "mock_used": mock_response is not None,
        })

    save_state(root, state)
    result["ended_at"] = _now_local_iso()
    return result
