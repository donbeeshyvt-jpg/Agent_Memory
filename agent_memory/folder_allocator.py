"""Folder allocation governance with non-skipping indices and ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import yaml

from agent_memory.folder_labels import ensure_dir_info_file, folder_display_name
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

_DIR_PREFIX_RE = r"^(\d{2})_([A-Za-z0-9][A-Za-z0-9_-]*)$"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_relative(path: str) -> str:
    if not path or path == ".":
        return ""
    return path.replace("\\", "/").strip("/").strip()


def _slugify_english_slug(raw: str) -> str:
    text = raw.strip().replace(" ", "_")
    keep = []
    for ch in text:
        if ch.isalnum() or ch in {"_", "-"}:
            keep.append(ch)
    slug = "".join(keep).strip("_-")
    return slug or "untitled"


def _candidate_indices(entries: Iterable[Path]) -> list[int]:
    import re

    pattern = re.compile(_DIR_PREFIX_RE)
    values: list[int] = []
    for item in entries:
        if not item.is_dir():
            continue
        match = pattern.match(item.name)
        if not match:
            continue
        values.append(int(match.group(1)))
    return sorted(set(values))


def _first_available(indices: list[int], base_index: int) -> int:
    if base_index < 0 or base_index > 99:
        raise ValueError("base_index 必須在 0~99")
    candidate = base_index
    taken = set(indices)
    while candidate in taken:
        candidate += 1
        if candidate > 99:
            raise ValueError("可用序號已耗盡（>99）")
    return candidate


@dataclass(slots=True)
class AllocationDecision:
    """Single folder allocation decision record."""

    decision_id: str
    timestamp: str
    source_path: str
    source_hash: str
    topic_label: str
    parent_family: int
    candidate_folders: list[str] = field(default_factory=list)
    best_match_folder: str | None = None
    best_match_score: float | None = None
    decision_type: str = "merge_existing"
    target_folder: str = ""
    display_folder: str = ""
    next_subindex_used: int | None = None
    trigger_conditions: dict[str, Any] = field(default_factory=dict)
    override_by_user: bool = False
    reason: str = ""
    operator: str = "agent"

    def to_ledger_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "topic_label": self.topic_label,
            "parent_family": self.parent_family,
            "candidate_folders": self.candidate_folders,
            "best_match_folder": self.best_match_folder,
            "best_match_score": self.best_match_score,
            "decision_type": self.decision_type,
            "target_folder": self.target_folder,
            "display_folder": self.display_folder,
            "next_subindex_used": self.next_subindex_used,
            "trigger_conditions": self.trigger_conditions,
            "override_by_user": self.override_by_user,
            "reason": self.reason,
            "operator": self.operator,
        }


class FolderAllocator:
    """Govern folder allocation decisions and ledger writes."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve()

    def allocate(
        self,
        *,
        parent_relative: str,
        english_slug: str,
        zh_purpose: str,
        source_path: str,
        source_hash: str,
        topic_label: str,
        base_index: int,
        reason: str,
        operator: str = "agent",
        override_by_user: bool = False,
        trigger_conditions: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> AllocationDecision:
        parent_rel = _normalize_relative(parent_relative)
        parent_abs = self._absolute(parent_rel)
        parent_abs.mkdir(parents=True, exist_ok=True)

        clean_slug = _slugify_english_slug(english_slug)
        candidates = [entry.name for entry in parent_abs.iterdir() if entry.is_dir()]
        candidates.sort()

        existing = self._find_existing_slug(parent_abs, clean_slug)
        timestamp = _iso(_utc_now())
        decision_id = f"alloc-{timestamp}-{uuid4().hex[:6]}"
        trigger_conditions = trigger_conditions or {}

        if existing is not None:
            target_rel = self._join_relative(parent_rel, existing.name)
            display = folder_display_name(target_rel, zh_purpose=zh_purpose)
            decision = AllocationDecision(
                decision_id=decision_id,
                timestamp=timestamp,
                source_path=source_path,
                source_hash=source_hash,
                topic_label=topic_label,
                parent_family=base_index,
                candidate_folders=candidates,
                best_match_folder=target_rel,
                best_match_score=1.0,
                decision_type="manual_override" if override_by_user else "merge_existing",
                target_folder=target_rel,
                display_folder=display,
                next_subindex_used=None,
                trigger_conditions=trigger_conditions,
                override_by_user=override_by_user,
                reason=reason or "命中同名 slug，優先合併既有資料夾。",
                operator=operator,
            )
            if not dry_run:
                self._append_ledger(decision)
                ensure_dir_info_file(self.vault_root, target_rel, zh_purpose=zh_purpose, overwrite=False)
            return decision

        indices = _candidate_indices(parent_abs.iterdir())
        next_index = _first_available(indices, base_index)
        folder_name = f"{next_index:02d}_{clean_slug}"
        folder_rel = self._join_relative(parent_rel, folder_name)
        display = folder_display_name(folder_rel, zh_purpose=zh_purpose)

        decision = AllocationDecision(
            decision_id=decision_id,
            timestamp=timestamp,
            source_path=source_path,
            source_hash=source_hash,
            topic_label=topic_label,
            parent_family=base_index,
            candidate_folders=candidates,
            best_match_folder=None,
            best_match_score=None,
            decision_type="create_new_subfolder",
            target_folder=folder_rel,
            display_folder=display,
            next_subindex_used=next_index,
            trigger_conditions=trigger_conditions,
            override_by_user=override_by_user,
            reason=reason or "無可合併資料夾，依最小可用序號建立新資料夾。",
            operator=operator,
        )
        if not dry_run:
            # Governance rule: ledger first, then file-system write.
            self._append_ledger(decision)
            self._absolute(folder_rel).mkdir(parents=True, exist_ok=True)
            ensure_dir_info_file(self.vault_root, folder_rel, zh_purpose=zh_purpose, overwrite=False)
        return decision

    def _absolute(self, relative: str) -> Path:
        candidate = (self.vault_root / relative).resolve()
        try:
            candidate.relative_to(self.vault_root)
        except ValueError as exc:
            raise ValueError(f"路徑跳脫 vault：{relative}") from exc
        return candidate

    def _join_relative(self, parent: str, child: str) -> str:
        if not parent:
            return child
        return f"{parent}/{child}"

    def _find_existing_slug(self, parent_abs: Path, slug: str) -> Path | None:
        suffix = f"_{slug}"
        for entry in parent_abs.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.endswith(suffix):
                return entry
        return None

    def _append_ledger(self, decision: AllocationDecision) -> None:
        ledger_path = self._absolute(".ai/folder_allocations.md")
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        if not ledger_path.exists():
            atomic_write(ledger_path, "# Folder Allocation Ledger\n\n")

        payload = decision.to_ledger_payload()
        yaml_block = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
        block = f"\n---\n{yaml_block}\n---\n"

        with file_lock(ledger_path, timeout=5.0):
            existing = ledger_path.read_text(encoding="utf-8")
            atomic_write(ledger_path, existing.rstrip() + "\n" + block)
