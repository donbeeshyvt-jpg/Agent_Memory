"""Recent Updates index — R10 C37: Obsidian-visible「最近改了什麼」.

把分散在 .ai/ + 11_AI_Mirror/ingestion_logs/ 內的事件聚成一份使用者開 Obsidian 就看
得到的 markdown, 對齊 MISSION §3.2「使用者用 Obsidian 也能看」.

對應 HANDOFF §3.2 R10 候選清單第 3 條「09_Index/Recent_Updates.md 自動更新」.

設計重點:
- 寫到 00_System/09_Index/Recent_Updates.md (09_Index 已在 _SKELETON_DIRS, 不另建)
- frontmatter `pinned=true + lifecycle_state=long` 讓 curator 不會自動降級或封存
- 每次跑 overwrite (不 append), 只看「最近 N 天」(預設 7d), 不無限長
- 純讀 promotion_events.md + curator_runs.jsonl + .ai/pending_*.json + Mid_Term/
  跟 R8 weekly_digest 共用 scan helper, 避免邏輯重複
- 沒 LLM call, 純機械聚合 — 對齊 D2 (C37 規格中) 「不再加 LLM call, R9 已飽和」

跟 R8 weekly_digest 的關係:
- weekly_digest = log style snapshot, 落 11_AI_Mirror/ingestion_logs/weekly_digest/<YYYY-WW>.md, 一週一份
- Recent_Updates  = index style persistent view, 一直只有 1 份, overwrite, 給使用者 pin in Obsidian

兩者讀的來源大致重疊, 但用途互補.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

RECENT_UPDATES_RELATIVE_PATH = "00_System/09_Index/03_Recent_Updates.md"
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_ENTRIES_PER_SECTION = 20


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _safe_parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _gather_recent_midterm_entities(
    vault_root: Path,
    since: datetime,
    *,
    max_entries: int,
) -> list[dict[str, Any]]:
    """掃 10_Permanent/Mid_Term/ 抓 created/updated 在 since 之後的 entity."""
    root = Path(vault_root).expanduser().resolve()
    mid_dir = root / "10_Permanent/Mid_Term"
    if not mid_dir.exists():
        return []
    from agent_memory.types import LifecycleState
    from agent_memory.vault import ObsidianVaultAdapter

    adapter = ObsidianVaultAdapter(root)
    out: list[dict[str, Any]] = []
    for p in sorted(mid_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        created = note.frontmatter.created.astimezone() if note.frontmatter.created else None
        updated = note.frontmatter.updated.astimezone() if note.frontmatter.updated else created
        anchor = updated or created
        if anchor is None or anchor < since:
            continue
        out.append({
            "entity_id": p.stem,
            "mention_count": note.frontmatter.mention_count,
            "lifecycle_state": note.frontmatter.lifecycle_state.value
            if isinstance(note.frontmatter.lifecycle_state, LifecycleState)
            else str(note.frontmatter.lifecycle_state),
            "pinned": note.frontmatter.pinned,
            "anchor_at": anchor.isoformat(),
        })
    out.sort(key=lambda e: e.get("anchor_at", ""), reverse=True)
    return out[:max_entries]


def _gather_recent_promotion_events(
    vault_root: Path,
    since: datetime,
    *,
    max_entries: int,
) -> list[dict[str, Any]]:
    """讀 11_AI_Mirror/ingestion_logs/promotion_events.md heading 提 raw event list."""
    import re

    root = Path(vault_root).expanduser().resolve()
    path = root / "11_AI_Mirror/ingestion_logs/promotion_events.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}T\S+)\s+(promotion|demote)\s+(\S+)", re.MULTILINE)
    out: list[dict[str, Any]] = []
    for m in pattern.finditer(text):
        ts = _safe_parse_iso(m.group(1))
        if ts is None or ts.astimezone() < since:
            continue
        out.append({
            "timestamp": m.group(1),
            "kind": m.group(2),
            "target": m.group(3),
        })
    out.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return out[:max_entries]


def _gather_recent_skill_promotions(
    vault_root: Path,
    since: datetime,
    *,
    max_entries: int,
) -> list[dict[str, Any]]:
    """讀 .ai/pending_skill_suggestions.json 抓 promoted_at 在 since 之後."""
    root = Path(vault_root).expanduser().resolve()
    path = root / ".ai/pending_skill_suggestions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for s in data:
        if not isinstance(s, dict):
            continue
        promoted_at = _safe_parse_iso(s.get("promoted_at"))
        if promoted_at is None or promoted_at.astimezone() < since:
            continue
        out.append({
            "entity_id": s.get("entity_id", ""),
            "promoted_to": s.get("promoted_to", ""),
            "promoted_at": s.get("promoted_at", ""),
            "summary": str(s.get("summary", ""))[:100],
        })
    out.sort(key=lambda e: e.get("promoted_at", ""), reverse=True)
    return out[:max_entries]


def _gather_recent_curator_runs(
    vault_root: Path,
    since: datetime,
    *,
    max_entries: int,
) -> list[dict[str, Any]]:
    """讀 11_AI_Mirror/ingestion_logs/curator_runs.jsonl 抓最近跑過幾輪."""
    root = Path(vault_root).expanduser().resolve()
    path = root / "11_AI_Mirror/ingestion_logs/curator_runs.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = _safe_parse_iso(entry.get("started_at"))
        if ts is None or ts.astimezone() < since:
            break  # jsonl append-only 倒讀, 過了 since 就停
        out.append({
            "started_at": entry.get("started_at", ""),
            "mode": entry.get("mode", ""),
            "steps": entry.get("steps", []),
            "errors": entry.get("errors", []),
        })
        if len(out) >= max_entries:
            break
    return out


def _gather_recent_weekly_digests(
    vault_root: Path,
    since: datetime,
    *,
    max_entries: int,
) -> list[dict[str, Any]]:
    """列 11_AI_Mirror/ingestion_logs/weekly_digest/ 內 since 之後產的 digest 檔."""
    root = Path(vault_root).expanduser().resolve()
    digest_dir = root / "11_AI_Mirror/ingestion_logs/weekly_digest"
    if not digest_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(digest_dir.glob("*.md"), reverse=True):
        if p.name.startswith("_"):
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        if mtime < since:
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        out.append({
            "week_id": p.stem,
            "path": rel,
            "generated_at": mtime.isoformat(),
        })
        if len(out) >= max_entries:
            break
    return out


def build_recent_updates_markdown(
    vault_root: Path,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_entries_per_section: int = DEFAULT_MAX_ENTRIES_PER_SECTION,
) -> str:
    """純函式產 markdown 字串, 不寫檔. 給 generator + e2e 各自接."""
    now = _now_local()
    since = now - timedelta(days=lookback_days)

    midterm = _gather_recent_midterm_entities(vault_root, since, max_entries=max_entries_per_section)
    promotions = _gather_recent_promotion_events(vault_root, since, max_entries=max_entries_per_section)
    skills = _gather_recent_skill_promotions(vault_root, since, max_entries=max_entries_per_section)
    curator_runs = _gather_recent_curator_runs(vault_root, since, max_entries=max_entries_per_section)
    digests = _gather_recent_weekly_digests(vault_root, since, max_entries=max_entries_per_section)

    lines: list[str] = []

    # Frontmatter — pinned=true 讓 curator 不會自動降級 / archive
    lines.append("---")
    lines.append("schema_version: 3")
    lines.append("lifecycle_state: long")
    lines.append("mention_count: 0")
    lines.append("pinned: true")
    lines.append("ai_ready: true")
    lines.append("etl_status: internalised")
    lines.append("security_level: low")
    lines.append(f"updated: '{now.isoformat()}'")
    lines.append("tags: [auto_generated, index, recent_updates]")
    lines.append("aliases: [最近更新, Recent Updates]")
    lines.append("---")
    lines.append("")
    lines.append("# 最近更新 (Recent Updates)")
    lines.append("")
    lines.append(f"> 自動產出: {now.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append(f"> 回看視窗: 最近 **{lookback_days}** 天")
    lines.append("> 來源: curator daily/weekly 跑完自動 overwrite 本檔 (C37)")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 1 — 新累積的 Mid_Term entity
    lines.append("## 新增 / 更新的 Mid_Term entity")
    lines.append("")
    if midterm:
        lines.append("| Entity | mention | state | pinned | 最近活動 |")
        lines.append("|---|---|---|---|---|")
        for e in midterm:
            pin = "📌" if e.get("pinned") else ""
            lines.append(
                f"| [[Mid_Term/{e['entity_id']}]] "
                f"| {e.get('mention_count', 0)} "
                f"| `{e.get('lifecycle_state', '')}` "
                f"| {pin} "
                f"| {e.get('anchor_at', '')} |"
            )
        lines.append("")
    else:
        lines.append(f"_最近 {lookback_days} 天沒有新累積的 Mid_Term entity._")
        lines.append("")

    # Section 2 — 升降格事件
    lines.append("## 升降格事件 (promotion / demote)")
    lines.append("")
    if promotions:
        lines.append("| 時間 | 類型 | 對象 |")
        lines.append("|---|---|---|")
        for ev in promotions:
            kind = ev.get("kind", "")
            target = ev.get("target", "")
            emoji = "⬆️" if kind == "promotion" else "⬇️"
            lines.append(f"| {ev.get('timestamp', '')} | {emoji} {kind} | `{target}` |")
        lines.append("")
    else:
        lines.append(f"_最近 {lookback_days} 天沒有升降格事件._")
        lines.append("")

    # Section 3 — Skill 升格
    lines.append("## Skill 升格")
    lines.append("")
    if skills:
        for s in skills:
            entity = s.get("entity_id", "")
            promoted_to = s.get("promoted_to", "")
            promoted_at = s.get("promoted_at", "")
            summary = s.get("summary", "")
            lines.append(f"- **{entity}** → `{promoted_to}` ({promoted_at})")
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")
    else:
        lines.append(f"_最近 {lookback_days} 天沒有新升 Skill._")
        lines.append("")

    # Section 4 — Weekly digest pointer
    lines.append("## 本週 / 近期 Weekly digest")
    lines.append("")
    if digests:
        for d in digests:
            lines.append(f"- [[{d['path']}|{d['week_id']}]] — 產出 {d.get('generated_at', '')}")
        lines.append("")
    else:
        lines.append(f"_最近 {lookback_days} 天沒有 weekly digest (curator weekly_deep 跑了才會產)._")
        lines.append("")

    # Section 5 — curator 跑過幾輪
    lines.append("## Curator 跑過幾輪")
    lines.append("")
    if curator_runs:
        light_count = sum(1 for r in curator_runs if r.get("mode") == "light_2h" or r.get("mode") == "daily_light")
        medium_count = sum(1 for r in curator_runs if r.get("mode") == "medium_24h" or r.get("mode") == "daily_light")
        weekly_count = sum(1 for r in curator_runs if r.get("mode") == "weekly_deep")
        lines.append(f"- 最近 {lookback_days} 天: 共 **{len(curator_runs)}** 輪 (light={light_count} / medium={medium_count} / weekly={weekly_count})")
        # 列最近 5 條
        lines.append("")
        lines.append("| 開始時間 | mode | steps | errors |")
        lines.append("|---|---|---|---|")
        for r in curator_runs[:5]:
            steps = r.get("steps") or []
            errors = r.get("errors") or []
            steps_str = str(len(steps)) if isinstance(steps, list) else "?"
            errors_str = str(len(errors)) if isinstance(errors, list) else "?"
            lines.append(f"| {r.get('started_at', '')} | `{r.get('mode', '')}` | {steps_str} | {errors_str} |")
        lines.append("")
    else:
        lines.append(f"_最近 {lookback_days} 天 curator 還沒跑過_")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 📂 詳細 log: `11_AI_Mirror/ingestion_logs/curator_runs.jsonl` / `promotion_events.md`")
    lines.append("> 📊 Weekly digest: `11_AI_Mirror/ingestion_logs/weekly_digest/`")
    lines.append("> ⚙️ pending pool: 走 `python -m agent_memory pending-overview` (R10 C36)")
    lines.append("")

    return "\n".join(lines)


def write_recent_updates(
    vault_root: Path,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_entries_per_section: int = DEFAULT_MAX_ENTRIES_PER_SECTION,
) -> dict[str, Any]:
    """跑 build_recent_updates_markdown 並 atomic 寫到 00_System/09_Index/Recent_Updates.md."""
    root = Path(vault_root).expanduser().resolve()
    md = build_recent_updates_markdown(
        root,
        lookback_days=lookback_days,
        max_entries_per_section=max_entries_per_section,
    )
    path = root / RECENT_UPDATES_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, timeout=5.0):
        atomic_write(path, md)
    return {
        "path": RECENT_UPDATES_RELATIVE_PATH,
        "bytes_written": len(md.encode("utf-8")),
        "lookback_days": lookback_days,
    }
