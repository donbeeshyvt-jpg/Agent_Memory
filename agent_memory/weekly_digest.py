"""Weekly digest — 自動產出「這週吸收了什麼」摘要 (R8 C25).

對應 MISSION §3.7 「歸納已吸收的知識」+ 使用者願景「也能幫使用者歸納出已經吸收過的知識」.

由 curator weekly deep run 順帶觸發 — 每週產一個 markdown digest 寫到:
    11_AI_Mirror/ingestion_logs/weekly_digest/<YYYY-WW>.md

內容:
- 本週 daily_flush 數
- 本週新建 Mid_Term entity 數 + 列表
- 本週升長期 (promote_midterm_to_long) 數 + 列表
- 本週 stale / archive 數
- 本週升 Skill 數
- 本週 pending gap / pending skill suggestions 數
- 本週 umbrella consolidate 數
- 簡短 narrative (基於統計, 不依賴 LLM)

呈現方式 (對齊 MISSION §3.1 對話驅動):
- digest 寫進 vault (Obsidian 可開)
- chat_runtime 在新 chat 開頭, 若有「上週新 digest 還沒呈現過」就貼 footer 給使用者
- .ai/weekly_digest_state.json 記「最後呈現給使用者的 digest week_id」

不依賴 LLM (省 token), 純統計 + 模板.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import LifecycleState
from agent_memory.vault import ObsidianVaultAdapter

WEEKLY_DIGEST_DIR_RELATIVE = "11_AI_Mirror/ingestion_logs/weekly_digest"
WEEKLY_DIGEST_STATE_RELATIVE = ".ai/weekly_digest_state.json"
PROMOTION_EVENTS_RELATIVE = "11_AI_Mirror/ingestion_logs/promotion_events.md"
CURATOR_RUNS_RELATIVE = "11_AI_Mirror/ingestion_logs/curator_runs.jsonl"


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _now_local_iso() -> str:
    return _now_local().isoformat()


def week_id_of(dt: datetime) -> str:
    """`YYYY-WW` ISO week id (本機時區)."""

    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def current_week_id() -> str:
    return week_id_of(_now_local())


# ─── State (last shown digest) ───────────────────────────────────────────────


def load_digest_state(vault_root: Path) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    path = root / WEEKLY_DIGEST_STATE_RELATIVE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_digest_state(vault_root: Path, state: dict[str, Any]) -> None:
    root = Path(vault_root).expanduser().resolve()
    path = root / WEEKLY_DIGEST_STATE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, timeout=5.0):
        atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


# ─── 統計掃描 ────────────────────────────────────────────────────────────────


def _count_recent_daily_flushes(vault_root: Path, since: datetime) -> int:
    root = Path(vault_root).expanduser().resolve()
    flush_dir = root / "11_AI_Mirror/ingestion_logs/daily_flush"
    if not flush_dir.exists():
        return 0
    count = 0
    for path in flush_dir.glob("*.md"):
        if not path.is_file() or path.name.startswith("_"):
            continue
        if datetime.fromtimestamp(path.stat().st_mtime).astimezone() >= since:
            count += 1
    return count


def _scan_recent_midterm(vault_root: Path, since: datetime) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    mid_dir = root / "10_Permanent/Mid_Term"
    if not mid_dir.exists():
        return {"new_entities": [], "current_active_count": 0}
    adapter = ObsidianVaultAdapter(root)
    new_entities: list[dict[str, Any]] = []
    active = 0
    for p in sorted(mid_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        if note.frontmatter.lifecycle_state == LifecycleState.MID:
            active += 1
        created = note.frontmatter.created.astimezone() if note.frontmatter.created else None
        if created and created >= since:
            new_entities.append({
                "entity_id": p.stem,
                "mention_count": note.frontmatter.mention_count,
                "lifecycle_state": note.frontmatter.lifecycle_state.value,
                "pinned": note.frontmatter.pinned,
            })
    return {"new_entities": new_entities, "current_active_count": active}


_PROMOTION_BLOCK_HEADING_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}T\S+)\s+(promotion|demote)\b", re.MULTILINE)


def _scan_promotion_events(vault_root: Path, since: datetime) -> dict[str, Any]:
    """讀 promotion_events.md 抓本週升降事件 (粗略統計, 不 parse 細節)."""

    root = Path(vault_root).expanduser().resolve()
    path = root / PROMOTION_EVENTS_RELATIVE
    if not path.exists():
        return {"promote_count": 0, "demote_count": 0}
    text = path.read_text(encoding="utf-8")
    promote = 0
    demote = 0
    for m in _PROMOTION_BLOCK_HEADING_RE.finditer(text):
        ts_str, kind = m.group(1), m.group(2)
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if ts.astimezone() >= since:
            if kind == "promotion":
                promote += 1
            else:
                demote += 1
    return {"promote_count": promote, "demote_count": demote}


def _scan_recent_skill_promotions(vault_root: Path, since: datetime) -> list[str]:
    """從 pending_skill_suggestions.json 抓本週已 promoted 的."""

    root = Path(vault_root).expanduser().resolve()
    path = root / ".ai/pending_skill_suggestions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []
    promoted: list[str] = []
    for s in data:
        if not isinstance(s, dict):
            continue
        promoted_at_str = s.get("promoted_at", "")
        if not promoted_at_str:
            continue
        try:
            promoted_at = datetime.fromisoformat(promoted_at_str)
        except (ValueError, TypeError):
            continue
        if promoted_at.astimezone() >= since:
            promoted.append(s.get("entity_id", "?"))
    return promoted


def _count_pending(vault_root: Path) -> dict[str, int]:
    root = Path(vault_root).expanduser().resolve()
    out = {"pending_skill_suggestions": 0, "pending_user_gaps": 0}
    sp = root / ".ai/pending_skill_suggestions.json"
    if sp.exists():
        try:
            data = json.loads(sp.read_text(encoding="utf-8")) or []
            out["pending_skill_suggestions"] = sum(
                1 for s in data
                if isinstance(s, dict) and not s.get("dismissed_at") and not s.get("promoted_to")
            )
        except Exception:  # noqa: BLE001
            pass
    gp = root / ".ai/pending_user_gaps.json"
    if gp.exists():
        try:
            data = json.loads(gp.read_text(encoding="utf-8")) or []
            out["pending_user_gaps"] = sum(
                1 for g in data
                if isinstance(g, dict) and not g.get("dismissed_at") and not g.get("resolved_at")
            )
        except Exception:  # noqa: BLE001
            pass
    return out


# ─── Generate digest ──────────────────────────────────────────────────────────


def generate_weekly_digest(vault_root: Path) -> dict[str, Any]:
    """產出本週 digest, 寫 markdown + 更 state. 同週重跑會覆寫."""

    root = Path(vault_root).expanduser().resolve()
    now = _now_local()
    week_id = current_week_id()
    since = now - timedelta(days=7)

    flushes = _count_recent_daily_flushes(root, since)
    mid_scan = _scan_recent_midterm(root, since)
    prom = _scan_promotion_events(root, since)
    skills = _scan_recent_skill_promotions(root, since)
    pending = _count_pending(root)

    digest_dir = root / WEEKLY_DIGEST_DIR_RELATIVE
    digest_dir.mkdir(parents=True, exist_ok=True)
    digest_path = digest_dir / f"{week_id}.md"

    new_entities = mid_scan["new_entities"]
    new_entity_lines = "\n".join(
        f"- `{e['entity_id']}` (mention={e['mention_count']}, state={e['lifecycle_state']})"
        for e in new_entities
    ) or "- (本週沒新中期 entity)"
    skill_lines = ("\n".join(f"- `{eid}` → `Skills/{eid}/`" for eid in skills)) or "- (本週沒升 Skill)"

    body = f"""# 週進化摘要 {week_id}

> 由 R8 C25 curator weekly deep 自動產生. 對應 MISSION §3.7 主動歸納.
> 統計區間: {since.date().isoformat()} → {now.date().isoformat()}

## 對話量
- daily_flush 數 (本週對話的「日」)：**{flushes}**

## 記憶累積
- 中期 (Mid_Term) 目前活躍 entity 數：**{mid_scan['current_active_count']}**
- 本週**新建**中期 entity 數：**{len(new_entities)}**

### 新建中期 entity
{new_entity_lines}

## 升降格事件 (本週)
- 升格 (短→中 / 中→長 / 升 Skill 等)：**{prom['promote_count']}** 條
- 降級 (stale / archive)：**{prom['demote_count']}** 條
- 升 **Skill** 數：**{len(skills)}**

### 本週升 Skill
{skill_lines}

## 待你決定 (pending)
- 等你回應的 **skill 升格提議**：**{pending['pending_skill_suggestions']}**
- 等你回答的 **USER.md gap 提問**：**{pending['pending_user_gaps']}**

## 簡短結論
{_build_narrative(flushes, len(new_entities), prom['promote_count'], len(skills), pending)}

---
*產生時間：{now.isoformat()}*
"""
    atomic_write(digest_path, body)

    state = load_digest_state(root)
    state["latest_digest_week"] = week_id
    state["latest_digest_generated_at"] = now.isoformat()
    state["latest_digest_path"] = str(digest_path.relative_to(root)).replace("\\", "/")
    save_digest_state(root, state)

    return {
        "week_id": week_id,
        "digest_path": state["latest_digest_path"],
        "stats": {
            "flushes": flushes,
            "new_midterm_entities": len(new_entities),
            "current_active_midterm": mid_scan["current_active_count"],
            "promote_events": prom["promote_count"],
            "demote_events": prom["demote_count"],
            "skill_promotions": len(skills),
            "pending_skill_suggestions": pending["pending_skill_suggestions"],
            "pending_user_gaps": pending["pending_user_gaps"],
        },
    }


def _build_narrative(flushes: int, new_mid: int, promotes: int, skills: int, pending: dict[str, int]) -> str:
    parts: list[str] = []
    if flushes == 0:
        parts.append("本週沒對話過 — agent 沒新東西可吸收.")
    elif flushes < 3:
        parts.append(f"本週對話量偏少 ({flushes} 天).")
    else:
        parts.append(f"本週對話累積 **{flushes} 天**, 活躍度正常.")

    if new_mid >= 5:
        parts.append(f"中期記憶**大幅成長** (+{new_mid}), 表示有多個新主題在累積.")
    elif new_mid > 0:
        parts.append(f"中期記憶溫和成長 (+{new_mid}).")
    else:
        parts.append("中期記憶沒新 entity, 多在累計既有概念.")

    if skills >= 1:
        parts.append(f"成功升 **{skills} 個 Skill** — agent 自我進化見效.")
    if promotes >= 3:
        parts.append(f"升降格事件 {promotes} 條 — 記憶 lifecycle 持續流動.")

    pending_skill = pending.get("pending_skill_suggestions", 0)
    pending_gap = pending.get("pending_user_gaps", 0)
    if pending_skill > 0 or pending_gap > 0:
        parts.append(
            f"還有 **{pending_skill} 個 skill 升格提議** + **{pending_gap} 個 USER.md gap 提問**等你回應."
        )

    return " ".join(parts)


# ─── chat_runtime hook: 開頭呈現 ─────────────────────────────────────────────


def pick_undelivered_digest_footer(vault_root: Path) -> Optional[str]:
    """若有 latest digest 還沒呈現給使用者就回 footer (chat_runtime 開頭加).

    回 None 表示無新 digest / 已呈現過.
    """

    root = Path(vault_root).expanduser().resolve()
    state = load_digest_state(root)
    latest_week = state.get("latest_digest_week")
    last_shown = state.get("last_shown_week")
    if not latest_week:
        return None
    if last_shown == latest_week:
        return None
    digest_rel = state.get("latest_digest_path", "")
    stats = {}
    digest_abs = root / digest_rel if digest_rel else None
    if digest_abs and digest_abs.exists():
        # 從檔抓粗略統計 (避免重新 scan)
        try:
            text = digest_abs.read_text(encoding="utf-8")
            # 簡單抽幾個關鍵數字
            for line in text.splitlines():
                if "daily_flush 數" in line and "**" in line:
                    stats["flushes"] = line
                if "新建**中期 entity 數" in line and "**" in line:
                    stats["new_mid"] = line
                if "升 **Skill** 數" in line and "**" in line:
                    stats["skills"] = line
        except Exception:  # noqa: BLE001
            pass

    footer = (
        "\n\n---\n"
        f"📊 上週進化摘要 ({latest_week}) — 完整: `{digest_rel}`\n"
    )
    if stats:
        for v in stats.values():
            footer += f"  {v}\n"

    # 標 last_shown
    state["last_shown_week"] = latest_week
    state["last_shown_at"] = _now_local_iso()
    save_digest_state(root, state)
    return footer
