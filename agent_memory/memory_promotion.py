"""Short-term recall tracking and promotion cycle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_memory.entity_extract import extract_entities_from_text
from agent_memory.search import SearchHit
from agent_memory.search.manager import MemorySearchManager
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import Frontmatter, LifecycleState, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

RECALL_TRACKER_RELATIVE_PATH = ".ai/short_term_recall.json"
PROMOTION_EVENTS_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/promotion_events.md"
PROMOTION_MARKER_PREFIX = "<!-- agent-memory-promotion:"
PROMOTION_MARKER_SUFFIX = "-->"
# R7 C17: 中期記憶聚合落點
MIDTERM_DIR_RELATIVE = "10_Permanent/Mid_Term"
_PROMOTION_SOURCE_PREFIXES = (
    "11_AI_Mirror/ingestion_logs/daily_flush/",
    "11_AI_Mirror/internalised_candidates/",
)
_SUGGEST_CONCEPT_TAGS = {"concept", "pattern", "principle", "design", "architecture"}
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/")


def _normalize_id(raw: str, *, fallback: str) -> str:
    normalized = _SLUG_RE.sub("-", raw.lower()).strip("-")
    return normalized or fallback


def _is_promotion_source(path: str) -> bool:
    normalized = _normalize_path(path)
    return any(normalized.startswith(prefix) for prefix in _PROMOTION_SOURCE_PREFIXES)


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class RecallEntry:
    path: str
    content_hash: str
    recall_count: int = 0
    total_score: float = 0.0
    unique_query_hashes: set[str] = field(default_factory=set)
    recall_days: set[str] = field(default_factory=set)
    first_recalled_at: str = ""
    last_recalled_at: str = ""
    light_phase_hits: int = 0
    rem_phase_hits: int = 0
    promoted: bool = False
    promotion_target: str = ""

    @property
    def average_score(self) -> float:
        return self.total_score / max(self.recall_count, 1)

    @property
    def unique_query_count(self) -> int:
        return len(self.unique_query_hashes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "recall_count": self.recall_count,
            "total_score": self.total_score,
            "unique_query_hashes": sorted(self.unique_query_hashes),
            "recall_days": sorted(self.recall_days),
            "first_recalled_at": self.first_recalled_at,
            "last_recalled_at": self.last_recalled_at,
            "light_phase_hits": self.light_phase_hits,
            "rem_phase_hits": self.rem_phase_hits,
            "promoted": self.promoted,
            "promotion_target": self.promotion_target,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RecallEntry":
        return cls(
            path=str(payload.get("path", "")),
            content_hash=str(payload.get("content_hash", "")),
            recall_count=int(payload.get("recall_count", 0)),
            total_score=float(payload.get("total_score", 0.0)),
            unique_query_hashes={str(x) for x in payload.get("unique_query_hashes", [])},
            recall_days={str(x) for x in payload.get("recall_days", [])},
            first_recalled_at=str(payload.get("first_recalled_at", "")),
            last_recalled_at=str(payload.get("last_recalled_at", "")),
            light_phase_hits=int(payload.get("light_phase_hits", 0)),
            rem_phase_hits=int(payload.get("rem_phase_hits", 0)),
            promoted=bool(payload.get("promoted", False)),
            promotion_target=str(payload.get("promotion_target", "")),
        )


@dataclass(slots=True)
class PromotionThresholds:
    min_score: float = 0.75
    min_recall_count: int = 3
    min_unique_queries: int = 2
    min_unique_days: int = 2
    grace_period_hours: float = 24.0


def _load_tracker(path: Path) -> dict[str, RecallEntry]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, RecallEntry] = {}
    for key, raw in payload.items():
        if not isinstance(raw, dict):
            continue
        entry = RecallEntry.from_dict(raw)
        if entry.path:
            result[str(key)] = entry
    return result


def _save_tracker(path: Path, entries: dict[str, RecallEntry]) -> None:
    payload = {key: value.to_dict() for key, value in entries.items()}
    with file_lock(path, timeout=5.0):
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _tracker_path(vault_root: Path) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / RECALL_TRACKER_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, "{}\n")
    return target


def _append_promotion_event(vault_root: Path, *, block: str) -> str:
    root = Path(vault_root).expanduser().resolve()
    target = (root / PROMOTION_EVENTS_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, "# promotion_events\n\n> 短期升長期事件台帳。\n\n")
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + block)
    return str(target.relative_to(root)).replace("\\", "/")


def _phase_signal_boost(entry: RecallEntry) -> float:
    boost = entry.light_phase_hits * 0.05 + entry.rem_phase_hits * 0.15
    return min(boost, 0.3)


def compute_promotion_score(entry: RecallEntry, *, has_concept_tags: bool = False) -> float:
    frequency = min(entry.recall_count / 5.0, 1.0)
    relevance = max(0.0, min(entry.average_score, 1.0))
    diversity = min(entry.unique_query_count / 3.0, 1.0)

    recency = 0.0
    if entry.last_recalled_at:
        try:
            last = datetime.fromisoformat(entry.last_recalled_at.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_days = max((datetime.now(timezone.utc) - last).total_seconds() / 86400.0, 0.0)
            recency = 0.5 ** (age_days / 7.0)
        except ValueError:
            recency = 0.0
    consolidation = min(len(entry.recall_days) / 3.0, 1.0)
    conceptual = 1.3 if has_concept_tags else 1.0
    base = frequency * relevance * diversity * recency * consolidation * conceptual
    return base + _phase_signal_boost(entry)


def is_eligible(entry: RecallEntry, score: float, thresholds: PromotionThresholds) -> bool:
    if score < thresholds.min_score:
        return False
    if entry.recall_count < thresholds.min_recall_count:
        return False
    if entry.unique_query_count < thresholds.min_unique_queries:
        return False
    if len(entry.recall_days) < thresholds.min_unique_days:
        return False
    if entry.first_recalled_at and thresholds.grace_period_hours > 0:
        try:
            first = datetime.fromisoformat(entry.first_recalled_at.replace("Z", "+00:00"))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - first).total_seconds() / 3600.0
            if age_hours < thresholds.grace_period_hours:
                return False
        except ValueError:
            return False
    return True


def _excerpt_for_promotion(note: MemoryNote, *, max_chars: int = 720) -> str:
    lines: list[str] = []
    for raw in note.body.splitlines():
        text = raw.strip()
        if not text:
            continue
        if text.startswith("#"):
            continue
        if text.startswith(PROMOTION_MARKER_PREFIX):
            continue
        if text.startswith("- "):
            lines.append(text[2:].strip())
        else:
            lines.append(text)
    excerpt = " ".join(lines).strip()
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[: max_chars - 3] + "..."


def _decide_target(note: MemoryNote, excerpt: str) -> str:
    tags = {str(tag).strip().lower() for tag in note.frontmatter.tags}
    if tags & _SUGGEST_CONCEPT_TAGS:
        return "concept"
    lowered = excerpt.lower()
    fact_keywords = ("偏好", "決定", "規則", "慣例", "always", "never", "must")
    if any(key in lowered for key in fact_keywords):
        return "long_term"
    return "concept"


def _make_marker(target: str, target_path: str) -> str:
    return f"{PROMOTION_MARKER_PREFIX}{target}:{target_path}{PROMOTION_MARKER_SUFFIX}"


def _extract_marker_target(content: str) -> str:
    start = content.find(PROMOTION_MARKER_PREFIX)
    if start < 0:
        return ""
    end = content.find(PROMOTION_MARKER_SUFFIX, start)
    if end < 0:
        return ""
    raw = content[start + len(PROMOTION_MARKER_PREFIX) : end]
    parts = raw.split(":", 1)
    if len(parts) != 2:
        return ""
    return parts[1].strip()


def _append_marker(note: MemoryNote, marker: str) -> None:
    if marker in note.body:
        return
    note.body = note.body.rstrip() + "\n\n" + marker + "\n"


def _promote_to_memory(adapter: ObsidianVaultAdapter, *, source_path: str, excerpt: str, score: float) -> str:
    target_path = "10_Permanent/MEMORY.md"
    note = adapter.read_note(target_path)
    if note is None:
        note = MemoryNote(
            path=target_path,
            frontmatter=Frontmatter(
                type=MemoryType.LONG_TERM,
                source=MemorySource.PROMOTION,
                tags=["memory", "promotion"],
                agent="promotion-cycle",
            ),
            body="# MEMORY\n",
        )
    section = (
        f"\n## promotion {datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"- source_path: `{source_path}`\n"
        f"- score: `{score:.3f}`\n"
        f"- excerpt: {excerpt}\n"
    )
    note.body = note.body.rstrip() + section + "\n"
    note.frontmatter.source = MemorySource.PROMOTION
    note.frontmatter.agent = "promotion-cycle"
    if "promotion" not in note.frontmatter.tags:
        note.frontmatter.tags.append("promotion")
    adapter.write_note(note)
    return target_path


def _promote_to_concept(adapter: ObsidianVaultAdapter, *, source_path: str, excerpt: str, score: float) -> str:
    source_stem = Path(source_path).stem
    fallback = f"concept-{datetime.now().strftime('%Y%m%d')}"
    slug_seed = _normalize_id(source_stem, fallback=fallback)
    concept_id = f"{slug_seed}-{_sha16(excerpt)[:6]}"
    target_path = f"10_Permanent/Concepts/{concept_id}.md"
    existing = adapter.read_note(target_path)
    if existing is None:
        body = (
            f"# {concept_id}\n\n"
            "## 來源\n\n"
            f"- source_path: `{source_path}`\n"
            f"- promoted_at: `{_now_iso()}`\n"
            f"- score: `{score:.3f}`\n\n"
            "## 內容\n\n"
            f"{excerpt}\n"
        )
        note = MemoryNote(
            path=target_path,
            frontmatter=Frontmatter(
                type=MemoryType.CONCEPT,
                source=MemorySource.PROMOTION,
                tags=["concept", "promoted"],
                agent="promotion-cycle",
                extras={"source_path": source_path, "score": round(score, 4)},
            ),
            body=body,
        )
    else:
        existing.body = (
            existing.body.rstrip()
            + "\n\n## promotion update\n\n"
            + f"- source_path: `{source_path}`\n"
            + f"- score: `{score:.3f}`\n"
            + f"- excerpt: {excerpt}\n"
        )
        note = existing
        note.frontmatter.source = MemorySource.PROMOTION
        note.frontmatter.agent = "promotion-cycle"
        note.frontmatter.extras["score"] = round(score, 4)
    adapter.write_note(note)
    return target_path


def record_recall_hits(
    vault_root: Path,
    *,
    query: str,
    hits: list[SearchHit],
    phase: str = "light",
) -> dict[str, Any]:
    """Record recall stats for promotable short-term notes."""

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    tracker = _tracker_path(root)
    entries = _load_tracker(tracker)

    phase_norm = str(phase).strip().lower()
    if phase_norm not in {"light", "rem"}:
        phase_norm = "light"

    query_hash = _sha16(query.strip().lower())
    today = datetime.now(timezone.utc).date().isoformat()
    now_iso = _now_iso()
    updated = 0
    for hit in hits:
        path = _normalize_path(hit.path)
        if not _is_promotion_source(path):
            continue
        note = adapter.read_note(path)
        if note is None:
            continue
        key = path
        content_hash = _sha16(note.body)
        entry = entries.get(key) or RecallEntry(path=path, content_hash=content_hash)
        marker_target = _extract_marker_target(note.body)
        if marker_target:
            entry.content_hash = content_hash
            entry.promoted = True
            entry.promotion_target = marker_target
            entries[key] = entry
            continue
        if entry.content_hash != content_hash:
            entry.content_hash = content_hash
            entry.recall_count = 0
            entry.total_score = 0.0
            entry.unique_query_hashes.clear()
            entry.recall_days.clear()
            entry.first_recalled_at = ""
            entry.last_recalled_at = ""
            entry.light_phase_hits = 0
            entry.rem_phase_hits = 0
            if not entry.promoted:
                entry.promotion_target = ""
        entry.recall_count += 1
        entry.total_score += float(hit.score)
        entry.unique_query_hashes.add(query_hash)
        entry.recall_days.add(today)
        if not entry.first_recalled_at:
            entry.first_recalled_at = now_iso
        entry.last_recalled_at = now_iso
        if phase_norm == "rem":
            entry.rem_phase_hits += 1
        else:
            entry.light_phase_hits += 1
        entries[key] = entry
        updated += 1

    _save_tracker(tracker, entries)
    return {
        "tracker_path": str(tracker.relative_to(root)).replace("\\", "/"),
        "updated_entries": updated,
        "phase": phase_norm,
    }


def list_recall_entries(
    vault_root: Path,
    *,
    limit: int = 30,
    promoted_only: bool = False,
) -> list[dict[str, Any]]:
    root = Path(vault_root).expanduser().resolve()
    tracker = _tracker_path(root)
    entries = _load_tracker(tracker)
    rows = list(entries.values())
    rows.sort(key=lambda item: (item.promoted, item.recall_count, item.last_recalled_at), reverse=True)
    output: list[dict[str, Any]] = []
    for entry in rows:
        if promoted_only and not entry.promoted:
            continue
        output.append(entry.to_dict())
        if len(output) >= max(1, int(limit)):
            break
    return output


def run_promotion_cycle(
    vault_root: Path,
    *,
    phase: str = "light",
    thresholds: PromotionThresholds | None = None,
    operator: str = "promotion-cycle",
    dry_run: bool = True,
    max_promotions: int = 20,
) -> dict[str, Any]:
    """Scan recall tracker and promote eligible short-term notes."""

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    tracker_path = _tracker_path(root)
    entries = _load_tracker(tracker_path)
    search = MemorySearchManager(adapter)

    threshold = thresholds or PromotionThresholds()
    phase_norm = str(phase).strip().lower()
    if phase_norm == "rem":
        threshold = PromotionThresholds(
            min_score=threshold.min_score * 0.85,
            min_recall_count=max(1, threshold.min_recall_count - 1),
            min_unique_queries=threshold.min_unique_queries,
            min_unique_days=threshold.min_unique_days,
            grace_period_hours=threshold.grace_period_hours,
        )
    else:
        phase_norm = "light"

    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    remaining = max(1, int(max_promotions))

    for key, entry in sorted(entries.items(), key=lambda item: item[1].recall_count, reverse=True):
        if entry.promoted:
            continue
        note = adapter.read_note(entry.path)
        if note is None:
            skipped.append({"path": entry.path, "reason": "source_missing"})
            continue
        marker_target = _extract_marker_target(note.body)
        if marker_target:
            entry.promoted = True
            entry.promotion_target = marker_target
            entries[key] = entry
            skipped.append({"path": entry.path, "reason": "already_marked"})
            continue
        if not _is_promotion_source(entry.path):
            skipped.append({"path": entry.path, "reason": "outside_promotion_source"})
            continue

        has_concept_tags = any(str(tag).strip().lower() in _SUGGEST_CONCEPT_TAGS for tag in note.frontmatter.tags)
        score = compute_promotion_score(entry, has_concept_tags=has_concept_tags)
        candidates.append(
            {
                "path": entry.path,
                "score": round(score, 4),
                "recall_count": entry.recall_count,
                "unique_queries": entry.unique_query_count,
                "unique_days": len(entry.recall_days),
            }
        )
        if not is_eligible(entry, score, threshold):
            skipped.append({"path": entry.path, "reason": "below_threshold", "score": round(score, 4)})
            continue

        excerpt = _excerpt_for_promotion(note)
        if not excerpt:
            skipped.append({"path": entry.path, "reason": "empty_excerpt"})
            continue
        target_type = _decide_target(note, excerpt)

        if dry_run:
            promoted.append(
                {
                    "path": entry.path,
                    "target_type": target_type,
                    "target_path": "",
                    "score": round(score, 4),
                    "dry_run": True,
                }
            )
            continue
        if remaining <= 0:
            skipped.append({"path": entry.path, "reason": "promotion_limit"})
            continue

        if target_type == "concept":
            target_path = _promote_to_concept(adapter, source_path=entry.path, excerpt=excerpt, score=score)
        else:
            target_path = _promote_to_memory(adapter, source_path=entry.path, excerpt=excerpt, score=score)
        marker = _make_marker(target_type, target_path)
        _append_marker(note, marker)
        note.frontmatter.source = MemorySource.PROMOTION
        note.frontmatter.agent = _normalize_id(operator, fallback="promotion-cycle")
        note.frontmatter.extras["promoted_at"] = _now_iso()
        note.frontmatter.extras["promotion_target"] = target_path
        adapter.write_note(note)
        search.index_path(target_path)
        search.index_path(entry.path)

        entry.promoted = True
        entry.promotion_target = target_path
        entries[key] = entry
        remaining -= 1

        block = (
            f"## {_now_iso()} promotion\n\n"
            f"- source_path: `{entry.path}`\n"
            f"- target_type: `{target_type}`\n"
            f"- target_path: `{target_path}`\n"
            f"- score: `{score:.4f}`\n"
            f"- operator: `{_normalize_id(operator, fallback='promotion-cycle')}`\n\n"
        )
        _append_promotion_event(root, block=block)
        promoted.append(
            {
                "path": entry.path,
                "target_type": target_type,
                "target_path": target_path,
                "score": round(score, 4),
                "dry_run": False,
            }
        )

    _save_tracker(tracker_path, entries)
    return {
        "phase": phase_norm,
        "dry_run": bool(dry_run),
        "tracker_path": str(tracker_path.relative_to(root)).replace("\\", "/"),
        "thresholds": {
            "min_score": threshold.min_score,
            "min_recall_count": threshold.min_recall_count,
            "min_unique_queries": threshold.min_unique_queries,
            "min_unique_days": threshold.min_unique_days,
            "grace_period_hours": threshold.grace_period_hours,
        },
        "candidates": candidates,
        "promoted": promoted,
        "skipped": skipped,
    }


# ─── R7 C17: 短 → 中期 entity 聚合 ────────────────────────────────────────────


def _now_local_iso() -> str:
    """本機時區 ISO with offset (e.g. 2026-05-16T14:30:00+08:00) — R7 C18 也用此."""

    return datetime.now().astimezone().isoformat()


def aggregate_to_midterm(
    vault_root: Path,
    daily_flush_path: str,
    *,
    session_id: str = "",
    max_entities_per_flush: int = 10,
) -> dict[str, Any]:
    """從一個 daily_flush 抽 entity → 累加到對應 `Mid_Term/<entity>.md`.

    R7 C17 短 → 中期聚合主邏輯 (V2_Round7 §4.1).

    對每個抽出的 entity_id:
    - 若 `Mid_Term/<entity_id>.md` 不存在 → 建檔, lifecycle_state=mid, mention_count=1
    - 若已存在且非 pinned → mention_count +1, 加 subsection 記這次 session
    - 若已存在且 pinned → 跳過 mention_count 累計 (pinned baseline 不被熱度干擾)

    Args:
        vault_root: vault 根目錄
        daily_flush_path: 來源 daily_flush 路徑 (相對 vault, 例 `11_AI_Mirror/ingestion_logs/daily_flush/2026-05-16.md`)
        session_id: 對應 session id (給 traceability)
        max_entities_per_flush: 一個 daily_flush 最多處理幾個 entity (避免 noise 炸 Mid_Term/)

    Returns:
        dict: {processed_entities, created, updated, skipped_pinned, daily_flush_path, error?}
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    daily_note = adapter.read_note(daily_flush_path)
    if daily_note is None:
        return {
            "error": f"daily_flush not found: {daily_flush_path}",
            "processed_entities": [],
            "created": [],
            "updated": [],
            "skipped_pinned": [],
        }

    entities = extract_entities_from_text(daily_note.body, max_entities=max_entities_per_flush)
    if not entities:
        return {
            "processed_entities": [],
            "created": [],
            "updated": [],
            "skipped_pinned": [],
            "daily_flush_path": daily_flush_path,
        }

    created: list[str] = []
    updated: list[str] = []
    skipped_pinned: list[str] = []
    now_iso = _now_local_iso()

    for entity_id in entities:
        target_path = f"{MIDTERM_DIR_RELATIVE}/{entity_id}.md"
        existing = adapter.read_note(target_path)

        if existing is None:
            # 第一次遇到此 entity → 建中期檔
            note = MemoryNote(
                path=target_path,
                frontmatter=Frontmatter(
                    type=MemoryType.CONCEPT,
                    source=MemorySource.PROMOTION,
                    tags=["mid_term", "aggregated"],
                    agent="entity-aggregator",
                    lifecycle_state=LifecycleState.MID,
                    mention_count=1,
                    last_activity_at=now_iso,
                    pinned=False,
                    extras={
                        "source_daily_flush": daily_flush_path,
                        "first_session": session_id,
                    },
                ),
                body=(
                    f"# {entity_id}\n\n"
                    "## 來源\n\n"
                    f"- 第一次提及: `{daily_flush_path}` ({now_iso})\n"
                    f"- session: `{session_id}`\n\n"
                    "## 累積提及\n\n"
                    f"- {now_iso} session=`{session_id}` from `{daily_flush_path}`\n\n"
                    "> 此檔由 entity-aggregator 自動建立 (R7 C17 短→中聚合).\n"
                    "> Mid_Term 為「可變」層: LLM 對話可改寫此檔以累積上下文.\n"
                    "> 當 `mention_count >= 3` + stable_age >= 7d + no-edit >= 3d 後 curator 會升格到長期 (Concepts/Facts/MEMORY).\n"
                ),
            )
            adapter.write_note(note)
            created.append(entity_id)
            continue

        # 已存在
        fm = existing.frontmatter
        if fm.pinned:
            # pinned 跳過累計 (避免 hot entity 干擾)
            skipped_pinned.append(entity_id)
            continue

        # 累計 mention + last_activity_at + body subsection
        fm.mention_count += 1
        fm.last_activity_at = now_iso
        # 若 lifecycle_state 已是 long/stale/archived (例如曾升格再被降回) 不要拉回 mid
        if fm.lifecycle_state in (LifecycleState.SHORT,):
            fm.lifecycle_state = LifecycleState.MID
        new_block = f"- {now_iso} session=`{session_id}` from `{daily_flush_path}`"
        if new_block.strip() not in existing.body:
            existing.body = existing.body.rstrip() + "\n" + new_block + "\n"
        existing.frontmatter = fm
        adapter.write_note(existing)
        updated.append(entity_id)

    return {
        "processed_entities": entities,
        "created": created,
        "updated": updated,
        "skipped_pinned": skipped_pinned,
        "daily_flush_path": daily_flush_path,
    }


# ─── R7 C19: 中 → 長升格 + 90/180d 降級 ─────────────────────────────────────


@dataclass(slots=True)
class MidToLongThresholds:
    """V2_Round7 §4.2 中→長升格門檻 (使用者拍板)."""

    min_mention_count: int = 3
    min_stable_days: int = 7
    no_edit_days: int = 3


@dataclass(slots=True)
class LifecycleThresholds:
    """V2_Round7 §4.3 長期降級門檻 (個人用 90/180d, archive 不刪檔)."""

    stale_after_days: int = 90
    archive_after_days: int = 180


def _load_mid_to_long_thresholds(vault_root: Path) -> MidToLongThresholds:
    """Load mid_to_long thresholds from promotion.yaml (缺 → defaults)."""

    root = Path(vault_root).expanduser().resolve()
    cfg_path = root / "00_System/08_Runtime_Profiles/promotion.yaml"
    if not cfg_path.exists():
        return MidToLongThresholds()
    try:
        import yaml as _yaml
        payload = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        mtl = payload.get("mid_to_long", {}) if isinstance(payload, dict) else {}
        if not isinstance(mtl, dict):
            mtl = {}
        return MidToLongThresholds(
            min_mention_count=int(mtl.get("min_mention_count", 3)),
            min_stable_days=int(mtl.get("min_stable_days", 7)),
            no_edit_days=int(mtl.get("no_edit_days", 3)),
        )
    except Exception:  # noqa: BLE001
        return MidToLongThresholds()


def _load_lifecycle_thresholds(vault_root: Path) -> LifecycleThresholds:
    """Load long_lifecycle thresholds from promotion.yaml."""

    root = Path(vault_root).expanduser().resolve()
    cfg_path = root / "00_System/08_Runtime_Profiles/promotion.yaml"
    if not cfg_path.exists():
        return LifecycleThresholds()
    try:
        import yaml as _yaml
        payload = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        ll = payload.get("long_lifecycle", {}) if isinstance(payload, dict) else {}
        if not isinstance(ll, dict):
            ll = {}
        return LifecycleThresholds(
            stale_after_days=int(ll.get("stale_after_days", 90)),
            archive_after_days=int(ll.get("archive_after_days", 180)),
        )
    except Exception:  # noqa: BLE001
        return LifecycleThresholds()


def _safe_parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def promote_midterm_to_long(
    vault_root: Path,
    *,
    thresholds: MidToLongThresholds | None = None,
    dry_run: bool = False,
    max_promotions: int = 20,
) -> dict[str, Any]:
    """中 → 長升格 (R7 C19, V2_Round7 §4.2 雙軌制 = counter + 時間穩定性).

    對 `Mid_Term/<entity>.md` 滿足 ALL 條件:
        - `lifecycle_state == mid` (未升格過)
        - `pinned == False`
        - `mention_count >= N2` (預設 3)
        - stable_age (now - created) >= 7d
        - no-edit (now - updated) >= 3d

    動作:
        - 升到 `Concepts/` 或 `MEMORY.md` (沿用 `_decide_target` 邏輯)
        - 原 Mid_Term 檔 `lifecycle_state=long` + `extras.promoted_to/promoted_at` (不刪原檔留 traceability)
        - 寫 promotion_events.md
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    threshold = thresholds or _load_mid_to_long_thresholds(root)
    now = datetime.now().astimezone()  # 本機時區
    mid_dir = root / MIDTERM_DIR_RELATIVE

    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    remaining = max(1, int(max_promotions))

    if not mid_dir.exists():
        return {
            "thresholds": {
                "min_mention_count": threshold.min_mention_count,
                "min_stable_days": threshold.min_stable_days,
                "no_edit_days": threshold.no_edit_days,
            },
            "dry_run": dry_run,
            "promoted": [],
            "skipped": [],
            "candidates": [],
        }

    for path_obj in sorted(mid_dir.glob("*.md")):
        if path_obj.name.startswith("_"):
            continue
        rel = str(path_obj.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        fm = note.frontmatter

        if fm.lifecycle_state != LifecycleState.MID:
            skipped.append({"path": rel, "reason": f"not_mid (state={fm.lifecycle_state.value})"})
            continue
        if fm.pinned:
            skipped.append({"path": rel, "reason": "pinned"})
            continue
        if fm.mention_count < threshold.min_mention_count:
            skipped.append({
                "path": rel,
                "reason": f"mention_below_threshold ({fm.mention_count} < {threshold.min_mention_count})",
            })
            continue

        created_local = fm.created.astimezone() if fm.created else now
        updated_local = fm.updated.astimezone() if fm.updated else now
        stable_days = (now - created_local).total_seconds() / 86400
        no_edit_days = (now - updated_local).total_seconds() / 86400

        if stable_days < threshold.min_stable_days:
            skipped.append({
                "path": rel,
                "reason": f"not_stable_enough ({stable_days:.1f}d / {threshold.min_stable_days}d)",
            })
            continue
        if no_edit_days < threshold.no_edit_days:
            skipped.append({
                "path": rel,
                "reason": f"too_recent_edit ({no_edit_days:.1f}d / {threshold.no_edit_days}d)",
            })
            continue

        candidate_info = {
            "path": rel,
            "mention_count": fm.mention_count,
            "stable_days": round(stable_days, 1),
            "no_edit_days": round(no_edit_days, 1),
        }
        candidates.append(candidate_info)

        if dry_run:
            continue
        if remaining <= 0:
            skipped.append({"path": rel, "reason": "promotion_limit"})
            continue

        excerpt = _excerpt_for_promotion(note)
        if not excerpt:
            skipped.append({"path": rel, "reason": "empty_excerpt"})
            continue
        target_type = _decide_target(note, excerpt)

        try:
            if target_type == "concept":
                target_path = _promote_to_concept(adapter, source_path=rel, excerpt=excerpt, score=1.0)
            else:
                target_path = _promote_to_memory(adapter, source_path=rel, excerpt=excerpt, score=1.0)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"path": rel, "reason": f"promote_error: {exc}"})
            continue

        # 原 Mid_Term 檔轉 long state + marker (不刪原檔, 留 traceability)
        fm.lifecycle_state = LifecycleState.LONG
        fm.extras["promoted_to"] = target_path
        fm.extras["promoted_at"] = now.isoformat()
        note.frontmatter = fm
        marker = f"\n<!-- promoted_to: {target_path} @ {now.isoformat()} -->\n"
        if marker.strip() not in note.body:
            note.body = note.body.rstrip() + marker
        try:
            adapter.write_note(note)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"path": rel, "reason": f"midterm_marker_write_error: {exc}"})
            continue

        block = (
            f"## {now.isoformat()} promotion (R7 mid→long)\n\n"
            f"- source_path: `{rel}`\n"
            f"- target_type: `{target_type}`\n"
            f"- target_path: `{target_path}`\n"
            f"- mention_count: `{fm.mention_count}`\n"
            f"- stable_days: `{stable_days:.1f}`\n"
            f"- operator: `curator-weekly`\n\n"
        )
        _append_promotion_event(root, block=block)

        promoted.append({
            "path": rel,
            "target_type": target_type,
            "target_path": target_path,
            "mention_count": fm.mention_count,
        })
        remaining -= 1

    return {
        "thresholds": {
            "min_mention_count": threshold.min_mention_count,
            "min_stable_days": threshold.min_stable_days,
            "no_edit_days": threshold.no_edit_days,
        },
        "dry_run": dry_run,
        "candidates": candidates,
        "promoted": promoted,
        "skipped": skipped,
    }


def demote_long_to_stale_or_archive(
    vault_root: Path,
    *,
    thresholds: LifecycleThresholds | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """長期降級 (R7 C19, hermes 抄但個人用 90/180d 放寬).

    對 `10_Permanent/` 內所有 .md (跳過 Mid_Term/ 子層, 跳過 _DIR_INFO):
        - pinned skip
        - `last_activity_at` 為空 → 用 `updated` 當 fallback
        - elapsed > 180d → 移到 `99_Archive/auto_archived/<YYYY>/<name>.md` + state=archived
        - elapsed > 90d → state=stale (標記不動位置)

    Archive 走 atomic_write 而非 write_note (繞過 scanner — 搬檔不是新內容).
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    threshold = thresholds or _load_lifecycle_thresholds(root)
    now = datetime.now().astimezone()

    staled: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    permanent_dir = root / "10_Permanent"

    if not permanent_dir.exists():
        return {
            "thresholds": {
                "stale_after_days": threshold.stale_after_days,
                "archive_after_days": threshold.archive_after_days,
            },
            "dry_run": dry_run,
            "staled": [],
            "archived": [],
            "skipped": [],
        }

    for path_obj in permanent_dir.rglob("*.md"):
        if not path_obj.is_file():
            continue
        if path_obj.name.startswith("_"):
            continue
        rel = str(path_obj.relative_to(root)).replace("\\", "/")
        # 中期由 promote_midterm_to_long 管, demote 不動 Mid_Term/
        if "/Mid_Term/" in rel:
            continue
        note = adapter.read_note(rel)
        if note is None:
            continue
        fm = note.frontmatter
        if fm.pinned:
            skipped.append({"path": rel, "reason": "pinned"})
            continue
        if fm.lifecycle_state == LifecycleState.ARCHIVED:
            skipped.append({"path": rel, "reason": "already_archived"})
            continue
        if fm.lifecycle_state == LifecycleState.MID:
            # 不該出現在 10_Permanent/ 非 Mid_Term/ 路徑但保險
            skipped.append({"path": rel, "reason": "lifecycle_mid_outside_midterm"})
            continue

        last_active = _safe_parse_iso(fm.last_activity_at)
        if last_active is None:
            last_active = fm.updated.astimezone() if fm.updated else now
        elapsed_days = (now - last_active).total_seconds() / 86400

        # Archive 優先 (180d > 90d)
        if elapsed_days >= threshold.archive_after_days:
            if dry_run:
                archived.append({"path": rel, "elapsed_days": round(elapsed_days, 1), "skipped": "dry_run"})
                continue
            year = last_active.year if last_active else now.year
            archive_rel = f"99_Archive/auto_archived/{year}/{path_obj.name}"
            archive_abs = root / archive_rel
            try:
                archive_abs.parent.mkdir(parents=True, exist_ok=True)
                # 改 frontmatter state + extras 再 serialize 寫到新位置 (繞過 scanner)
                fm.lifecycle_state = LifecycleState.ARCHIVED
                fm.etl_status = fm.etl_status  # 保留 etl
                fm.extras["archived_at"] = now.isoformat()
                fm.extras["archived_from"] = rel
                metadata = adapter._frontmatter_to_dict(fm)
                text = adapter.serialize_frontmatter(metadata, note.body)
                atomic_write(archive_abs, text)
                path_obj.unlink()
                archived.append({"path": rel, "to": archive_rel, "elapsed_days": round(elapsed_days, 1)})
                _append_promotion_event(
                    root,
                    block=(
                        f"## {now.isoformat()} demote (R7 long→archived)\n\n"
                        f"- source_path: `{rel}`\n"
                        f"- archive_path: `{archive_rel}`\n"
                        f"- elapsed_days: `{elapsed_days:.1f}`\n"
                        f"- operator: `curator-weekly`\n\n"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                skipped.append({"path": rel, "reason": f"archive_error: {exc}"})
            continue

        # Stale (90d ≤ elapsed < 180d)
        if elapsed_days >= threshold.stale_after_days:
            if fm.lifecycle_state == LifecycleState.STALE:
                continue  # 已 stale 不重複標
            if dry_run:
                staled.append({"path": rel, "elapsed_days": round(elapsed_days, 1), "skipped": "dry_run"})
                continue
            fm.lifecycle_state = LifecycleState.STALE
            note.frontmatter = fm
            try:
                adapter.write_note(note)
                staled.append({"path": rel, "elapsed_days": round(elapsed_days, 1)})
            except Exception as exc:  # noqa: BLE001
                skipped.append({"path": rel, "reason": f"stale_mark_error: {exc}"})

    return {
        "thresholds": {
            "stale_after_days": threshold.stale_after_days,
            "archive_after_days": threshold.archive_after_days,
        },
        "dry_run": dry_run,
        "staled": staled,
        "archived": archived,
        "skipped": skipped,
    }


# ─── R7 C20a: Umbrella consolidation (keyword-based, hermes 抄) ──────────────


def consolidate_umbrella_keyword(
    vault_root: Path,
    *,
    min_group_size: int = 2,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Daily light step: keyword-based umbrella consolidation (R7 C20a).

    抄 hermes agent/curator.py:329-399 umbrella consolidation pattern, 簡化為 prefix-based:
    - 切 entity_id by '-' 得 first segment
    - 若同一 first_segment 有 >= min_group_size 個 Mid_Term entries → 建/補 umbrella

    範例:
        python-async.md + python-decorator.md + python-typing.md
        → 新建 (或補) Mid_Term/python.md (umbrella) + 3 個 subsection
        原 3 檔加 `<!-- umbrella'd into: ... -->` redirect marker + extras.umbrella_of

    Design rule (V2_Round7 §5.3):
    - 不刪原檔 (hermes "merge not delete" 模式)
    - 已是 umbrella (tags 含 "umbrella") 自己不再被 consolidate
    - lifecycle_state != mid 跳過 (升格過 / archived 不動)
    - pinned 跳過

    LLM-based deep consolidation (weekly) 留 C20a 後續 / Round 8.
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    mid_dir = root / MIDTERM_DIR_RELATIVE
    if not mid_dir.exists():
        return {
            "min_group_size": min_group_size,
            "dry_run": dry_run,
            "groups": [],
            "consolidated": [],
            "skipped": [],
        }

    # 1) Group by first prefix segment
    groups: dict[str, list[str]] = {}
    for path_obj in sorted(mid_dir.glob("*.md")):
        if path_obj.name.startswith("_"):
            continue
        eid = path_obj.stem
        rel = str(path_obj.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        if "umbrella" in note.frontmatter.tags:
            continue
        if note.frontmatter.lifecycle_state != LifecycleState.MID:
            continue
        if note.frontmatter.pinned:
            continue
        if "-" not in eid:
            continue
        prefix = eid.split("-", 1)[0]
        if not prefix:
            continue
        groups.setdefault(prefix, []).append(eid)

    consolidated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    group_summary: list[dict[str, Any]] = []
    now = datetime.now().astimezone()

    for prefix, members in groups.items():
        if len(members) < min_group_size:
            continue  # 太少不算 group
        group_summary.append({"prefix": prefix, "members": members})
        if dry_run:
            continue

        umbrella_path = f"{MIDTERM_DIR_RELATIVE}/{prefix}.md"
        umbrella_note = adapter.read_note(umbrella_path)
        merged_mention = 0
        member_summaries: list[str] = []

        for eid in members:
            mem_path = f"{MIDTERM_DIR_RELATIVE}/{eid}.md"
            mem_note = adapter.read_note(mem_path)
            if mem_note is None:
                continue
            merged_mention += mem_note.frontmatter.mention_count
            # 抽 title (第一個 # heading) 當 summary
            title = next(
                (ln.lstrip("# ").strip() for ln in mem_note.body.splitlines() if ln.strip().startswith("#")),
                eid,
            )
            member_summaries.append(
                f"### {eid} (mention_count={mem_note.frontmatter.mention_count})\n- title: {title}\n- source: `{mem_path}`\n"
            )
            # member 加 redirect marker + extras
            marker = f"<!-- umbrella'd into: {umbrella_path} @ {now.isoformat()} -->"
            if marker not in mem_note.body:
                mem_note.body = mem_note.body.rstrip() + "\n\n" + marker + "\n"
            mem_note.frontmatter.extras["umbrella_of"] = umbrella_path
            try:
                adapter.write_note(mem_note)
            except Exception as exc:  # noqa: BLE001
                skipped.append({"path": mem_path, "reason": f"member_marker_error: {exc}"})

        members_section = "\n".join(member_summaries) if member_summaries else "(no readable members)\n"
        body = (
            f"# {prefix} (umbrella)\n\n"
            "## Umbrella 說明\n\n"
            "- 此檔由 R7 C20a curator daily light **自動 keyword 合併**生成\n"
            f"- 合併時間: {now.isoformat()}\n"
            f"- 合併成員: {', '.join(members)}\n"
            f"- 合併規則: entity_id 同一前綴 `{prefix}-*` 且 ≥ {min_group_size} 個\n"
            "- hermes mode: 合併不刪, 子檔仍可被 RAG 個別命中\n\n"
            "## 子節點摘要\n\n"
            f"{members_section}\n"
        )

        if umbrella_note is None:
            new_note = MemoryNote(
                path=umbrella_path,
                frontmatter=Frontmatter(
                    type=MemoryType.CONCEPT,
                    source=MemorySource.PROMOTION,
                    tags=["umbrella", "mid_term", "consolidated"],
                    agent="curator-umbrella",
                    lifecycle_state=LifecycleState.MID,
                    mention_count=merged_mention,
                    last_activity_at=now.isoformat(),
                    pinned=False,
                    extras={
                        "umbrella_prefix": prefix,
                        "umbrella_members": members,
                        "consolidated_at": now.isoformat(),
                    },
                ),
                body=body,
            )
            try:
                adapter.write_note(new_note)
                consolidated.append({
                    "umbrella": umbrella_path,
                    "members": members,
                    "merged_mention": merged_mention,
                    "action": "created",
                })
            except Exception as exc:  # noqa: BLE001
                skipped.append({"prefix": prefix, "reason": f"umbrella_create_error: {exc}"})
        else:
            # 已存在 umbrella, append 新 members 不重複
            extras = umbrella_note.frontmatter.extras if isinstance(umbrella_note.frontmatter.extras, dict) else {}
            existing_members = extras.get("umbrella_members", [])
            if not isinstance(existing_members, list):
                existing_members = []
            new_members = [m for m in members if m not in existing_members]
            if not new_members:
                continue
            umbrella_note.body = (
                umbrella_note.body.rstrip()
                + f"\n\n## 新合併 ({now.date().isoformat()})\n\n"
                + members_section
                + "\n"
            )
            umbrella_note.frontmatter.extras["umbrella_members"] = list(existing_members) + new_members
            umbrella_note.frontmatter.mention_count = umbrella_note.frontmatter.mention_count + merged_mention
            umbrella_note.frontmatter.last_activity_at = now.isoformat()
            try:
                adapter.write_note(umbrella_note)
                consolidated.append({
                    "umbrella": umbrella_path,
                    "members": new_members,
                    "merged_mention": merged_mention,
                    "action": "appended",
                })
            except Exception as exc:  # noqa: BLE001
                skipped.append({"prefix": prefix, "reason": f"umbrella_append_error: {exc}"})

    return {
        "min_group_size": min_group_size,
        "dry_run": dry_run,
        "groups": group_summary,
        "consolidated": consolidated,
        "skipped": skipped,
    }


def list_midterm_entries(
    vault_root: Path,
    *,
    min_mention_count: int = 0,
    only_unpromoted: bool = True,
) -> list[dict[str, Any]]:
    """列出 Mid_Term/ 內所有 entity (給 curator weekly 升長期掃描用, C19 會接).

    Args:
        min_mention_count: 過濾 mention_count 達標的
        only_unpromoted: True → 只回未升格 (lifecycle_state=mid). 升格後變 long / stale / archived 就不再回.

    Returns: list of {path, entity_id, mention_count, last_activity_at, lifecycle_state, pinned, created}
    """

    root = Path(vault_root).expanduser().resolve()
    mid_dir = root / MIDTERM_DIR_RELATIVE
    if not mid_dir.exists():
        return []

    adapter = ObsidianVaultAdapter(root)
    rows: list[dict[str, Any]] = []
    for path_obj in sorted(mid_dir.glob("*.md")):
        if path_obj.name.startswith("_"):
            # 跳過 _DIR_INFO.md / _Example_*.md 之類底線開頭的非 entity 檔
            continue
        rel = str(path_obj.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        fm = note.frontmatter
        if fm.mention_count < min_mention_count:
            continue
        if only_unpromoted and fm.lifecycle_state != LifecycleState.MID:
            continue
        rows.append({
            "path": rel,
            "entity_id": path_obj.stem,
            "mention_count": fm.mention_count,
            "last_activity_at": fm.last_activity_at,
            "lifecycle_state": fm.lifecycle_state.value,
            "pinned": fm.pinned,
            "created": fm.created.isoformat() if fm.created else "",
        })
    return rows
