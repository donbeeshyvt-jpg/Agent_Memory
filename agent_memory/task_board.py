"""Collaborative task board for multi-persona coordination."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

TASK_BOARD_RELATIVE_PATH = "70_Active_Plans/Task_Board/tasks.yaml"
TASK_BOARD_VIEW_RELATIVE_PATH = "70_Active_Plans/Task_Board/TASKS.md"
TASK_EVENTS_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/task_events.md"
TASK_COMPLETION_RELATIVE_PATH = "10_Permanent/Facts/task_completion_log.md"

TASK_STATUSES = {"todo", "in_progress", "blocked", "done", "cancelled"}
_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _normalize_text(value: str, *, fallback: str) -> str:
    cleaned = _ID_RE.sub("-", (value or "").strip()).strip("-").lower()
    return cleaned or fallback


def _dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _render_board_markdown(payload: dict[str, Any]) -> str:
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []

    status_order = ["todo", "in_progress", "blocked", "done", "cancelled"]
    zh_map = {
        "todo": "待辦",
        "in_progress": "進行中",
        "blocked": "阻塞",
        "done": "完成",
        "cancelled": "取消",
    }
    lines = [
        "# TASK_BOARD / 協作任務板",
        "",
        f"- updated_at: `{payload.get('updated_at', '')}`",
        f"- total_tasks: `{len(tasks)}`",
        "",
    ]
    for status in status_order:
        bucket: list[dict[str, Any]] = []
        for task in tasks:
            if isinstance(task, dict) and str(task.get("status", "todo")) == status:
                bucket.append(task)
        lines.append(f"## {status.upper()} / {zh_map.get(status, status)} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("- （空）")
            lines.append("")
            continue
        for task in bucket:
            task_id = str(task.get("task_id", ""))
            title = str(task.get("title", "")).strip() or "(untitled)"
            supervisor = str(task.get("supervisor", "")).strip() or "n/a"
            assignees = task.get("assignees", [])
            if not isinstance(assignees, list):
                assignees = []
            done_items = 0
            total_items = 0
            checklist = task.get("checklist", [])
            if isinstance(checklist, list):
                total_items = len(checklist)
                for item in checklist:
                    if isinstance(item, dict) and bool(item.get("done", False)):
                        done_items += 1
            lines.append(
                f"- `{task_id}` {title} | supervisor={supervisor} | "
                f"assignees={','.join(str(x) for x in assignees) or 'n/a'} | "
                f"checklist={done_items}/{total_items}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"tsk-{stamp}-{uuid.uuid4().hex[:6]}"


def _normalize_people(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    for raw in values:
        text = _normalize_text(raw, fallback="")
        if text and text not in result:
            result.append(text)
    return result


def _append_task_event(vault_root: Path, payload: dict[str, Any]) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / TASK_EVENTS_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(
            target,
            "# task_events\n\n> 協作任務板事件台帳。\n\n",
        )
    block = (
        f"## {payload.get('timestamp', _now_iso())} {payload.get('event', 'event')}\n\n"
        f"- task_id: `{payload.get('task_id', '')}`\n"
        f"- operator: `{payload.get('operator', '')}`\n"
        f"- status: `{payload.get('status', '')}`\n"
        f"- detail: {payload.get('detail', '')}\n\n"
    )
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + block)
    return target


def _append_completion_memory(vault_root: Path, task: dict[str, Any]) -> str:
    adapter = ObsidianVaultAdapter(Path(vault_root).expanduser().resolve())
    path = TASK_COMPLETION_RELATIVE_PATH
    note = adapter.read_note(path)
    task_id = str(task.get("task_id", ""))
    title = str(task.get("title", ""))
    supervisor = str(task.get("supervisor", ""))
    assignees = task.get("assignees", [])
    if not isinstance(assignees, list):
        assignees = []
    detail = (
        f"- task_id: `{task_id}`\n"
        f"- title: {title}\n"
        f"- supervisor: {supervisor}\n"
        f"- assignees: {', '.join(str(x) for x in assignees)}\n"
        f"- completed_at: {task.get('completed_at', _now_iso())}\n"
    )
    section = f"## {task_id}\n\n{detail}\n"
    if note is None:
        note = MemoryNote(
            path=path,
            frontmatter=Frontmatter(
                type=MemoryType.LONG_TERM,
                source=MemorySource.AGENT,
                tags=["task", "completion"],
                agent="task-board",
                extras={"source": "task_board"},
            ),
            body="# task_completion_log\n\n" + section,
        )
    else:
        note.body = note.body.rstrip() + "\n\n" + section
    adapter.write_note(note)
    return path


def ensure_task_board_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    root = Path(vault_root).expanduser().resolve()
    target = (root / TASK_BOARD_RELATIVE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return target
    payload: dict[str, Any] = {
        "schema_version": 1,
        "board_id": "main",
        "tasks": [],
        "updated_at": _now_iso(),
    }
    atomic_write(target, _dump_yaml(payload))
    view = (root / TASK_BOARD_VIEW_RELATIVE_PATH).resolve()
    atomic_write(view, _render_board_markdown(payload))
    return target


def load_task_board(vault_root: Path) -> dict[str, Any]:
    path = ensure_task_board_file(vault_root)
    payload = _load_yaml(path)
    if not payload:
        payload = {"schema_version": 1, "board_id": "main", "tasks": [], "updated_at": _now_iso()}
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
    payload["tasks"] = tasks
    payload["updated_at"] = str(payload.get("updated_at", "")).strip() or _now_iso()
    return payload


def save_task_board(vault_root: Path, payload: dict[str, Any]) -> Path:
    root = Path(vault_root).expanduser().resolve()
    path = ensure_task_board_file(root)
    payload["updated_at"] = _now_iso()
    with file_lock(path, timeout=5.0):
        atomic_write(path, _dump_yaml(payload))
    view = (root / TASK_BOARD_VIEW_RELATIVE_PATH).resolve()
    atomic_write(view, _render_board_markdown(payload))
    return path


def create_task(
    vault_root: Path,
    *,
    title: str,
    supervisor: str,
    assignees: list[str] | None = None,
    watchers: list[str] | None = None,
    checklist: list[str] | None = None,
    detail: str = "",
    operator: str = "user",
    auto_cleanup: bool = True,
) -> dict[str, Any]:
    text = title.strip()
    if not text:
        raise ValueError("title 不可為空")
    board = load_task_board(vault_root)
    task = {
        "task_id": _task_id(),
        "title": text,
        "status": "todo",
        "supervisor": _normalize_text(supervisor, fallback="core"),
        "assignees": _normalize_people(assignees),
        "watchers": _normalize_people(watchers),
        "checklist": [
            {"text": str(item).strip(), "done": False}
            for item in (checklist or [])
            if str(item).strip()
        ],
        "detail": detail.strip(),
        "auto_cleanup": bool(auto_cleanup),
        "notes": [],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "completed_at": "",
        "completion_logged": False,
    }
    board["tasks"].append(task)
    save_task_board(vault_root, board)
    _append_task_event(
        vault_root,
        {
            "timestamp": _now_iso(),
            "event": "task_created",
            "task_id": task["task_id"],
            "operator": _normalize_text(operator, fallback="user"),
            "status": task["status"],
            "detail": task["title"],
        },
    )
    return task


def list_tasks(
    vault_root: Path,
    *,
    status: str | None = None,
    assignee: str | None = None,
    supervisor: str | None = None,
) -> list[dict[str, Any]]:
    board = load_task_board(vault_root)
    tasks = board.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
    status_norm = status.strip() if status else ""
    assignee_norm = _normalize_text(assignee or "", fallback="")
    supervisor_norm = _normalize_text(supervisor or "", fallback="")
    results: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if status_norm and str(task.get("status", "")) != status_norm:
            continue
        if assignee_norm:
            assignees = task.get("assignees", [])
            if not isinstance(assignees, list) or assignee_norm not in [str(x) for x in assignees]:
                continue
        if supervisor_norm and str(task.get("supervisor", "")) != supervisor_norm:
            continue
        results.append(task)
    return results


def set_task_status(
    vault_root: Path,
    *,
    task_id: str,
    status: str,
    operator: str = "agent",
    note: str = "",
) -> dict[str, Any]:
    status_norm = status.strip().lower()
    if status_norm not in TASK_STATUSES:
        raise ValueError(f"status 不合法：{status}")
    board = load_task_board(vault_root)
    tasks = board.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
    found: dict[str, Any] | None = None
    for item in tasks:
        if isinstance(item, dict) and str(item.get("task_id", "")) == task_id:
            found = item
            break
    if found is None:
        raise ValueError(f"找不到 task_id: {task_id}")

    found["status"] = status_norm
    found["updated_at"] = _now_iso()
    if note.strip():
        notes = found.get("notes", [])
        if not isinstance(notes, list):
            notes = []
        notes.append(
            {
                "timestamp": _now_iso(),
                "operator": _normalize_text(operator, fallback="agent"),
                "text": note.strip(),
            }
        )
        found["notes"] = notes
    if status_norm in {"done", "cancelled"}:
        found["completed_at"] = _now_iso()
        if not bool(found.get("completion_logged", False)):
            _append_completion_memory(vault_root, found)
            found["completion_logged"] = True

    auto_cleanup = bool(found.get("auto_cleanup", True))
    removed = False
    if status_norm in {"done", "cancelled"} and auto_cleanup:
        board["tasks"] = [item for item in tasks if not (isinstance(item, dict) and item.get("task_id") == task_id)]
        removed = True
    save_task_board(vault_root, board)
    _append_task_event(
        vault_root,
        {
            "timestamp": _now_iso(),
            "event": "task_status_changed",
            "task_id": task_id,
            "operator": _normalize_text(operator, fallback="agent"),
            "status": status_norm,
            "detail": note.strip() or ("auto_cleanup" if removed else ""),
        },
    )
    return {"task_id": task_id, "status": status_norm, "removed_from_active": removed}


def set_task_check(
    vault_root: Path,
    *,
    task_id: str,
    index: int,
    done: bool,
    operator: str = "agent",
) -> dict[str, Any]:
    board = load_task_board(vault_root)
    tasks = board.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
    target: dict[str, Any] | None = None
    for item in tasks:
        if isinstance(item, dict) and str(item.get("task_id", "")) == task_id:
            target = item
            break
    if target is None:
        raise ValueError(f"找不到 task_id: {task_id}")
    checklist = target.get("checklist", [])
    if not isinstance(checklist, list):
        checklist = []
    if index < 1 or index > len(checklist):
        raise ValueError(f"checklist index 超出範圍：{index}")
    cell = checklist[index - 1]
    if not isinstance(cell, dict):
        cell = {"text": str(cell), "done": False}
        checklist[index - 1] = cell
    cell["done"] = bool(done)
    target["updated_at"] = _now_iso()
    target["checklist"] = checklist
    save_task_board(vault_root, board)
    _append_task_event(
        vault_root,
        {
            "timestamp": _now_iso(),
            "event": "task_check_toggled",
            "task_id": task_id,
            "operator": _normalize_text(operator, fallback="agent"),
            "status": str(target.get("status", "todo")),
            "detail": f"index={index} done={bool(done)}",
        },
    )
    return {"task_id": task_id, "index": index, "done": bool(done)}


def prune_finished_tasks(vault_root: Path, *, operator: str = "agent") -> dict[str, Any]:
    board = load_task_board(vault_root)
    tasks = board.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status", "todo"))
        if status in {"done", "cancelled"}:
            if not bool(task.get("completion_logged", False)):
                _append_completion_memory(vault_root, task)
            removed.append(str(task.get("task_id", "")))
            continue
        kept.append(task)
    board["tasks"] = kept
    save_task_board(vault_root, board)
    for task_id in removed:
        _append_task_event(
            vault_root,
            {
                "timestamp": _now_iso(),
                "event": "task_pruned",
                "task_id": task_id,
                "operator": _normalize_text(operator, fallback="agent"),
                "status": "removed",
                "detail": "finished task pruned from active board",
            },
        )
    return {"removed": removed, "removed_count": len(removed)}
