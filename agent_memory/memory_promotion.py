"""Short-term recall tracking and promotion cycle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_memory.search import SearchHit
from agent_memory.search.manager import MemorySearchManager
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

RECALL_TRACKER_RELATIVE_PATH = ".ai/short_term_recall.json"
PROMOTION_EVENTS_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/promotion_events.md"
PROMOTION_MARKER_PREFIX = "<!-- agent-memory-promotion:"
PROMOTION_MARKER_SUFFIX = "-->"
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
