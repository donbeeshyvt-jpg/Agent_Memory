"""Notion 發佈佇列（僅 Vault 側合約與落地檔案）。

呼叫 Notion 官方 API 的「真正發佈」不在此模組：由使用者另寫 Cursor Skill / 外部程序
輪詢 `status=pending` 的佇列檔並更新狀態即可。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

NOTION_QUEUE_DIR_RELATIVE = "11_AI_Mirror/external_ingest/notion_queue"
NOTION_QUEUE_EVENTS_RELATIVE = "11_AI_Mirror/ingestion_logs/notion_publish_queue.md"
NOTION_QUEUE_CONTRACT_RELATIVE = f"{NOTION_QUEUE_DIR_RELATIVE}/_QUEUE_CONTRACT.md"

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug_fragment(text: str, *, max_len: int = 40) -> str:
    raw = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    if not raw:
        return "item"
    return raw[:max_len].rstrip("-")


def _queue_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"nq-{stamp}-{uuid.uuid4().hex[:6]}"


def _dump_frontmatter(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def ensure_notion_queue_contract(vault_root: Path, *, overwrite: bool = False) -> Path:
    """寫入佇列合約說明（給人類與後續 Skill 對齊欄位語意）。"""

    root = Path(vault_root).expanduser().resolve()
    target = (root / NOTION_QUEUE_CONTRACT_RELATIVE).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    body = (
        "# Notion 發佈佇列合約\n\n"
        "本目錄由 `memory-cli notion-queue` 寫入 **`status: pending`** 的 Markdown。\n\n"
        "## 後續 Skill 職責\n\n"
        "- 輪詢或使用檔案監聽找出 `pending`。\n"
        "- 將內容映射到 Notion page / database。\n"
        "- 發佈成功後將同檔案 frontmatter 的 `status` 改為 `published`，並填入 `notion_target_id`（自建欄位，非核心必填）。\n"
        "## Frontmatter（schema_version 1）\n\n"
        "| 鍵 | 說明 |\n"
        "| --- | --- |\n"
        "| `notion_queue_id` | 佇列主鍵 |\n"
        "| `status` | `pending` / （Skill 維護）`published` 等 |\n"
        "| `schema_version` | 固定 `1`，日後演進由 Skill 與此文檔對齊 |\n"
        "| `queued_at` | UTC ISO |\n"
        "| `operator` | 操作者 id |\n"
        "| `title` | 短標題 |\n"
        "| `persona_hint` | 可选，建議發佈時關聯的人格 |\n"
        "| `target_hint` | placeholder，將來資料庫 / 父頁 id |\n"
        "| `tags` | string 列表 |\n"
        "| `priority` | 建議：`low`/`normal`/`high` |\n\n"
        f"- updated_at: `{_now_iso()}`\n"
    )
    atomic_write(target, body)
    return target


def _append_queue_event(vault_root: Path, payload: dict[str, Any]) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / NOTION_QUEUE_EVENTS_RELATIVE).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(
            target,
            "# notion_publish_queue\n\n"
            "> Notion 發佈佇列事件（僅紀錄入佇列；不包含 API 呼叫結果）。\n\n",
        )
    block = (
        f"## {payload.get('timestamp', _now_iso())} {payload.get('event', 'enqueue')}\n\n"
        f"- notion_queue_id: `{payload.get('notion_queue_id', '')}`\n"
        f"- relative_path: `{payload.get('relative_path', '')}`\n"
        f"- operator: `{payload.get('operator', '')}`\n"
        f"- title: {payload.get('title', '')}\n\n"
    )
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + block)
    return target


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.strip().startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = parts[2].lstrip("\n")
    return meta, body


def queue_notion_publish(
    vault_root: Path,
    *,
    title: str,
    body_md: str,
    operator: str = "user",
    persona_hint: str = "",
    target_hint: str = "",
    tags: list[str] | None = None,
    priority: str = "normal",
) -> dict[str, Any]:
    """寫入一筆 pending 佇列檔，並記錄台帳一行。"""

    root = Path(vault_root).expanduser().resolve()
    ensure_notion_queue_contract(root)

    notion_queue_id = _queue_id()
    tag_values = list(tags or [])
    frontmatter = {
        "notion_queue_id": notion_queue_id,
        "status": "pending",
        "schema_version": 1,
        "queued_at": _now_iso(),
        "operator": (operator or "user").strip() or "user",
        "title": (title or "").strip() or "(untitled)",
        "persona_hint": (persona_hint or "").strip(),
        "target_hint": (target_hint or "").strip() or "notion-target-placeholder",
        "tags": tag_values,
        "priority": (priority or "normal").strip() or "normal",
    }

    slug = _slug_fragment(frontmatter["title"])
    stamp = notion_queue_id.removeprefix("nq-").rsplit("-", 1)[0]
    fname = f"{stamp}-{slug}.md"
    rel_path = f"{NOTION_QUEUE_DIR_RELATIVE}/{fname}"
    note_path = (root / rel_path).resolve()
    note_path.parent.mkdir(parents=True, exist_ok=True)

    content = (
        "---\n"
        f"{_dump_frontmatter(frontmatter)}"
        "---\n\n"
        f"{body_md.strip()}\n"
    )
    atomic_write(note_path, content)

    _append_queue_event(
        root,
        {
            "timestamp": _now_iso(),
            "event": "enqueue",
            "notion_queue_id": notion_queue_id,
            "relative_path": rel_path,
            "operator": frontmatter["operator"],
            "title": frontmatter["title"],
        },
    )
    return {
        "notion_queue_id": notion_queue_id,
        "relative_path": rel_path,
        "status": frontmatter["status"],
    }


def list_notion_queue_items(
    vault_root: Path,
    *,
    status_filter: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """依修改時間列出佇列目錄內項目（輕量解析 frontmatter）。"""

    root = Path(vault_root).expanduser().resolve()
    queue_dir = (root / NOTION_QUEUE_DIR_RELATIVE).resolve()
    if not queue_dir.exists():
        return []

    md_files = [p for p in queue_dir.glob("*.md") if p.is_file() and not p.name.startswith("_")]
    md_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    rows: list[dict[str, Any]] = []
    for path in md_files:
        raw = path.read_text(encoding="utf-8")
        fm, body_preview = _split_frontmatter(raw)
        st = str(fm.get("status", "")).strip().lower()
        if status_filter and st != status_filter.strip().lower():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        rows.append(
            {
                "relative_path": rel,
                "notion_queue_id": str(fm.get("notion_queue_id", "")),
                "status": st or "?",
                "title": str(fm.get("title", path.stem)),
                "queued_at": str(fm.get("queued_at", "")),
                "body_preview": (body_preview.strip()[:200] + "…") if len(body_preview.strip()) > 200 else body_preview.strip(),
            },
        )
        if len(rows) >= int(limit):
            break
    return rows
