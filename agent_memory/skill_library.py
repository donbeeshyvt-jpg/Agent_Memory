"""Skill library helpers with persona growth and maintenance."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

SKILL_INDEX_RELATIVE_PATH = "00_System/Skills/_SKILLS_INDEX.md"
SKILL_MAINTENANCE_REPORT_RELATIVE_PATH = "00_System/Skills/_MAINTENANCE_REPORT.md"
SKILL_EVENTS_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/skill_events.md"
SKILL_USAGE_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/skill_usage.jsonl"
PERSONA_SKILLS_BASE_RELATIVE_PATH = "00_System/Skills/_Persona"
TASK_COMPLETION_NOTE_RELATIVE_PATH = "10_Permanent/Facts/task_completion_log.md"

_SPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_HEADER_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+", re.MULTILINE)
_TASK_ID_RE = re.compile(r"task_id:\s*`([^`]+)`", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _slugify(text: str, *, fallback: str) -> str:
    cleaned = _SLUG_RE.sub("-", (text or "").strip()).strip("-").lower()
    return cleaned or fallback


def _normalize_text(text: str) -> str:
    return _SPACE_RE.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.removeprefix("# ").strip()
    return ""


def _resolve_source_path(vault_root: Path, source_path: str) -> Path:
    source = Path(source_path).expanduser()
    if source.is_absolute():
        return source.resolve()
    return (Path(vault_root).expanduser().resolve() / source).resolve()


def _skill_path(scope: str, *, persona_id: str, skill_id: str) -> str:
    normalized_skill = _slugify(skill_id, fallback="skill")
    if scope == "persona":
        persona = _slugify(persona_id, fallback="core")
        return f"{PERSONA_SKILLS_BASE_RELATIVE_PATH}/{persona}/{normalized_skill}/SKILL.md"
    return f"00_System/Skills/{normalized_skill}/SKILL.md"


def _skill_scope_from_path(path: str) -> tuple[str, str]:
    normalized = path.replace("\\", "/").strip()
    prefix = f"{PERSONA_SKILLS_BASE_RELATIVE_PATH}/"
    if normalized.startswith(prefix):
        rest = normalized.removeprefix(prefix)
        parts = rest.split("/")
        if len(parts) >= 3:
            return "persona", parts[0]
    return "shared", ""


def _extract_skill_id(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    if not normalized.endswith("/SKILL.md"):
        return ""
    parts = normalized.split("/")
    if len(parts) < 2:
        return ""
    return parts[-2]


def _append_skill_event(vault_root: Path, payload: dict[str, Any]) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / SKILL_EVENTS_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, "# skill_events\n\n")
    block = (
        f"## {payload.get('timestamp', _now_iso())}\n\n"
        f"- event: `{payload.get('event', '')}`\n"
        f"- skill_id: `{payload.get('skill_id', '')}`\n"
        f"- operator: `{payload.get('operator', '')}`\n"
        f"- detail: {payload.get('detail', '')}\n\n"
    )
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + block)
    return target


def _append_usage(vault_root: Path, payload: dict[str, Any]) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / SKILL_USAGE_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    if not target.exists():
        atomic_write(target, line)
        return target
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + line)
    return target


def _completed_task_ids(adapter: ObsidianVaultAdapter) -> set[str]:
    note = adapter.read_note(TASK_COMPLETION_NOTE_RELATIVE_PATH)
    if note is None:
        return set()
    ids = set()
    for match in _TASK_ID_RE.finditer(note.body):
        raw = str(match.group(1)).strip()
        if raw:
            ids.add(_slugify(raw, fallback=""))
    return ids


def record_skill_usage(
    vault_root: Path,
    *,
    persona_id: str,
    skill_id: str,
    scope: str = "auto",
    operator: str = "agent",
    success: bool | None = None,
    note: str = "",
    resolved_task_id: str = "",
    resolved_for: str = "user",
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    persona = _slugify(persona_id, fallback="core")
    resolved_scope = scope.strip().lower()
    if resolved_scope not in {"auto", "persona", "shared"}:
        resolved_scope = "auto"
    sid = _slugify(skill_id, fallback="skill")

    resolved_path = ""
    if resolved_scope in {"auto", "persona"}:
        candidate = _skill_path("persona", persona_id=persona, skill_id=sid)
        if adapter.read_note(candidate) is not None:
            resolved_scope = "persona"
            resolved_path = candidate
    if not resolved_path:
        candidate = _skill_path("shared", persona_id=persona, skill_id=sid)
        if adapter.read_note(candidate) is not None:
            resolved_scope = "shared"
            resolved_path = candidate
    if not resolved_path:
        resolved_scope = "missing"
        resolved_path = f"(not_found){sid}"

    resolved_task = _slugify(resolved_task_id, fallback="") if resolved_task_id else ""
    resolved_for_norm = str(resolved_for).strip().lower()
    if resolved_for_norm not in {"user", "persona"}:
        resolved_for_norm = "user"
    verified = False
    if resolved_task:
        verified = resolved_task in _completed_task_ids(adapter)
    counts_for_growth = bool(success is True and verified and resolved_for_norm in {"user", "persona"})

    payload = {
        "timestamp": _now_iso(),
        "persona_id": persona,
        "skill_id": sid,
        "scope": resolved_scope,
        "skill_path": resolved_path,
        "operator": _slugify(operator, fallback="agent"),
        "success": success,
        "resolved_task_id": resolved_task,
        "resolved_for": resolved_for_norm,
        "resolution_verified": verified,
        "counts_for_growth": counts_for_growth,
        "note": note.strip(),
    }
    _append_usage(root, payload)
    return payload


def _load_usage_entries(vault_root: Path) -> list[dict[str, Any]]:
    root = Path(vault_root).expanduser().resolve()
    target = (root / SKILL_USAGE_RELATIVE_PATH).resolve()
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def list_skills(
    vault_root: Path,
    *,
    scope: str = "all",
    persona_id: str | None = None,
) -> list[dict[str, str]]:
    adapter = ObsidianVaultAdapter(Path(vault_root).expanduser().resolve())
    adapter.ensure_skeleton()
    scope_norm = scope.strip().lower()
    if scope_norm not in {"all", "shared", "persona"}:
        scope_norm = "all"
    persona_norm = _slugify(persona_id or "", fallback="") if persona_id else ""

    paths = adapter.list_notes(MemoryType.SKILL)
    rows: list[dict[str, str]] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if not normalized.endswith("/SKILL.md"):
            continue
        note = adapter.read_note(normalized)
        if note is None:
            continue
        sid = _extract_skill_id(normalized)
        if not sid:
            continue
        item_scope, item_persona = _skill_scope_from_path(normalized)
        if scope_norm == "shared" and item_scope != "shared":
            continue
        if scope_norm == "persona" and item_scope != "persona":
            continue
        if persona_norm and item_scope == "persona" and item_persona != persona_norm:
            continue
        rows.append(
            {
                "skill_id": sid,
                "path": normalized,
                "scope": item_scope,
                "persona_id": item_persona,
                "updated_at": note.frontmatter.updated.isoformat(),
                "status": note.frontmatter.status,
                "agent": note.frontmatter.agent,
            }
        )
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return rows


def refresh_skill_index(vault_root: Path) -> str:
    adapter = ObsidianVaultAdapter(Path(vault_root).expanduser().resolve())
    adapter.ensure_skeleton()
    rows = list_skills(adapter.vault_root, scope="all")
    shared = sum(1 for row in rows if row["scope"] == "shared")
    persona = len(rows) - shared
    lines = [
        "# SKILLS_INDEX / 技能索引",
        "",
        f"- total: `{len(rows)}`",
        f"- shared: `{shared}`",
        f"- persona_private: `{persona}`",
        f"- updated_at: `{_now_iso()}`",
        "",
    ]
    if not rows:
        lines.append("- （目前無技能）")
    else:
        for idx, row in enumerate(rows, start=1):
            persona_suffix = f" persona={row['persona_id']}" if row["scope"] == "persona" else ""
            lines.append(
                f"{idx}. `{row['skill_id']}` | scope={row['scope']}{persona_suffix} | "
                f"status={row['status']} | updated={row['updated_at']} | path=`{row['path']}`"
            )
    note = MemoryNote(
        path=SKILL_INDEX_RELATIVE_PATH,
        frontmatter=Frontmatter(
            type=MemoryType.SKILL,
            source=MemorySource.AGENT,
            tags=["skills", "index"],
            agent="skill-library",
            extras={"generated": True},
        ),
        body="\n".join(lines).rstrip() + "\n",
    )
    adapter.write_note(note)
    return SKILL_INDEX_RELATIVE_PATH


def _build_normalized_skill_markdown(
    *,
    skill_id: str,
    title: str,
    owner_persona: str,
    source_ref: str,
    raw_content: str,
) -> str:
    compact = raw_content.strip()
    if len(compact) > 12000:
        compact = compact[:12000] + "\n\n...(truncated)...\n"
    return (
        f"# {skill_id} / {title}\n\n"
        "## Purpose\n\n"
        f"- 由 `{source_ref}` 內化成系統技能格式。\n"
        f"- owner_persona: `{owner_persona}`\n\n"
        "## Trigger\n\n"
        "- 當任務與此技能描述相符時，可提案套用。\n"
        "- 若缺關鍵參數，先向使用者提問再執行。\n\n"
        "## Steps\n\n"
        "1. 讀取任務與上下文。\n"
        "2. 套用技能步驟並產生可追蹤輸出。\n"
        "3. 回寫必要記錄（任務/記憶/台帳）。\n\n"
        "## Raw Source\n\n"
        "```markdown\n"
        f"{compact}\n"
        "```\n"
    )


def ingest_skill_file(
    vault_root: Path,
    *,
    source_path: str,
    skill_id: str | None = None,
    owner_persona: str = "core",
    operator: str = "user",
    overwrite: bool = False,
    scope: str = "shared",
    persona_id: str | None = None,
) -> dict[str, str]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()

    source_abs = _resolve_source_path(root, source_path)
    if not source_abs.exists() or not source_abs.is_file():
        raise ValueError(f"source_path 不存在：{source_abs}")
    raw = source_abs.read_text(encoding="utf-8-sig")
    heading = _first_heading(raw)
    stem = source_abs.stem
    resolved_skill_id = _slugify(skill_id or heading or stem, fallback="skill")
    scope_norm = scope.strip().lower()
    if scope_norm not in {"shared", "persona"}:
        scope_norm = "shared"
    persona = _slugify(persona_id or owner_persona, fallback="core")
    target_path = _skill_path(scope_norm, persona_id=persona, skill_id=resolved_skill_id)
    if adapter.read_note(target_path) is not None and not overwrite:
        raise ValueError(f"技能已存在：{resolved_skill_id}（可加 --overwrite）")

    body = _build_normalized_skill_markdown(
        skill_id=resolved_skill_id,
        title=heading or stem,
        owner_persona=_slugify(owner_persona, fallback="core"),
        source_ref=str(source_abs),
        raw_content=raw,
    )
    note = MemoryNote(
        path=target_path,
        frontmatter=Frontmatter(
            type=MemoryType.SKILL,
            source=MemorySource.USER,
            tags=["skill", "normalized", _slugify(owner_persona, fallback="core"), scope_norm],
            agent=_slugify(operator, fallback="user"),
            extras={
                "source_path": str(source_abs),
                "owner_persona": _slugify(owner_persona, fallback="core"),
                "scope": scope_norm,
                "persona_id": persona if scope_norm == "persona" else "",
                "normalized_at": _now_iso(),
            },
        ),
        body=body,
    )
    adapter.write_note(note)
    refresh_skill_index(root)
    _append_skill_event(
        root,
        {
            "timestamp": _now_iso(),
            "event": "skill_ingested",
            "skill_id": resolved_skill_id,
            "operator": _slugify(operator, fallback="user"),
            "detail": f"scope={scope_norm} persona={persona} source={source_abs}",
        },
    )
    return {"skill_id": resolved_skill_id, "skill_path": target_path}


def merge_skills(
    vault_root: Path,
    *,
    target_skill_id: str,
    source_skill_ids: list[str],
    operator: str = "agent",
    archive_sources: bool = True,
) -> dict[str, Any]:
    if not source_skill_ids:
        raise ValueError("source_skill_ids 不可為空")
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    target_id = _slugify(target_skill_id, fallback="merged-skill")
    source_ids = [_slugify(item, fallback="") for item in source_skill_ids]
    source_ids = [item for item in source_ids if item]
    if not source_ids:
        raise ValueError("source_skill_ids 不可為空")

    source_notes: list[tuple[str, MemoryNote]] = []
    for sid in source_ids:
        path = _skill_path("shared", persona_id="core", skill_id=sid)
        note = adapter.read_note(path)
        if note is None:
            raise ValueError(f"找不到共享技能：{sid}")
        source_notes.append((sid, note))

    lines = [
        f"# {target_id} / merged skill",
        "",
        "## Purpose",
        "",
        "- 合併多個相近技能，避免重複與碎片化。",
        "- 若任務需求不足，先向使用者確認關鍵參數。",
        "",
        "## Merged From",
        "",
    ]
    for sid, note in source_notes:
        lines.append(f"- `{sid}` ({note.path})")
    lines.append("")
    lines.append("## Combined Playbook")
    lines.append("")
    for sid, note in source_notes:
        excerpt = note.body.strip()
        if len(excerpt) > 4000:
            excerpt = excerpt[:4000] + "\n\n...(truncated)..."
        lines.append(f"### Source: {sid}")
        lines.append("")
        lines.append("```markdown")
        lines.append(excerpt)
        lines.append("```")
        lines.append("")

    target_path = _skill_path("shared", persona_id="core", skill_id=target_id)
    target_note = MemoryNote(
        path=target_path,
        frontmatter=Frontmatter(
            type=MemoryType.SKILL,
            source=MemorySource.AGENT,
            tags=["skill", "merged", "shared"],
            agent=_slugify(operator, fallback="agent"),
            extras={"merged_from": source_ids, "merged_at": _now_iso()},
        ),
        body="\n".join(lines).rstrip() + "\n",
    )
    adapter.write_note(target_note)
    archived: list[str] = []
    if archive_sources:
        for sid, note in source_notes:
            if sid == target_id:
                continue
            adapter.archive_note(note.path, reason=f"merged_into:{target_id}")
            archived.append(sid)
    refresh_skill_index(root)
    _append_skill_event(
        root,
        {
            "timestamp": _now_iso(),
            "event": "skill_merged",
            "skill_id": target_id,
            "operator": _slugify(operator, fallback="agent"),
            "detail": f"from={','.join(source_ids)} archive_sources={archive_sources}",
        },
    )
    return {"target_skill_id": target_id, "target_skill_path": target_path, "archived_sources": archived}


def evaluate_skill_completeness(markdown: str) -> dict[str, Any]:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lowered = text.lower()
    checks = {
        "has_heading": bool(_HEADER_RE.search(text)),
        "has_purpose": "## purpose" in lowered,
        "has_trigger": "## trigger" in lowered,
        "has_steps": "## steps" in lowered,
        "has_enough_steps": len(_STEP_RE.findall(text)) >= 3,
        "has_enough_length": len(_normalize_text(text)) >= 240,
    }
    weights = {
        "has_heading": 0.1,
        "has_purpose": 0.2,
        "has_trigger": 0.15,
        "has_steps": 0.2,
        "has_enough_steps": 0.2,
        "has_enough_length": 0.15,
    }
    score = 0.0
    missing: list[str] = []
    for key, ok in checks.items():
        if ok:
            score += weights.get(key, 0.0)
        else:
            missing.append(key)
    return {"score": round(score, 4), "checks": checks, "missing": missing}


def promote_persona_skill(
    vault_root: Path,
    *,
    persona_id: str,
    skill_id: str,
    operator: str = "agent",
    min_score: float = 0.75,
    force: bool = False,
    archive_source: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    persona = _slugify(persona_id, fallback="core")
    sid = _slugify(skill_id, fallback="skill")
    source_path = _skill_path("persona", persona_id=persona, skill_id=sid)
    source_note = adapter.read_note(source_path)
    if source_note is None:
        raise ValueError(f"找不到人格技能：{source_path}")
    completeness = evaluate_skill_completeness(source_note.body)
    score = float(completeness.get("score", 0.0))
    if not force and score < float(min_score):
        raise ValueError(f"技能完整度不足（score={score:.2f}, min={min_score:.2f}）")

    target_path = _skill_path("shared", persona_id="core", skill_id=sid)
    target_note = adapter.read_note(target_path)
    if target_note is not None and not force:
        raise ValueError(f"共享技能已存在：{target_path}（可加 --force）")

    body = source_note.body.rstrip() + (
        "\n\n## Promotion Trace\n\n"
        f"- promoted_from_persona: `{persona}`\n"
        f"- promoted_from_path: `{source_path}`\n"
        f"- promoted_at: `{_now_iso()}`\n"
        f"- completeness_score: `{score:.2f}`\n"
        f"- promoted_by: `{_slugify(operator, fallback='agent')}`\n"
    )
    note = MemoryNote(
        path=target_path,
        frontmatter=Frontmatter(
            type=MemoryType.SKILL,
            source=MemorySource.AGENT,
            tags=["skill", "shared", "promoted"],
            agent=_slugify(operator, fallback="agent"),
            extras={
                "promoted_from_persona": persona,
                "promoted_from_path": source_path,
                "promoted_at": _now_iso(),
                "completeness_score": score,
                "force": bool(force),
            },
        ),
        body=body,
    )
    adapter.write_note(note)
    if archive_source:
        adapter.archive_note(source_path, reason=f"promoted_to_shared:{sid}")
    refresh_skill_index(root)
    _append_skill_event(
        root,
        {
            "timestamp": _now_iso(),
            "event": "skill_promoted",
            "skill_id": sid,
            "operator": _slugify(operator, fallback="agent"),
            "detail": f"persona={persona} score={score:.2f} force={bool(force)}",
        },
    )
    return {
        "skill_id": sid,
        "source_path": source_path,
        "target_path": target_path,
        "completeness_score": score,
    }


def _to_datetime(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_skill_maintenance(
    vault_root: Path,
    *,
    maintainer_persona: str = "skill-curator",
    operator: str = "skill-curator",
    lookback_days: int = 30,
    min_usage: int = 5,
    min_completeness: float = 0.75,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()

    persona_rows = list_skills(root, scope="persona")
    usage_rows = _load_usage_entries(root)
    usage_since = _now() - timedelta(days=max(1, int(lookback_days)))
    usage_counter: dict[tuple[str, str], int] = {}
    for item in usage_rows:
        if not isinstance(item, dict):
            continue
        ts = _to_datetime(str(item.get("timestamp", "")))
        if ts is None or ts < usage_since:
            continue
        persona = _slugify(str(item.get("persona_id", "")), fallback="")
        sid = _slugify(str(item.get("skill_id", "")), fallback="")
        scope = str(item.get("scope", ""))
        if not persona or not sid or scope not in {"persona", "auto"}:
            continue
        growth_ok = bool(item.get("counts_for_growth", False))
        if not growth_ok:
            legacy_success = item.get("success", None)
            legacy_for = str(item.get("resolved_for", "")).strip().lower()
            legacy_verified = bool(item.get("resolution_verified", False))
            growth_ok = bool(legacy_success is True and legacy_verified and legacy_for in {"user", "persona"})
        if not growth_ok:
            continue
        key = (persona, sid)
        usage_counter[key] = usage_counter.get(key, 0) + 1

    promoted: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in persona_rows:
        if row.get("status", "active") != "active":
            continue
        persona = _slugify(row.get("persona_id", ""), fallback="")
        sid = _slugify(row.get("skill_id", ""), fallback="")
        if not persona or not sid:
            continue
        usage_count = usage_counter.get((persona, sid), 0)
        if usage_count < max(1, int(min_usage)):
            continue
        note = adapter.read_note(row.get("path", ""))
        if note is None:
            continue
        completeness = evaluate_skill_completeness(note.body)
        score = float(completeness.get("score", 0.0))
        candidate = {
            "persona_id": persona,
            "skill_id": sid,
            "usage_count": usage_count,
            "completeness_score": score,
            "source_path": row.get("path", ""),
        }
        candidates.append(candidate)
        if score < float(min_completeness):
            skipped.append({**candidate, "reason": "completeness_below_threshold"})
            continue
        target_path = _skill_path("shared", persona_id="core", skill_id=sid)
        if adapter.read_note(target_path) is not None:
            skipped.append({**candidate, "reason": "shared_exists"})
            continue
        if dry_run:
            promoted.append({**candidate, "target_path": target_path, "dry_run": True})
            continue
        result = promote_persona_skill(
            root,
            persona_id=persona,
            skill_id=sid,
            operator=operator,
            min_score=min_completeness,
            force=False,
            archive_source=False,
        )
        promoted.append({**candidate, "target_path": result["target_path"], "dry_run": False})

    report_lines = [
        "# SKILL_MAINTENANCE_REPORT / 技能庫維護報告",
        "",
        f"- generated_at: `{_now_iso()}`",
        f"- maintainer_persona: `{_slugify(maintainer_persona, fallback='skill-curator')}`",
        f"- lookback_days: `{max(1, int(lookback_days))}`",
        f"- min_usage: `{max(1, int(min_usage))}`",
        f"- min_completeness: `{float(min_completeness):.2f}`",
        f"- dry_run: `{bool(dry_run)}`",
        "- growth_rule: `success=true + resolved_task_id + completion_verified`",
        "",
        f"- candidates: `{len(candidates)}`",
        f"- promoted: `{len(promoted)}`",
        f"- skipped: `{len(skipped)}`",
        "",
        "## Promoted",
        "",
    ]
    if not promoted:
        report_lines.append("- （無）")
    else:
        for item in promoted:
            report_lines.append(
                f"- `{item['skill_id']}` from `{item['persona_id']}` | "
                f"usage={item['usage_count']} | score={item['completeness_score']:.2f} | "
                f"target=`{item['target_path']}`"
            )
    report_lines.extend(["", "## Skipped", ""])
    if not skipped:
        report_lines.append("- （無）")
    else:
        for item in skipped:
            report_lines.append(
                f"- `{item['skill_id']}` from `{item['persona_id']}` | "
                f"usage={item['usage_count']} | score={item['completeness_score']:.2f} | "
                f"reason={item['reason']}"
            )
    report = MemoryNote(
        path=SKILL_MAINTENANCE_REPORT_RELATIVE_PATH,
        frontmatter=Frontmatter(
            type=MemoryType.SKILL,
            source=MemorySource.AGENT,
            tags=["skills", "maintenance"],
            agent=_slugify(operator, fallback="skill-curator"),
            extras={
                "maintainer_persona": _slugify(maintainer_persona, fallback="skill-curator"),
                "promoted_count": len(promoted),
                "skipped_count": len(skipped),
            },
        ),
        body="\n".join(report_lines).rstrip() + "\n",
    )
    adapter.write_note(report)
    refresh_skill_index(root)
    _append_skill_event(
        root,
        {
            "timestamp": _now_iso(),
            "event": "skill_maintenance_run",
            "skill_id": "maintenance",
            "operator": _slugify(operator, fallback="skill-curator"),
            "detail": (
                f"candidates={len(candidates)} promoted={len(promoted)} "
                f"skipped={len(skipped)} dry_run={bool(dry_run)}"
            ),
        },
    )
    return {
        "report_path": SKILL_MAINTENANCE_REPORT_RELATIVE_PATH,
        "candidates": candidates,
        "promoted": promoted,
        "skipped": skipped,
    }


def _tokenize_query(text: str) -> list[str]:
    raw = _normalize_text(text).lower()
    return [token for token in raw.split(" ") if token]


def _extract_purpose_summary(markdown: str, *, limit: int = 180) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    capture = False
    chunks: list[str] = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower == "## purpose":
            capture = True
            continue
        if capture and stripped.startswith("## "):
            break
        if capture and stripped:
            chunks.append(stripped.lstrip("- ").strip())
            if len(" ".join(chunks)) >= limit:
                break
    if not chunks:
        compact = _normalize_text(text)
        return compact[:limit] + ("..." if len(compact) > limit else "")
    summary = _normalize_text(" ".join(chunks))
    return summary[:limit] + ("..." if len(summary) > limit else "")


def build_skill_prompt_context(
    vault_root: Path,
    *,
    persona_id: str,
    query: str,
    max_results: int = 4,
) -> tuple[str, list[dict[str, Any]]]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    persona = _slugify(persona_id, fallback="core")
    tokens = _tokenize_query(query)
    rows = list_skills(root, scope="all", persona_id=persona)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status", "active") != "active":
            continue
        path = row.get("path", "")
        note = adapter.read_note(path)
        if note is None:
            continue
        text = (path + "\n" + note.body[:4000]).lower()
        token_hits = sum(1 for token in tokens if token and token in text)
        if tokens and token_hits <= 0:
            continue
        scope_priority = 0 if row.get("scope", "shared") == "persona" else 1
        score = token_hits * 10 + (5 if scope_priority == 0 else 0)
        candidates.append(
            {
                "skill_id": row.get("skill_id", ""),
                "path": path,
                "scope": row.get("scope", "shared"),
                "persona_id": row.get("persona_id", ""),
                "score": score,
                "summary": _extract_purpose_summary(note.body),
            }
        )
    if not tokens:
        candidates.sort(key=lambda item: (0 if item["scope"] == "persona" else 1, item["skill_id"]))
    else:
        candidates.sort(key=lambda item: (-int(item["score"]), 0 if item["scope"] == "persona" else 1))

    selected = candidates[: max(1, int(max_results))]
    if not selected:
        return "", []
    lines = [
        "以下是目前可優先參考的技能（人格私有優先，共用次之）：",
        "",
    ]
    for item in selected:
        scope = str(item.get("scope", "shared"))
        persona_tag = f" persona={item.get('persona_id', '')}" if scope == "persona" else ""
        lines.append(
            f"- `{item.get('skill_id', '')}` | scope={scope}{persona_tag} | "
            f"path=`{item.get('path', '')}` | summary={item.get('summary', '')}"
        )
    lines.append("")
    return "\n".join(lines), selected
