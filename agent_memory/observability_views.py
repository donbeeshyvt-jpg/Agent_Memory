"""Observability views — R10 C36: 統一觀察工具入口.

把 R7-R9 散在 4 個 .ai/pending_*.json 的提議/gap 整成一致 view, 給 CLI sub-command +
chat /pending 之類入口共用. 全部唯讀, 不改 state.

對應 HANDOFF §3.2 observability + MISSION §3.2 「Obsidian 可看」(此檔是 CLI/JSON 側,
markdown 側 C37 寫 09_Index/Recent_Updates.md).

4 個來源:
- .ai/pending_skill_suggestions.json    (R7 C20b — skill 升格提議)
- .ai/pending_umbrella_suggestions.json (R9 C27 — umbrella 合併建議)
- .ai/pending_procedure_tag_suggestions.json (R9 C27 — procedure tag 自動標)
- .ai/pending_user_gaps.json            (R8 C24 + R9 C30 — USER.md gap + contradiction)

每個 view 接 include_resolved (預設 False = 只看 pending), 並把 source schema 抹平成
{id, status, summary, proposed_at, ...} 一致欄位給 CLI 印出.

設計重點: 不另開 .ai/ 檔, 只是 read-only aggregator. 後續若要加新 pending 來源
(例如 R10 contradiction subkind), 改本檔 + 對應 _normalize_* 函式即可.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _is_resolved_skill(entry: dict[str, Any]) -> bool:
    return bool(entry.get("dismissed_at")) or bool(entry.get("promoted_to"))


def _is_resolved_umbrella(entry: dict[str, Any]) -> bool:
    return bool(entry.get("dismissed_at")) or bool(entry.get("accepted_at"))


def _is_resolved_procedure(entry: dict[str, Any]) -> bool:
    return bool(entry.get("dismissed_at")) or bool(entry.get("applied_at"))


def _is_resolved_gap(entry: dict[str, Any]) -> bool:
    return bool(entry.get("dismissed_at")) or bool(entry.get("resolved_at"))


def list_skill_suggestions(
    vault_root: Path,
    *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """R7 C20b — pending skill 升格提議."""
    from agent_memory.skill_suggestions import load_pending

    pending = load_pending(Path(vault_root))
    return [s for s in pending if include_resolved or not _is_resolved_skill(s)]


def list_umbrella_suggestions(
    vault_root: Path,
    *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """R9 C27 — pending umbrella 合併建議 (LLM 整理產).

    Schema: {umbrella_id, members[], reason, proposed_at, accepted_at, dismissed_at}
    """
    from agent_memory.umbrella_llm import load_pending_umbrella

    pending = load_pending_umbrella(Path(vault_root))
    return [s for s in pending if include_resolved or not _is_resolved_umbrella(s)]


def list_procedure_tag_suggestions(
    vault_root: Path,
    *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """R9 C27 — pending procedure tag 自動標建議 (LLM 整理產).

    Schema: {entity_id, reason, proposed_at, applied_at, dismissed_at}
    """
    from agent_memory.umbrella_llm import load_pending_procedure_tags

    pending = load_pending_procedure_tags(Path(vault_root))
    return [s for s in pending if include_resolved or not _is_resolved_procedure(s)]


def list_user_gaps(
    vault_root: Path,
    *,
    include_resolved: bool = False,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """R8 C24 + R9 C30 — pending USER.md gap 與 contradiction.

    kind 可選 filter: 'placeholder' / 'midterm_not_in_user' / 'contradiction'.
    None = 不 filter (全部 kind).
    """
    from agent_memory.gap_analysis import load_pending_gaps

    pending = load_pending_gaps(Path(vault_root))
    out: list[dict[str, Any]] = []
    for g in pending:
        if not include_resolved and _is_resolved_gap(g):
            continue
        if kind is not None and g.get("kind") != kind:
            continue
        out.append(g)
    return out


def list_contradictions(
    vault_root: Path,
    *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """R9 C30 — USER.md vs Mid_Term 矛盾偵測 (gap pool 內 kind=contradiction 子集)."""
    return list_user_gaps(vault_root, include_resolved=include_resolved, kind="contradiction")


def pending_overview(vault_root: Path) -> dict[str, Any]:
    """一次回所有 pending 數量 + 最舊一筆 proposed_at + 來源檔路徑.

    給 CLI `pending-overview` 用; chat /pending overview 也可接.
    回傳:
        {
            "skill_suggestions": {"pending": N, "resolved": M, "oldest_proposed_at": "...", "path": "..."},
            "umbrella": ...,
            "procedure_tags": ...,
            "user_gaps": {"pending": ..., "by_kind": {"placeholder": N, ...}, ...},
            "total_pending": int,
        }
    """
    root = Path(vault_root)

    def _stat(
        view_fn,
        relative_path: str,
    ) -> dict[str, Any]:
        pending = view_fn(root, include_resolved=False)
        all_entries = view_fn(root, include_resolved=True)
        resolved_count = len(all_entries) - len(pending)
        oldest = ""
        for e in pending:
            ts = e.get("proposed_at", "")
            if ts and (not oldest or ts < oldest):
                oldest = ts
        return {
            "pending": len(pending),
            "resolved": resolved_count,
            "oldest_proposed_at": oldest,
            "path": relative_path,
        }

    skill = _stat(list_skill_suggestions, ".ai/pending_skill_suggestions.json")
    umbrella = _stat(list_umbrella_suggestions, ".ai/pending_umbrella_suggestions.json")
    procedure = _stat(list_procedure_tag_suggestions, ".ai/pending_procedure_tag_suggestions.json")

    gaps_pending = list_user_gaps(root, include_resolved=False)
    gaps_all = list_user_gaps(root, include_resolved=True)
    by_kind: dict[str, int] = {}
    for g in gaps_pending:
        k = str(g.get("kind", "unknown"))
        by_kind[k] = by_kind.get(k, 0) + 1
    oldest_gap = ""
    for g in gaps_pending:
        ts = g.get("proposed_at", "")
        if ts and (not oldest_gap or ts < oldest_gap):
            oldest_gap = ts
    user_gaps = {
        "pending": len(gaps_pending),
        "resolved": len(gaps_all) - len(gaps_pending),
        "by_kind": by_kind,
        "oldest_proposed_at": oldest_gap,
        "path": ".ai/pending_user_gaps.json",
    }

    return {
        "skill_suggestions": skill,
        "umbrella": umbrella,
        "procedure_tags": procedure,
        "user_gaps": user_gaps,
        "total_pending": skill["pending"] + umbrella["pending"] + procedure["pending"] + user_gaps["pending"],
    }
