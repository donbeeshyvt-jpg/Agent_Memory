"""End-to-end smoke suite for integration readiness."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from agent_memory.chat_session import append_chat_turn
from agent_memory.context_compaction import compact_session_memory
from agent_memory.chat_runtime import run_chat_turn
from agent_memory.hooks import write_event_logger
from agent_memory.llm_client import LLMClient
from agent_memory.memory_promotion import PromotionThresholds, run_promotion_cycle
from agent_memory.notion_queue import NOTION_QUEUE_EVENTS_RELATIVE, queue_notion_publish
from agent_memory.profile_scope import runtime_profile_for_persona
from agent_memory.runtime import MemoryRuntime, RuntimeProfile
from agent_memory.skill_library import (
    ingest_skill_file,
    record_skill_usage,
    run_skill_maintenance,
)
from agent_memory.task_board import create_task, set_task_status
from agent_memory.types import MemorySource
from agent_memory.vault import ObsidianVaultAdapter
from agent_memory.web_research import run_web_research


def _now_token() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _append_check(
    checks: list[dict[str, Any]],
    *,
    name: str,
    ok: bool,
    detail: str,
    required: bool = True,
    path: str = "",
) -> None:
    checks.append(
        {
            "name": name,
            "ok": bool(ok),
            "required": bool(required),
            "detail": detail,
            "path": path,
        }
    )


def run_smoke_suite(
    vault_root: Path,
    *,
    with_web: bool = False,
    with_llm: bool = False,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()

    runtime = MemoryRuntime(adapter, profile=RuntimeProfile())
    runtime.register_write_hook(write_event_logger(adapter.vault_root))

    token = f"smoke-{_now_token()}"
    checks: list[dict[str, Any]] = []
    task_id = ""
    skill_id = f"smoke-skill-{token}".lower()

    smoke_note_path = "10_Permanent/Facts/_smoke_suite.md"
    try:
        content = (
            "# smoke_suite\n\n"
            f"- token: `{token}`\n"
            "- purpose: integration readiness\n"
        )
        runtime.apply_memory_tool(
            action="replace",
            path=smoke_note_path,
            content=content,
            reason="smoke test write",
            agent="smoke-suite",
            source=MemorySource.AGENT,
            tags=["smoke", "integration"],
            extras={"token": token},
        )
        got = runtime.memory_get(path=smoke_note_path)
        ok = bool(got.ok and got.note and token in got.note.body)
        _append_check(
            checks,
            name="memory_write_read",
            ok=ok,
            detail="memory replace + memory_get",
            path=smoke_note_path,
        )
    except Exception as exc:  # noqa: BLE001
        _append_check(
            checks,
            name="memory_write_read",
            ok=False,
            detail=f"error: {exc}",
            path=smoke_note_path,
        )

    try:
        hits = runtime.memory_search(query=token, max_results=5, auto_reindex=True)
        hit_paths = [str(item.path) for item in hits]
        ok = smoke_note_path in hit_paths
        _append_check(
            checks,
            name="memory_search",
            ok=ok,
            detail=f"hits={len(hits)}",
            path=smoke_note_path,
        )
    except Exception as exc:  # noqa: BLE001
        _append_check(
            checks,
            name="memory_search",
            ok=False,
            detail=f"error: {exc}",
            path=smoke_note_path,
        )

    e2e_candidate_path = f"11_AI_Mirror/internalised_candidates/{token}-candidate.md"
    try:
        runtime.apply_memory_tool(
            action="replace",
            path=e2e_candidate_path,
            content=(
                f"# candidate {token}\n\n"
                "- 這是一條可被 promotion 的短期候選。\n"
                "- 決策：維持第二大腦 markdown 為記憶真值。\n"
            ),
            reason="smoke promotion candidate",
            agent="smoke-suite",
            source=MemorySource.MIRROR,
            tags=["smoke", "candidate"],
            extras={"token": token},
        )
        runtime.memory_search(query=f"candidate {token}", max_results=5, auto_reindex=True)
        runtime.memory_search(query=f"decision {token}", max_results=5, auto_reindex=False)

        for idx in range(6):
            append_chat_turn(
                adapter,
                persona_id="core",
                context_id="smoke",
                session_id=token,
                user_message=(
                    f"[{idx+1}] compact test token={token}。"
                    "這是一段用來驗證 pre-flush 與 compact 的冗長測試訊息，"
                    "需要讓會話檔長度超過門檻。"
                ),
                assistant_message="收到，將壓縮前摘要再保留近期回合，並記錄到 daily flush。",
            )
        compact = compact_session_memory(
            root,
            persona_id="core",
            context_id="smoke",
            session_id=token,
            max_chars=600,
            keep_recent_turns=3,
            use_llm_summary=False,
        )
        promote = run_promotion_cycle(
            root,
            phase="light",
            thresholds=PromotionThresholds(
                min_score=0.0,
                min_recall_count=1,
                min_unique_queries=1,
                min_unique_days=1,
                grace_period_hours=0.0,
            ),
            operator="smoke-suite",
            dry_run=False,
            max_promotions=5,
        )
        promoted = promote.get("promoted", [])
        ok = bool(compact.get("status") == "ok" and isinstance(promoted, list) and len(promoted) > 0)
        _append_check(
            checks,
            name="flush_promotion_flow",
            ok=ok,
            detail=(
                f"compact={compact.get('status', '')} "
                f"promoted={len(promoted) if isinstance(promoted, list) else 0}"
            ),
            path=e2e_candidate_path,
        )
    except Exception as exc:  # noqa: BLE001
        _append_check(
            checks,
            name="flush_promotion_flow",
            ok=False,
            detail=f"error: {exc}",
            path=e2e_candidate_path,
        )

    try:
        task = create_task(
            root,
            title=f"{token} integration task",
            supervisor="smoke-manager",
            assignees=["smoke-worker"],
            checklist=["run pipeline", "verify evidence"],
            detail="smoke suite collaborative task",
            operator="smoke-suite",
            auto_cleanup=True,
        )
        task_id = str(task.get("task_id", "")).strip()
        set_task_status(root, task_id=task_id, status="in_progress", operator="smoke-suite", note="started")
        set_task_status(root, task_id=task_id, status="done", operator="smoke-suite", note="completed")
        completion_note = adapter.read_note("10_Permanent/Facts/task_completion_log.md")
        verified = bool(completion_note and task_id and task_id in completion_note.body)
        _append_check(
            checks,
            name="task_board_flow",
            ok=verified,
            detail=f"task_id={task_id}",
            path="10_Permanent/Facts/task_completion_log.md",
        )
    except Exception as exc:  # noqa: BLE001
        _append_check(
            checks,
            name="task_board_flow",
            ok=False,
            detail=f"error: {exc}",
            path="70_Active_Plans/Task_Board/tasks.yaml",
        )

    try:
        manual_dir = adapter.absolute_path("11_AI_Mirror/external_ingest/manual_skills")
        manual_dir.mkdir(parents=True, exist_ok=True)
        source_path = manual_dir / f"{skill_id}.md"
        source_path.write_text(
            (
                f"# {skill_id}\n\n"
                "## Purpose\n\n"
                "- Validate skill growth evidence flow.\n\n"
                "## Trigger\n\n"
                "- Smoke integration checks.\n\n"
                "## Steps\n\n"
                "1. Confirm task completion evidence.\n"
                "2. Record skill usage.\n"
                "3. Produce maintenance candidate.\n"
            ),
            encoding="utf-8",
        )
        ingested = ingest_skill_file(
            root,
            source_path=str(source_path),
            skill_id=skill_id,
            owner_persona="smoke-worker",
            operator="smoke-suite",
            overwrite=True,
            scope="persona",
            persona_id="smoke-worker",
        )
        usage = record_skill_usage(
            root,
            persona_id="smoke-worker",
            skill_id=skill_id,
            scope="persona",
            operator="smoke-suite",
            success=True,
            resolved_task_id=task_id,
            resolved_for="user",
            note="smoke verified resolution",
        )
        maintain = run_skill_maintenance(
            root,
            maintainer_persona="skill-curator",
            operator="skill-curator",
            lookback_days=30,
            min_usage=1,
            min_completeness=0.6,
            dry_run=True,
        )
        candidates = maintain.get("candidates", [])
        has_candidate = False
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, dict) and str(item.get("skill_id", "")) == skill_id:
                    has_candidate = True
                    break
        ok = bool(usage.get("counts_for_growth", False) and has_candidate)
        _append_check(
            checks,
            name="skill_growth_flow",
            ok=ok,
            detail=f"skill_id={skill_id} growth={usage.get('counts_for_growth', False)} candidate={has_candidate}",
            path=str(ingested.get("skill_path", "")),
        )
    except Exception as exc:  # noqa: BLE001
        _append_check(
            checks,
            name="skill_growth_flow",
            ok=False,
            detail=f"error: {exc}",
            path="00_System/Skills/_Persona/",
        )

    try:
        enqueue = queue_notion_publish(
            root,
            title=f"smoke notion {token}",
            body_md=f"- smoke_token: `{token}`\n",
            operator="smoke-suite",
            tags=["smoke"],
        )
        qp = Path(root / str(enqueue.get("relative_path", ""))).resolve()
        ledger = Path(root / NOTION_QUEUE_EVENTS_RELATIVE).resolve()
        ok = qp.is_file() and ledger.is_file() and enqueue.get("notion_queue_id", "") in qp.read_text(encoding="utf-8")
        _append_check(
            checks,
            name="notion_publish_queue_enqueue",
            ok=ok,
            detail=f"path={enqueue.get('relative_path', '')}",
            path=str(enqueue.get("relative_path", "")),
        )
    except Exception as exc:  # noqa: BLE001
        _append_check(
            checks,
            name="notion_publish_queue_enqueue",
            ok=False,
            detail=f"error: {exc}",
            path="11_AI_Mirror/external_ingest/notion_queue/",
        )

    if with_web:
        try:
            result = run_web_research(
                root,
                query="sqlite fts5 bm25",
                operator="smoke-suite",
                max_results=1,
                fetch_top=1,
                timeout_s=20.0,
            )
            note_path = str(result.get("note_path", ""))
            ok = bool(note_path)
            _append_check(
                checks,
                name="web_research_flow",
                ok=ok,
                detail=f"note_path={note_path}",
                required=False,
                path=note_path,
            )
        except Exception as exc:  # noqa: BLE001
            _append_check(
                checks,
                name="web_research_flow",
                ok=False,
                detail=f"error: {exc}",
                required=False,
                path="11_AI_Mirror/external_ingest/web_research/",
            )

    if with_llm:
        try:
            profile = runtime_profile_for_persona(adapter, "core")
            llm_runtime = MemoryRuntime(adapter, profile=profile)
            client = LLMClient(adapter.vault_root)
            payload = run_chat_turn(
                adapter=adapter,
                runtime=llm_runtime,
                client=client,
                persona="core",
                context="smoke",
                session=token,
                message=f"請只回覆 smoke-ok {token}",
                temperature=0.0,
                timeout_s=45.0,
                memory_mode="session_only",
                transport="smoke",
                channel_id="smoke",
                user_id="smoke-suite",
            )
            response = str(payload.get("response", "")).strip()
            route_event = payload.get("llm_route_event")
            ok = bool(response and isinstance(route_event, dict))
            _append_check(
                checks,
                name="llm_chat_flow",
                ok=ok,
                detail=f"response_preview={response[:80]} route_logged={isinstance(route_event, dict)}",
                required=False,
                path=str(payload.get("memory_paths", {}).get("session", "")),
            )
        except Exception as exc:  # noqa: BLE001
            _append_check(
                checks,
                name="llm_chat_flow",
                ok=False,
                detail=f"error: {exc}",
                required=False,
                path="70_Active_Plans/Session_Logs/",
            )

    overall_ok = all(bool(item.get("ok", False)) for item in checks if bool(item.get("required", True)))
    return {
        "vault_root": str(root),
        "token": token,
        "overall_ok": overall_ok,
        "checks": checks,
    }
