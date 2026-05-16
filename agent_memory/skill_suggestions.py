"""Skill 升格提議系統 — 對話中主動提議 (R7 C20b).

curator weekly deep run 時 scan Mid_Term/ tag 含 procedure/workflow/steps 的檔,
寫到 .ai/pending_skill_suggestions.json. chat_runtime 對話末端最多貼 1 個提議
給使用者; 使用者下一輪 input 開頭 keyword (升/好/跳過/不要) → 觸發升格或 dismiss.

對應 V2_Round7 §5.5 + 使用者拍板「不要 menu 要對話中問」.

跟 menu [P][S] (power user 批量) 並存:
- 預設路徑: 對話中提議 (此檔)
- 進階路徑: menu [P][S] 批量看 pending (power user)

設計重點:
- 每 response 最多 1 個提議, 不轟炸使用者
- 同 entity 7 天 cooldown (避免重複)
- 7 天無回應自動 dismiss
- parse 使用者 intent 只在「短輸入 + 純 keyword 開頭」時觸發 (避免「升職很爽」誤判)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import Frontmatter, LifecycleState, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

PENDING_SKILL_SUGGESTIONS_RELATIVE_PATH = ".ai/pending_skill_suggestions.json"
MIDTERM_DIR = "10_Permanent/Mid_Term"
SKILLS_DIR = "00_System/Skills"

# 升格信號 tag — scan 時看 Mid_Term frontmatter.tags 是否含
SKILL_SIGNAL_TAGS: frozenset[str] = frozenset({"procedure", "workflow", "steps", "skill_candidate", "playbook"})

# 使用者回應 parse pattern (精準 keyword 開頭)
_ACCEPT_RE = re.compile(r"^(?:升格|升|好的|好|可以|ok|對|yes|y|建)", re.IGNORECASE)
_DECLINE_RE = re.compile(r"^(?:跳過|不要|不用|不|別|no|n|skip)", re.IGNORECASE)

# 短輸入 intent 上限 — 超過視為一般對話不 parse intent
_MAX_INTENT_INPUT_LEN = 10

_SLUG_RE = re.compile(r"[^\w一-鿿\-]+", re.UNICODE)


def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _normalize_skill_id(raw: str) -> str:
    cleaned = _SLUG_RE.sub("-", raw.strip().lower()).strip("-")
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rstrip("-")
    return cleaned or "skill-candidate"


# ─── IO: pending json ────────────────────────────────────────────────────────


def load_pending(vault_root: Path) -> list[dict[str, Any]]:
    """讀 .ai/pending_skill_suggestions.json. 不存在 / 壞 → 回 []."""

    root = Path(vault_root).expanduser().resolve()
    path = root / PENDING_SKILL_SUGGESTIONS_RELATIVE_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []
    except Exception:  # noqa: BLE001
        return []


def save_pending(vault_root: Path, suggestions: list[dict[str, Any]]) -> None:
    root = Path(vault_root).expanduser().resolve()
    path = root / PENDING_SKILL_SUGGESTIONS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, timeout=5.0):
        atomic_write(path, json.dumps(suggestions, ensure_ascii=False, indent=2) + "\n")


# ─── Scan: curator weekly deep run 用 ─────────────────────────────────────────


def scan_skill_candidates(
    vault_root: Path,
    *,
    cooldown_days: int = 7,
) -> dict[str, Any]:
    """Scan Mid_Term/ 找 tag 含 SKILL_SIGNAL_TAGS, append 到 pending_skill_suggestions.json.

    去重 / cooldown:
    - 同 entity_id 在 7 天 cooldown 內已 propose / dismiss → 跳過
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    mid_dir = root / MIDTERM_DIR
    pending = load_pending(root)
    now = datetime.now().astimezone()
    cooldown = timedelta(days=cooldown_days)

    if not mid_dir.exists():
        return {"new_added": [], "skipped": [], "total_pending": len(pending)}

    by_entity: dict[str, dict[str, Any]] = {}
    for s in pending:
        eid = s.get("entity_id", "")
        if eid:
            by_entity[eid] = s

    new_added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for path_obj in sorted(mid_dir.glob("*.md")):
        if path_obj.name.startswith("_"):
            continue
        eid = path_obj.stem
        rel = str(path_obj.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        fm = note.frontmatter
        if fm.pinned:
            continue
        if fm.lifecycle_state != LifecycleState.MID:
            continue
        if not (set(fm.tags) & SKILL_SIGNAL_TAGS):
            continue

        existing = by_entity.get(eid)
        if existing:
            proposed_at = _parse_iso(existing.get("proposed_at"))
            dismissed_at = _parse_iso(existing.get("dismissed_at"))
            promoted_to = existing.get("promoted_to")
            if promoted_to:
                skipped.append({"entity_id": eid, "reason": "already_promoted"})
                continue
            if dismissed_at and (now - dismissed_at) < cooldown:
                skipped.append({"entity_id": eid, "reason": "dismissed_in_cooldown"})
                continue
            if proposed_at and not dismissed_at and (now - proposed_at) < cooldown:
                skipped.append({"entity_id": eid, "reason": "pending_in_cooldown"})
                continue

        summary = next(
            (ln.strip() for ln in note.body.splitlines() if ln.strip() and not ln.strip().startswith("#")),
            f"Mid_Term entry: {eid}",
        )
        new_entry = {
            "entity_id": eid,
            "source_path": rel,
            "summary": summary[:200],
            "suggested_skill_id": _normalize_skill_id(eid),
            "mention_count": fm.mention_count,
            "tags": list(fm.tags),
            "proposed_at": now.isoformat(),
            "dismissed_at": None,
            "promoted_to": None,
        }
        if existing:
            for i, s in enumerate(pending):
                if s.get("entity_id") == eid:
                    pending[i] = new_entry
                    break
        else:
            pending.append(new_entry)
        new_added.append(new_entry)

    save_pending(root, pending)
    return {"new_added": new_added, "skipped": skipped, "total_pending": len(pending)}


# ─── chat_runtime hook: pick + parse intent + record response ───────────────


def pick_next_proposal(
    vault_root: Path,
    *,
    auto_dismiss_days: int = 7,
) -> Optional[dict[str, Any]]:
    """從 pending 挑下一個提議給 chat_runtime 貼 response 末端.

    已 dismissed / 已 promoted skip.
    proposed_at > auto_dismiss_days 自動標 dismiss (不提).
    """

    root = Path(vault_root).expanduser().resolve()
    pending = load_pending(root)
    if not pending:
        return None
    now = datetime.now().astimezone()
    cutoff = timedelta(days=auto_dismiss_days)
    changed = False
    selected: dict[str, Any] | None = None

    for s in pending:
        if s.get("dismissed_at") or s.get("promoted_to"):
            continue
        proposed_at = _parse_iso(s.get("proposed_at"))
        if proposed_at and (now - proposed_at) > cutoff:
            s["dismissed_at"] = now.isoformat()
            s["dismiss_reason"] = "auto_timeout"
            changed = True
            continue
        if selected is None:
            selected = s

    if changed:
        save_pending(root, pending)
    return selected


def parse_user_response_intent(text: str) -> str:
    """Parse 使用者回應 → 'accept' / 'decline' / 'none'.

    精準 keyword 開頭 + 短輸入限制 (避免「升職很爽」誤判).
    """

    if not text:
        return "none"
    stripped = text.strip()
    if len(stripped) > _MAX_INTENT_INPUT_LEN:
        return "none"
    if _ACCEPT_RE.match(stripped):
        return "accept"
    if _DECLINE_RE.match(stripped):
        return "decline"
    return "none"


def record_user_response(
    vault_root: Path,
    *,
    entity_id: str,
    accept: bool,
) -> dict[str, Any]:
    """使用者回應後 update pending. accept → promote_to_skill; decline → dismiss."""

    root = Path(vault_root).expanduser().resolve()
    pending = load_pending(root)
    now = datetime.now().astimezone()
    for s in pending:
        if s.get("entity_id") != entity_id:
            continue
        if accept:
            try:
                target_path = promote_to_skill(
                    root,
                    entity_id=entity_id,
                    suggested_skill_id=s.get("suggested_skill_id", entity_id),
                )
                s["promoted_to"] = target_path
                s["promoted_at"] = now.isoformat()
                save_pending(root, pending)
                return {"action": "promoted", "entity_id": entity_id, "target": target_path}
            except Exception as exc:  # noqa: BLE001
                return {"action": "error", "entity_id": entity_id, "error": str(exc)}
        s["dismissed_at"] = now.isoformat()
        s["dismiss_reason"] = "user_declined"
        save_pending(root, pending)
        return {"action": "dismissed", "entity_id": entity_id}
    return {"action": "not_found", "entity_id": entity_id}


# ─── 實際升格邏輯 ────────────────────────────────────────────────────────────


def promote_to_skill(
    vault_root: Path,
    *,
    entity_id: str,
    suggested_skill_id: str,
) -> str:
    """從 Mid_Term/<entity>.md 升格成 00_System/Skills/<skill_id>/SKILL.md."""

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    source_path = f"{MIDTERM_DIR}/{entity_id}.md"
    source_note = adapter.read_note(source_path)
    if source_note is None:
        raise FileNotFoundError(f"Mid_Term source not found: {source_path}")

    skill_id = _normalize_skill_id(suggested_skill_id or entity_id)
    target_path = f"{SKILLS_DIR}/{skill_id}/SKILL.md"
    existing = adapter.read_note(target_path)
    if existing is not None:
        # 避免覆蓋既有 skill — 加 timestamp suffix
        skill_id = f"{skill_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        target_path = f"{SKILLS_DIR}/{skill_id}/SKILL.md"

    body = (
        f"# Skill: {skill_id}\n\n"
        "## 來源\n\n"
        f"- 從中期記憶升格: `{source_path}` (entity_id: `{entity_id}`)\n"
        f"- 升格時間: {_now_local_iso()}\n"
        f"- 原始 mention_count: `{source_note.frontmatter.mention_count}`\n\n"
        "## 摘要\n\n"
        f"{source_note.body[:500]}\n\n"
        "## 步驟 / 流程\n\n"
        "<!-- 由 R7 C20b 從 Mid_Term 自動升格. 使用者可手動補充細節. -->\n"
    )
    skill_note = MemoryNote(
        path=target_path,
        frontmatter=Frontmatter(
            type=MemoryType.SKILL,
            source=MemorySource.PROMOTION,
            tags=["skill", "promoted_from_midterm"],
            agent="curator-skill-promoter",
            lifecycle_state=LifecycleState.LONG,
            pinned=False,
            extras={"promoted_from": source_path, "skill_id": skill_id},
        ),
        body=body,
    )
    adapter.write_note(skill_note)

    # 原 Mid_Term 檔加 marker (不刪)
    marker = f"\n<!-- promoted_to_skill: {target_path} @ {_now_local_iso()} -->\n"
    if marker.strip() not in source_note.body:
        source_note.body = source_note.body.rstrip() + marker
    source_note.frontmatter.extras["promoted_to_skill"] = target_path
    try:
        adapter.write_note(source_note)
    except Exception:  # noqa: BLE001
        # marker 寫失敗不影響 skill 升格成功
        pass

    return target_path


def build_chat_proposal_footer(suggestion: dict[str, Any]) -> str:
    """Build 對話末端提議 footer text."""

    eid = suggestion.get("entity_id", "")
    summary = suggestion.get("summary", "")
    mc = suggestion.get("mention_count", 0)
    return (
        "\n\n---\n"
        f"💡 我注意到「{summary}」這個流程你最近提到 {mc} 次,\n"
        f"要不要把它做成可重用的 skill (entity: `{eid}`)?\n"
        "回「升格」我就建; 回「跳過」下次不再提; 不回應 7 天後自動消失."
    )
