"""CLI entrypoint for Agent Memory Core."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from datetime import datetime
from typing import Any

from agent_memory.chat_session import append_chat_turn, sanitize_component
from agent_memory.brain_template import seed_brain_from_template
from agent_memory.channel_bindings import (
    bind_channel_persona,
    list_channel_bindings,
    set_default_persona,
    unbind_channel,
)
from agent_memory.config import (
    clear_user_vault_root,
    resolve_vault_root,
    resolve_vault_root_with_source,
    set_user_vault_root,
    user_config_path,
)
from agent_memory.folder_allocator import FolderAllocator
from agent_memory.hooks import write_event_logger
from agent_memory.notion_queue import list_notion_queue_items, queue_notion_publish
from agent_memory.llm_routing import (
    load_llm_router_config,
    resolve_llm_route,
    save_llm_router_config,
)
from agent_memory.context_compaction import compact_session_memory
from agent_memory.dialogue_modes import load_dialogue_modes, resolve_dialogue_mode
from agent_memory.llm_ledger import LLM_ROUTE_EVENTS_RELATIVE_PATH, list_llm_route_events
from agent_memory.memory_promotion import (
    PromotionThresholds,
    list_recall_entries,
    run_promotion_cycle,
)
from agent_memory.persona_factory import (
    approve_persona_proposal,
    create_persona_proposal,
    disable_persona,
    ensure_default_steward_persona,
    list_personas,
    update_persona_profile,
)
from agent_memory.persona_portability import export_persona_bundle, import_persona_bundle
from agent_memory.profile_scope import load_yaml_object, runtime_profile_for_persona
from agent_memory.retrieval_benchmark import (
    default_cases as benchmark_default_cases,
    default_variants as benchmark_default_variants,
    load_cases_from_yaml,
    run_benchmark,
)
from agent_memory.retrieval_routing import (
    RETRIEVAL_ROUTER_RELATIVE_PATH,
    load_retrieval_router_config,
    resolve_retrieval_route,
    save_retrieval_router_config,
)
from agent_memory.runtime import MemoryRuntime, RuntimeProfile
from agent_memory.skill_library import (
    evaluate_skill_completeness,
    ingest_skill_file,
    list_skills,
    merge_skills,
    promote_persona_skill,
    record_skill_usage,
    refresh_skill_index,
    run_skill_maintenance,
)
from agent_memory.task_board import (
    create_task,
    list_tasks,
    prune_finished_tasks,
    set_task_check,
    set_task_status,
)
from agent_memory.smoke_suite import run_smoke_suite
from agent_memory.transport_ingest import run_transport_event
from agent_memory.transport_profiles import load_transport_profiles, resolve_transport_profile
from agent_memory.tools.schemas import (
    memory_get_schema,
    memory_search_schema,
    memory_tool_schema,
    tool_schema_bundle,
)
from agent_memory.types import Frontmatter, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter
from agent_memory.web_research import ingest_web_url, run_web_research


def _frontmatter_dict(fm: Frontmatter) -> dict[str, Any]:
    return {
        "type": fm.type.value,
        "source": fm.source.value,
        "created": fm.created.isoformat(),
        "updated": fm.updated.isoformat(),
        "agent": fm.agent,
        "status": fm.status,
        "schema_version": fm.schema_version,
        "tags": fm.tags,
        "char_count": fm.char_count,
        "extras": fm.extras,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-cli", description="Agent Memory core utility commands.")
    parser.add_argument("--vault-root", default=None, help="Obsidian vault root path.")
    parser.add_argument(
        "--runtime-persona",
        default="core",
        help="Runtime persona id for route scope checks (non-chat commands).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize vault skeleton.")
    init.add_argument("--owner-id", default=None, help="Optional owner id for brain manifest.")
    init.add_argument("--brain-id", default=None, help="Optional explicit brain id for manifest.")
    init.add_argument("--force-manifest", action="store_true", help="Overwrite existing brain manifest.")
    init.set_defaults(func=_cmd_init)

    brain_shell = sub.add_parser("brain-shell", help="Build reusable 00~99 second-brain shell.")
    brain_shell.add_argument("--owner-id", default=None, help="Owner id for this brain instance.")
    brain_shell.add_argument("--brain-id", default=None, help="Optional explicit brain id.")
    brain_shell.add_argument("--force-manifest", action="store_true", help="Overwrite existing manifest.")
    brain_shell.add_argument(
        "--set-default",
        action="store_true",
        help="Persist this vault path into user config after scaffold.",
    )
    brain_shell.add_argument("--json", action="store_true", help="Print scaffold result as JSON.")
    brain_shell.set_defaults(func=_cmd_brain_shell)

    brain_show = sub.add_parser("brain-show", help="Show brain manifest and shell metadata.")
    brain_show.add_argument("--json", action="store_true", help="Print manifest payload as JSON.")
    brain_show.set_defaults(func=_cmd_brain_show)

    brain_seed = sub.add_parser("brain-seed-template", help="Seed personas/skills from a template vault.")
    brain_seed.add_argument("--template-vault", required=True, help="Template vault root path.")
    brain_seed.add_argument("--overwrite", action="store_true", help="Overwrite existing persona/skill files.")
    brain_seed.add_argument("--skip-personas", action="store_true", help="Do not import persona profiles/routes.")
    brain_seed.add_argument("--skip-persona-skills", action="store_true", help="Do not import persona-scoped skills.")
    brain_seed.add_argument("--include-shared-skills", action="store_true", help="Also import shared skills from template.")
    brain_seed.add_argument("--skip-dialogue-modes", action="store_true", help="Do not import dialogue_modes.yaml.")
    brain_seed.add_argument("--json", action="store_true", help="Print result as JSON.")
    brain_seed.set_defaults(func=_cmd_brain_seed_template)

    channel_bind = sub.add_parser("channel-bind", help="Bind one transport channel to persona.")
    channel_bind.add_argument("--transport", required=True, help="Transport name, e.g. discord/line/web.")
    channel_bind.add_argument("--channel-id", required=True, help="Channel/thread/user id.")
    channel_bind.add_argument("--persona", required=True, help="Persona id.")
    channel_bind.add_argument("--operator", default="user", help="Operator id.")
    channel_bind.add_argument("--json", action="store_true", help="Print result as JSON.")
    channel_bind.set_defaults(func=_cmd_channel_bind)

    channel_default = sub.add_parser("channel-default-persona", help="Set fallback persona when no channel binding exists.")
    channel_default.add_argument("--persona", required=True, help="Fallback persona id.")
    channel_default.add_argument("--operator", default="user", help="Operator id.")
    channel_default.add_argument("--json", action="store_true", help="Print result as JSON.")
    channel_default.set_defaults(func=_cmd_channel_default_persona)

    channel_unbind = sub.add_parser("channel-unbind", help="Remove one transport channel binding.")
    channel_unbind.add_argument("--transport", required=True, help="Transport name.")
    channel_unbind.add_argument("--channel-id", required=True, help="Channel/thread/user id.")
    channel_unbind.add_argument("--json", action="store_true", help="Print result as JSON.")
    channel_unbind.set_defaults(func=_cmd_channel_unbind)

    channel_list = sub.add_parser("channel-bindings", help="List channel persona bindings.")
    channel_list.add_argument("--json", action="store_true", help="Print result as JSON.")
    channel_list.set_defaults(func=_cmd_channel_bindings)

    transport_show = sub.add_parser("transport-show", help="Show transport profile config.")
    transport_show.add_argument("--transport", default=None, help="Optional transport name (line/discord/web).")
    transport_show.add_argument("--json", action="store_true", help="Print result as JSON.")
    transport_show.set_defaults(func=_cmd_transport_show)

    dialogue_mode_show = sub.add_parser("dialogue-mode-show", help="Show dialogue mode config or one resolved mode.")
    dialogue_mode_show.add_argument("--resolve", action="store_true", help="Resolve mode by persona/transport.")
    dialogue_mode_show.add_argument("--persona", default="core", help="Persona id when used with --resolve.")
    dialogue_mode_show.add_argument("--transport", default="web", help="Transport id when used with --resolve.")
    dialogue_mode_show.add_argument("--dialogue-mode", "--mode", dest="dialogue_mode", default="", help="Requested mode id.")
    dialogue_mode_show.add_argument("--json", action="store_true", help="Print result as JSON.")
    dialogue_mode_show.set_defaults(func=_cmd_dialogue_mode_show)

    transport_ingest = sub.add_parser("transport-ingest", help="Ingest one inbound transport payload.")
    transport_ingest.add_argument("--transport", required=True, help="Transport name, e.g. line/discord/web.")
    transport_ingest.add_argument("--payload-file", default=None, help="Path to JSON payload file.")
    transport_ingest.add_argument("--payload-json", default=None, help="Raw JSON payload string.")
    transport_ingest.add_argument("--persona", default=None, help="Optional persona override.")
    transport_ingest.add_argument("--override-profile", default=None, help="Request-level profile override.")
    transport_ingest.add_argument("--override-model", default=None, help="Request-level model override.")
    transport_ingest.add_argument(
        "--dialogue-mode",
        "--mode",
        dest="dialogue_mode",
        default=None,
        help="Optional dialogue mode id, e.g. standard/coach/strategist/executor.",
    )
    transport_ingest.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    transport_ingest.add_argument("--timeout", type=float, default=90.0, help="HTTP timeout seconds.")
    transport_ingest.add_argument(
        "--memory-mode",
        choices=["session_only", "session_and_daily"],
        default="session_and_daily",
        help="Where to persist this turn.",
    )
    transport_ingest.add_argument(
        "--allow-llm-degraded",
        action="store_true",
        help="Allow degraded response when all LLM routes fail.",
    )
    transport_ingest.add_argument(
        "--require-nondegraded",
        action="store_true",
        help="Exit non-zero when degraded=true (for mainline gate checks).",
    )
    transport_ingest.add_argument("--json", action="store_true", help="Print result as JSON.")
    transport_ingest.set_defaults(func=_cmd_transport_ingest)

    task_create = sub.add_parser("task-create", help="Create one collaborative task.")
    task_create.add_argument("--title", required=True, help="Task title.")
    task_create.add_argument("--supervisor", required=True, help="Supervisor persona id, e.g. manager.")
    task_create.add_argument("--assignee", action="append", default=None, help="Repeatable assignee persona id.")
    task_create.add_argument("--watcher", action="append", default=None, help="Repeatable watcher persona id.")
    task_create.add_argument("--check", action="append", default=None, help="Repeatable checklist item.")
    task_create.add_argument("--detail", default="", help="Task detail.")
    task_create.add_argument("--operator", default="user", help="Operator id.")
    task_create.add_argument(
        "--no-auto-cleanup",
        action="store_true",
        help="Keep done/cancelled task in active board.",
    )
    task_create.add_argument("--json", action="store_true", help="Print result as JSON.")
    task_create.set_defaults(func=_cmd_task_create)

    task_list = sub.add_parser("task-list", help="List collaborative tasks.")
    task_list.add_argument(
        "--status",
        default=None,
        choices=["todo", "in_progress", "blocked", "done", "cancelled"],
        help="Optional status filter.",
    )
    task_list.add_argument("--assignee", default=None, help="Filter by assignee.")
    task_list.add_argument("--supervisor", default=None, help="Filter by supervisor.")
    task_list.add_argument("--json", action="store_true", help="Print result as JSON.")
    task_list.set_defaults(func=_cmd_task_list)

    task_update = sub.add_parser("task-update", help="Update task status.")
    task_update.add_argument("--task-id", required=True, help="Task id.")
    task_update.add_argument(
        "--status",
        required=True,
        choices=["todo", "in_progress", "blocked", "done", "cancelled"],
        help="Next status.",
    )
    task_update.add_argument("--note", default="", help="Optional update note.")
    task_update.add_argument("--operator", default="agent", help="Operator id.")
    task_update.add_argument("--json", action="store_true", help="Print result as JSON.")
    task_update.set_defaults(func=_cmd_task_update)

    task_check = sub.add_parser("task-check", help="Toggle one checklist item.")
    task_check.add_argument("--task-id", required=True, help="Task id.")
    task_check.add_argument("--index", required=True, type=int, help="Checklist index (1-based).")
    task_check.add_argument("--done", action="store_true", help="Mark checked.")
    task_check.add_argument("--undone", action="store_true", help="Mark unchecked.")
    task_check.add_argument("--operator", default="agent", help="Operator id.")
    task_check.add_argument("--json", action="store_true", help="Print result as JSON.")
    task_check.set_defaults(func=_cmd_task_check)

    task_prune = sub.add_parser("task-prune", help="Prune done/cancelled tasks from active board.")
    task_prune.add_argument("--operator", default="agent", help="Operator id.")
    task_prune.add_argument("--json", action="store_true", help="Print result as JSON.")
    task_prune.set_defaults(func=_cmd_task_prune)

    skill_ingest = sub.add_parser("skill-ingest", help="Normalize one manual skill file into skill library.")
    skill_ingest.add_argument("--source-path", required=True, help="Path of manual skill source file.")
    skill_ingest.add_argument("--skill-id", default=None, help="Optional target skill id.")
    skill_ingest.add_argument("--owner-persona", default="core", help="Owner persona id.")
    skill_ingest.add_argument(
        "--scope",
        default="shared",
        choices=["shared", "persona"],
        help="Target scope for this skill.",
    )
    skill_ingest.add_argument("--persona", default=None, help="Target persona id when scope=persona.")
    skill_ingest.add_argument("--operator", default="user", help="Operator id.")
    skill_ingest.add_argument("--overwrite", action="store_true", help="Overwrite existing skill.")
    skill_ingest.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_ingest.set_defaults(func=_cmd_skill_ingest)

    skill_list = sub.add_parser("skill-list", help="List skills in skill library.")
    skill_list.add_argument(
        "--scope",
        default="all",
        choices=["all", "shared", "persona"],
        help="Skill scope filter.",
    )
    skill_list.add_argument("--persona", default=None, help="Persona filter for persona skills.")
    skill_list.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_list.set_defaults(func=_cmd_skill_list)

    skill_merge = sub.add_parser("skill-merge", help="Merge multiple skills into one.")
    skill_merge.add_argument("--target", required=True, help="Target skill id.")
    skill_merge.add_argument("--source", action="append", required=True, help="Repeatable source skill id.")
    skill_merge.add_argument("--operator", default="agent", help="Operator id.")
    skill_merge.add_argument(
        "--no-archive-sources",
        action="store_true",
        help="Do not archive source skills after merge.",
    )
    skill_merge.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_merge.set_defaults(func=_cmd_skill_merge)

    skill_index = sub.add_parser("skill-index", help="Refresh skill index markdown.")
    skill_index.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_index.set_defaults(func=_cmd_skill_index)

    skill_eval = sub.add_parser("skill-eval", help="Evaluate skill completeness score.")
    skill_eval.add_argument("--skill-id", required=True, help="Skill id.")
    skill_eval.add_argument(
        "--scope",
        default="shared",
        choices=["shared", "persona"],
        help="Skill scope.",
    )
    skill_eval.add_argument("--persona", default=None, help="Persona id when scope=persona.")
    skill_eval.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_eval.set_defaults(func=_cmd_skill_eval)

    skill_promote = sub.add_parser("skill-promote", help="Promote one persona skill into shared skill library.")
    skill_promote.add_argument("--persona", required=True, help="Source persona id.")
    skill_promote.add_argument("--skill-id", required=True, help="Skill id.")
    skill_promote.add_argument("--operator", default="agent", help="Operator id.")
    skill_promote.add_argument("--min-score", type=float, default=0.75, help="Minimum completeness score.")
    skill_promote.add_argument("--force", action="store_true", help="Force promotion even below min-score.")
    skill_promote.add_argument("--archive-source", action="store_true", help="Archive source persona skill.")
    skill_promote.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_promote.set_defaults(func=_cmd_skill_promote)

    skill_use = sub.add_parser("skill-use", help="Record one skill usage event.")
    skill_use.add_argument("--persona", required=True, help="Persona id.")
    skill_use.add_argument("--skill-id", required=True, help="Skill id.")
    skill_use.add_argument(
        "--scope",
        default="auto",
        choices=["auto", "persona", "shared"],
        help="Scope hint for skill resolution.",
    )
    skill_use.add_argument("--operator", default="agent", help="Operator id.")
    skill_use.add_argument("--note", default="", help="Usage note.")
    skill_use.add_argument("--success", action="store_true", help="Mark success=true.")
    skill_use.add_argument("--fail", action="store_true", help="Mark success=false.")
    skill_use.add_argument("--resolved-task-id", default="", help="Completed task id as resolution evidence.")
    skill_use.add_argument(
        "--resolved-for",
        default="user",
        choices=["user", "persona"],
        help="Who was actually solved by this skill use.",
    )
    skill_use.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_use.set_defaults(func=_cmd_skill_use)

    skill_maintain = sub.add_parser("skill-maintain", help="Run periodic skill library maintenance.")
    skill_maintain.add_argument("--maintainer-persona", default="skill-curator", help="Maintenance persona id.")
    skill_maintain.add_argument("--operator", default="skill-curator", help="Operator id.")
    skill_maintain.add_argument("--lookback-days", type=int, default=30, help="Usage lookback days.")
    skill_maintain.add_argument("--min-usage", type=int, default=5, help="Promotion min usage threshold.")
    skill_maintain.add_argument(
        "--min-completeness",
        type=float,
        default=0.75,
        help="Promotion minimum completeness score.",
    )
    skill_maintain.add_argument("--dry-run", action="store_true", help="Do not actually promote, report only.")
    skill_maintain.add_argument("--json", action="store_true", help="Print result as JSON.")
    skill_maintain.set_defaults(func=_cmd_skill_maintain)

    web_research = sub.add_parser("web-research", help="Search web and ingest results into mirror memory.")
    web_research.add_argument("--query", required=True, help="Web research query.")
    web_research.add_argument("--operator", default="researcher", help="Operator id.")
    web_research.add_argument("--max-results", type=int, default=5, help="Search result count.")
    web_research.add_argument("--fetch-top", type=int, default=3, help="How many hits to fetch.")
    web_research.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    web_research.add_argument("--json", action="store_true", help="Print result as JSON.")
    web_research.set_defaults(func=_cmd_web_research)

    web_ingest = sub.add_parser("web-ingest-url", help="Ingest one URL into mirror memory.")
    web_ingest.add_argument("--url", required=True, help="URL to ingest.")
    web_ingest.add_argument("--title", default="", help="Optional title.")
    web_ingest.add_argument("--operator", default="researcher", help="Operator id.")
    web_ingest.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds.")
    web_ingest.add_argument("--json", action="store_true", help="Print result as JSON.")
    web_ingest.set_defaults(func=_cmd_web_ingest_url)

    smoke = sub.add_parser("smoke-test", help="Run integration smoke suite.")
    smoke.add_argument("--with-web", action="store_true", help="Include web research smoke check.")
    smoke.add_argument("--with-llm", action="store_true", help="Include LLM chat smoke check.")
    smoke.add_argument("--json", action="store_true", help="Print result as JSON.")
    smoke.set_defaults(func=_cmd_smoke_test)

    compact = sub.add_parser("compact-session", help="Run pre-flush + compaction for one session log.")
    compact.add_argument("--persona", default="core", help="Persona id for session path.")
    compact.add_argument("--context", default="cli", help="Context id for session path.")
    compact.add_argument("--session", default="default", help="Session id for session path.")
    compact.add_argument("--date", default=None, help="Optional date folder (YYYY-MM-DD).")
    compact.add_argument("--max-chars", type=int, default=12000, help="Compaction trigger body char threshold.")
    compact.add_argument("--keep-recent-turns", type=int, default=6, help="How many recent turns to keep.")
    compact.add_argument("--use-llm-summary", action="store_true", help="Use routed LLM for summary generation.")
    compact.add_argument("--llm-persona", default="core", help="Persona id for summary LLM route resolution.")
    compact.add_argument("--override-profile", default=None, help="Optional summary model profile override.")
    compact.add_argument("--override-model", default=None, help="Optional summary model override.")
    compact.add_argument("--timeout", type=float, default=45.0, help="Summary LLM timeout seconds.")
    compact.add_argument("--json", action="store_true", help="Print result as JSON.")
    compact.set_defaults(func=_cmd_compact_session)

    recall_show = sub.add_parser("recall-show", help="Show short-term recall tracker entries.")
    recall_show.add_argument("--limit", type=int, default=30, help="Maximum entries to return.")
    recall_show.add_argument("--promoted-only", action="store_true", help="Only show promoted entries.")
    recall_show.add_argument("--json", action="store_true", help="Print result as JSON.")
    recall_show.set_defaults(func=_cmd_recall_show)

    promote = sub.add_parser("promote-cycle", help="Run short-term to long-term promotion cycle.")
    promote.add_argument("--phase", choices=["light", "rem"], default="light", help="Dreaming phase mode.")
    promote.add_argument("--min-score", type=float, default=0.75, help="Promotion score threshold.")
    promote.add_argument("--min-recall", type=int, default=3, help="Minimum recall count.")
    promote.add_argument("--min-queries", type=int, default=2, help="Minimum unique query count.")
    promote.add_argument("--min-days", type=int, default=2, help="Minimum unique recall days.")
    promote.add_argument("--grace-hours", type=float, default=24.0, help="Minimum age hours before promotion.")
    promote.add_argument("--max-promotions", type=int, default=20, help="Maximum promotions in one run.")
    promote.add_argument("--operator", default="promotion-cycle", help="Operator id.")
    promote.add_argument("--dry-run", action="store_true", help="Only evaluate candidates, do not write.")
    promote.add_argument("--json", action="store_true", help="Print result as JSON.")
    promote.set_defaults(func=_cmd_promote_cycle)

    e2e = sub.add_parser("core-e2e", help="Run write-search-flush-promotion end-to-end cycle.")
    e2e.add_argument("--persona", default="core", help="Persona id for session compaction path.")
    e2e.add_argument("--context", default="e2e", help="Context id for e2e session.")
    e2e.add_argument("--session", default="", help="Optional explicit session id.")
    e2e.add_argument("--json", action="store_true", help="Print result as JSON.")
    e2e.set_defaults(func=_cmd_core_e2e)

    # V2 Phase A C13: wikilinks_graph inspection CLI
    wikilinks = sub.add_parser("wikilinks-graph", help="Wikilinks graph (GraphRAG) — rebuild / status / neighbors.")
    wikilinks.add_argument("--action", choices=["rebuild", "status", "neighbors"], default="status", help="rebuild = scan vault and save; status = show stats; neighbors = show 1-hop neighbors of a path.")
    wikilinks.add_argument("--path", default="", help="Required for --action neighbors. Vault-relative path.")
    wikilinks.add_argument("--max-hops", type=int, default=1, help="For --action neighbors. Default 1.")
    wikilinks.add_argument("--json", action="store_true", help="Print result as JSON.")
    wikilinks.set_defaults(func=_cmd_wikilinks_graph)

    # R7 C22: curator status / force-run / skill-suggestions inspection CLI
    curator_status = sub.add_parser("curator-status", help="R7 curator — show state + should_run_now diagnosis.")
    curator_status.add_argument("--json", action="store_true", help="Print as JSON.")
    curator_status.set_defaults(func=_cmd_curator_status)

    curator_force = sub.add_parser("curator-force-run", help="R7 curator — force-run skipping should_run_now check.")
    curator_force.add_argument("--mode", choices=["daily", "weekly"], default="daily", help="daily light or weekly deep.")
    curator_force.add_argument("--json", action="store_true", help="Print as JSON.")
    curator_force.set_defaults(func=_cmd_curator_force_run)

    skill_sugg = sub.add_parser("skill-suggestions-list", help="R7 C20b — list pending skill upgrade suggestions.")
    skill_sugg.add_argument("--include-resolved", action="store_true", help="Include dismissed/promoted entries.")
    skill_sugg.add_argument("--json", action="store_true", help="Print as JSON.")
    skill_sugg.set_defaults(func=_cmd_skill_suggestions_list)

    midterm_list = sub.add_parser("midterm-list", help="R7 — list 10_Permanent/Mid_Term entries (entity 累積狀態).")
    midterm_list.add_argument("--min-mention", type=int, default=0, help="Filter mention_count >= N.")
    midterm_list.add_argument("--all", action="store_true", help="Include promoted/stale/archived (default only mid).")
    midterm_list.add_argument("--json", action="store_true", help="Print as JSON.")
    midterm_list.set_defaults(func=_cmd_midterm_list)

    # R9 C29: on-demand topic reflection
    reflect = sub.add_parser("reflect", help="R9 — 主動整理 X 主題, 產 Concepts/reflection_<topic>_<date>.md.")
    reflect.add_argument("--topic", required=True, help="Topic 字串 (例: Python / Round 7 / Discord).")
    reflect.add_argument("--max-match", type=int, default=30, help="Scan 最多幾個 .md (預設 30).")
    reflect.add_argument("--json", action="store_true", help="Print as JSON.")
    reflect.set_defaults(func=_cmd_reflect)

    persona_create = sub.add_parser("persona-create", help="Create one persona proposal.")
    persona_create.add_argument("--display-name", required=True, help="Persona display name.")
    persona_create.add_argument("--persona-id", default=None, help="Optional explicit persona id.")
    persona_create.add_argument("--mission", default="", help="Persona mission statement.")
    persona_create.add_argument("--style", default="concise", help="Speaking style.")
    persona_create.add_argument("--language", default="zh-Hant", help="Persona language.")
    persona_create.add_argument(
        "--role-type",
        default=None,
        choices=["tooling", "chat"],
        help="Persona type: tooling(可寫程式) or chat(純聊天).",
    )
    persona_create.add_argument("--default-mode", default=None, help="Default route mode (optional).")
    persona_create.add_argument("--include", action="append", default=None, help="Repeatable memory include scope.")
    persona_create.add_argument("--exclude", action="append", default=None, help="Repeatable memory exclude scope.")
    persona_create.add_argument("--allow", action="append", default=None, help="Repeatable write allow scope.")
    persona_create.add_argument("--deny", action="append", default=None, help="Repeatable write deny scope.")
    persona_create.add_argument("--enable-tools", action="store_true", help="Enable tool/code-write capability for this persona.")
    persona_create.add_argument("--disable-tools", action="store_true", help="Disable tool/code-write capability for this persona.")
    persona_create.add_argument("--allow-experimental-role", action="store_true", help=argparse.SUPPRESS)
    persona_create.add_argument("--operator", default="user", help="Who creates this proposal.")
    persona_create.add_argument("--auto-approve", action="store_true", help="Create and approve immediately.")
    persona_create.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_create.set_defaults(func=_cmd_persona_create)

    persona_approve = sub.add_parser("persona-approve", help="Approve one persona proposal.")
    persona_approve.add_argument("--proposal-id", required=True, help="Proposal id, e.g. pf-xxxx.")
    persona_approve.add_argument("--operator", default="user", help="Approver id.")
    persona_approve.add_argument("--overwrite", action="store_true", help="Overwrite existing persona files.")
    persona_approve.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_approve.set_defaults(func=_cmd_persona_approve)

    persona_disable = sub.add_parser("persona-disable", help="Disable one persona and keep rollback trail.")
    persona_disable.add_argument("--persona", required=True, help="Persona id.")
    persona_disable.add_argument("--operator", default="user", help="Operator id.")
    persona_disable.add_argument("--reason", default="", help="Disable reason.")
    persona_disable.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_disable.set_defaults(func=_cmd_persona_disable)

    persona_update = sub.add_parser("persona-update", help="Update one active persona profile/route basics.")
    persona_update.add_argument("--persona", required=True, help="Persona id.")
    persona_update.add_argument("--display-name", default=None, help="New display name.")
    persona_update.add_argument("--mission", default=None, help="New mission statement.")
    persona_update.add_argument("--style", default=None, help="New style.")
    persona_update.add_argument("--language", default=None, help="New language.")
    persona_update.add_argument(
        "--role-type",
        default=None,
        choices=["tooling", "chat"],
        help="Persona type: tooling(可寫程式) or chat(純聊天).",
    )
    persona_update.add_argument("--default-mode", default=None, help="New default dialogue mode.")
    persona_update.add_argument("--enable-tools", action="store_true", help="Enable tool/code-write capability for this persona.")
    persona_update.add_argument("--disable-tools", action="store_true", help="Disable tool/code-write capability for this persona.")
    persona_update.add_argument("--allow-experimental-role", action="store_true", help=argparse.SUPPRESS)
    persona_update.add_argument("--operator", default="user", help="Operator id.")
    persona_update.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_update.set_defaults(func=_cmd_persona_update)

    persona_list = sub.add_parser("persona-list", help="List personas from runtime registry.")
    persona_list.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_list.set_defaults(func=_cmd_persona_list)

    persona_pack = sub.add_parser("persona-pack", help="Pack one persona with its route/skills/session memories.")
    persona_pack.add_argument("--persona", required=True, help="Persona id to pack.")
    persona_pack.add_argument("--output-dir", default=None, help="Optional output directory for zip bundle.")
    persona_pack.add_argument("--skip-sessions", action="store_true", help="Do not include session logs.")
    persona_pack.add_argument("--skip-persona-skills", action="store_true", help="Do not include persona-scoped skills.")
    persona_pack.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_pack.set_defaults(func=_cmd_persona_pack)

    persona_unpack = sub.add_parser("persona-unpack", help="Unpack one persona bundle into current vault.")
    persona_unpack.add_argument("--package", required=True, help="Path to persona bundle zip.")
    persona_unpack.add_argument("--overwrite", action="store_true", help="Overwrite existing files in target vault.")
    persona_unpack.add_argument("--keep-status", action="store_true", help="Keep imported status (do not force active).")
    persona_unpack.add_argument("--json", action="store_true", help="Print result as JSON.")
    persona_unpack.set_defaults(func=_cmd_persona_unpack)

    vault_show = sub.add_parser("vault-show", help="Show resolved vault root and source.")
    vault_show.set_defaults(func=_cmd_vault_show)

    vault_set = sub.add_parser("vault-set", help="Persist default vault root to user config.")
    vault_set.add_argument("path", help="Obsidian vault root path.")
    vault_set.set_defaults(func=_cmd_vault_set)

    vault_clear = sub.add_parser("vault-clear", help="Remove persisted default vault root config.")
    vault_clear.set_defaults(func=_cmd_vault_clear)

    add = sub.add_parser("add", help="Legacy shortcut for add operation.")
    add.add_argument("target", choices=["user", "memory", "concept", "skill", "daily", "path"])
    add.add_argument("content", help="Markdown content to add.")
    add.add_argument("--key", default=None, help="Target key/path. Needed for concept/skill/path.")
    add.add_argument("--agent", default="agent-memory-core", help="Writer id.")
    add.set_defaults(func=_cmd_add)

    get = sub.add_parser("get", help="Read note by vault-relative path.")
    get.add_argument("path", help="Vault-relative path (e.g. 10_Permanent/MEMORY.md).")
    get.add_argument("--lines", default=None, help="Optional range, e.g. 1-20 or 3:8.")
    get.add_argument("--body-only", action="store_true", help="Only print markdown body.")
    get.set_defaults(func=_cmd_get)

    memory = sub.add_parser("memory", help="Unified memory(add/replace/remove/get) command.")
    memory.add_argument("action", choices=["add", "replace", "remove", "get"])
    memory.add_argument("--path", required=True, help="Vault-relative path.")
    memory.add_argument("--content", default=None, help="Content for add/replace actions.")
    memory.add_argument("--reason", default="", help="Reason for replace/remove.")
    memory.add_argument("--agent", default="agent-memory-core", help="Writer identity.")
    memory.add_argument("--source", default="agent", choices=["user", "agent", "flush", "mirror", "promotion"])
    memory.add_argument("--tag", action="append", default=None, help="Repeatable frontmatter tag.")
    memory.add_argument("--extra", action="append", default=None, help="Repeatable extra key=value.")
    memory.add_argument("--body-only", action="store_true", help="Only print body when action=get.")
    memory.add_argument("--lines", default=None, help="Optional range for get, e.g. 1-20.")
    memory.set_defaults(func=_cmd_memory)

    snapshot = sub.add_parser("snapshot", help="Build frozen USER + MEMORY snapshot block.")
    snapshot.add_argument("--user-path", default="10_Permanent/Profiles/USER.md")
    snapshot.add_argument("--memory-path", default="10_Permanent/MEMORY.md")
    snapshot.set_defaults(func=_cmd_snapshot)

    reindex = sub.add_parser("reindex", help="Run incremental sqlite/FTS reindex.")
    reindex.add_argument("--include", action="append", default=None, help="Repeatable include path prefix.")
    reindex.add_argument("--exclude", action="append", default=None, help="Repeatable exclude path prefix.")
    reindex.add_argument("--no-sync-views", action="store_true", help="Skip updating user-facing index markdown.")
    reindex.add_argument("--json", action="store_true", help="Print result as JSON.")
    reindex.set_defaults(func=_cmd_reindex)

    search = sub.add_parser("search", help="Search indexed memory notes.")
    search.add_argument("query", help="Search query text.")
    search.add_argument("--limit", type=int, default=10, help="Maximum hits.")
    search.add_argument("--include-archived", action="store_true", help="Include archived notes.")
    search.add_argument(
        "--no-auto-reindex",
        action="store_true",
        help="Do not run incremental reindex before searching.",
    )
    search.add_argument(
        "--strategy",
        choices=["hybrid", "fts", "vector"],
        default="",
        help="Retrieval strategy override (default: follow retrieval_router.yaml).",
    )
    search.add_argument("--mmr", action="store_true", help="Force enable MMR rerank for this query.")
    search.add_argument("--no-mmr", action="store_true", help="Force disable MMR rerank for this query.")
    search.add_argument("--mmr-lambda", type=float, default=None, help="MMR lambda (0~1).")
    search.add_argument("--json", action="store_true", help="Print results as JSON.")
    search.set_defaults(func=_cmd_search)

    sync_views = sub.add_parser("sync-views", help="Generate user-facing Obsidian index markdown files.")
    sync_views.add_argument("--output-dir", default="00_System/09_Index", help="Output directory in vault.")
    sync_views.add_argument("--json", action="store_true", help="Print result as JSON.")
    sync_views.set_defaults(func=_cmd_sync_views)

    alloc = sub.add_parser("allocate-folder", help="Allocate numbered folder and append ledger.")
    alloc.add_argument("--parent", default="", help="Parent folder relative path, empty means vault root.")
    alloc.add_argument("--english-slug", required=True, help="ASCII English slug for folder naming.")
    alloc.add_argument("--zh-purpose", required=True, help="Traditional Chinese purpose label.")
    alloc.add_argument("--source-path", default="unknown", help="Source note path triggering allocation.")
    alloc.add_argument("--source-hash", default=None, help="sha256 hash, auto-computed if omitted.")
    alloc.add_argument("--topic-label", default=None, help="Topic label for ledger.")
    alloc.add_argument("--base-index", type=int, default=None, help="Start index for allocation.")
    alloc.add_argument("--reason", default="", help="Decision reason in ledger.")
    alloc.add_argument("--operator", default="agent", choices=["agent", "user"])
    alloc.add_argument("--override-by-user", action="store_true")
    alloc.add_argument("--dry-run", action="store_true")
    alloc.set_defaults(func=_cmd_allocate_folder)

    llm_show = sub.add_parser("llm-show", help="Show effective LLM routing for one persona/context.")
    llm_show.add_argument("--persona", default="core", help="Persona id to resolve.")
    llm_show.add_argument("--override-profile", default=None, help="Request-level profile override.")
    llm_show.add_argument("--override-model", default=None, help="Request-level model override.")
    llm_show.add_argument("--json", action="store_true", help="Print routing result as JSON.")
    llm_show.set_defaults(func=_cmd_llm_show)

    llm_set_default = sub.add_parser("llm-set-default", help="Set global default model profile.")
    llm_set_default.add_argument("--profile", required=True, help="Provider profile id in llm_router.yaml.")
    llm_set_default.add_argument("--model", required=True, help="Model id for the default profile.")
    llm_set_default.add_argument("--json", action="store_true", help="Print updated values as JSON.")
    llm_set_default.set_defaults(func=_cmd_llm_set_default)

    llm_set_persona = sub.add_parser("llm-set-persona", help="Set persona-specific model override.")
    llm_set_persona.add_argument("--persona", required=True, help="Persona id.")
    llm_set_persona.add_argument("--profile", required=True, help="Provider profile id.")
    llm_set_persona.add_argument("--model", required=True, help="Model id.")
    llm_set_persona.add_argument("--json", action="store_true", help="Print updated values as JSON.")
    llm_set_persona.set_defaults(func=_cmd_llm_set_persona)

    llm_clear_persona = sub.add_parser("llm-clear-persona", help="Remove persona-specific model override.")
    llm_clear_persona.add_argument("--persona", required=True, help="Persona id.")
    llm_clear_persona.add_argument("--json", action="store_true", help="Print updated values as JSON.")
    llm_clear_persona.set_defaults(func=_cmd_llm_clear_persona)

    retrieval_show = sub.add_parser("retrieval-show", help="Show retrieval routing (embedding + MMR) settings.")
    retrieval_show.add_argument("--persona", default="", help="Optional persona id for override resolution.")
    retrieval_show.add_argument("--json", action="store_true", help="Print result as JSON.")
    retrieval_show.set_defaults(func=_cmd_retrieval_show)

    retrieval_set_embedding = sub.add_parser(
        "retrieval-set-embedding",
        help="Set retrieval embedding backend mode/profile/model.",
    )
    retrieval_set_embedding.add_argument("--mode", choices=["hash", "provider"], required=True, help="Embedding mode.")
    retrieval_set_embedding.add_argument("--profile", default="", help="Provider profile id (mode=provider).")
    retrieval_set_embedding.add_argument("--model", default="", help="Embedding model id (mode=provider).")
    retrieval_set_embedding.add_argument("--timeout", type=float, default=20.0, help="Embedding timeout seconds.")
    retrieval_set_embedding.add_argument("--json", action="store_true", help="Print result as JSON.")
    retrieval_set_embedding.set_defaults(func=_cmd_retrieval_set_embedding)

    retrieval_set_search = sub.add_parser(
        "retrieval-set-search",
        help="Set retrieval default strategy and MMR policy.",
    )
    retrieval_set_search.add_argument(
        "--default-strategy",
        choices=["hybrid", "fts", "vector"],
        default="hybrid",
        help="Default retrieval strategy.",
    )
    retrieval_set_search.add_argument("--mmr-enabled", action="store_true", help="Enable MMR rerank by default.")
    retrieval_set_search.add_argument("--mmr-disabled", action="store_true", help="Disable MMR rerank by default.")
    retrieval_set_search.add_argument("--mmr-lambda", type=float, default=0.7, help="MMR lambda (0~1).")
    retrieval_set_search.add_argument("--candidate-multiplier", type=int, default=4, help="Candidate pool multiplier.")
    retrieval_set_search.add_argument("--json", action="store_true", help="Print result as JSON.")
    retrieval_set_search.set_defaults(func=_cmd_retrieval_set_search)

    retrieval_benchmark = sub.add_parser(
        "retrieval-benchmark",
        help="Run retrieval quality benchmark across strategy/MMR variants.",
    )
    retrieval_benchmark.add_argument(
        "--cases-file",
        default="",
        help="Optional YAML file path with benchmark cases.",
    )
    retrieval_benchmark.add_argument("--limit", type=int, default=8, help="Max hits per query.")
    retrieval_benchmark.add_argument("--include-archived", action="store_true", help="Include archived notes.")
    retrieval_benchmark.add_argument(
        "--auto-reindex-each-query",
        action="store_true",
        help="Run incremental reindex before each query (slower, more stable).",
    )
    retrieval_benchmark.add_argument("--json", action="store_true", help="Print full benchmark payload as JSON.")
    retrieval_benchmark.set_defaults(func=_cmd_retrieval_benchmark)

    llm_log = sub.add_parser("llm-log", help="Show provider/model route ledger entries.")
    llm_log.add_argument("--limit", type=int, default=20, help="Maximum events to return.")
    llm_log.add_argument("--persona", default="", help="Filter by persona id.")
    llm_log.add_argument("--session", default="", help="Filter by session id.")
    llm_log.add_argument("--transport", default="", help="Filter by transport.")
    llm_log.add_argument("--json", action="store_true", help="Print ledger events as JSON.")
    llm_log.set_defaults(func=_cmd_llm_log)

    chat = sub.add_parser("chat", help="Run one chat turn via routed LLM and write session memory.")
    chat.add_argument("message", help="User message for this turn.")
    chat.add_argument("--persona", default=None, help="Persona id. If omitted, can resolve by channel binding.")
    chat.add_argument("--transport", default="cli", help="Transport name, e.g. cli/discord/line/web.")
    chat.add_argument("--channel-id", default="", help="Channel or thread id for binding lookup.")
    chat.add_argument("--use-binding", action="store_true", help="Resolve persona from channel binding first.")
    chat.add_argument("--context", default="cli", help="Conversation context id.")
    chat.add_argument("--session", default="default", help="Session id.")
    chat.add_argument(
        "--dialogue-mode",
        "--mode",
        dest="dialogue_mode",
        default=None,
        help="Optional dialogue mode id, e.g. standard/coach/strategist/executor.",
    )
    chat.add_argument("--override-profile", default=None, help="Request-level profile override.")
    chat.add_argument("--override-model", default=None, help="Request-level model override.")
    chat.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    chat.add_argument("--timeout", type=float, default=90.0, help="HTTP timeout seconds.")
    chat.add_argument(
        "--memory-mode",
        choices=["session_only", "session_and_daily"],
        default="session_and_daily",
        help="Where to persist this turn.",
    )
    chat.add_argument(
        "--allow-llm-degraded",
        action="store_true",
        help="Allow degraded response when all LLM routes fail.",
    )
    chat.add_argument(
        "--require-nondegraded",
        action="store_true",
        help="Exit non-zero when degraded=true (for mainline gate checks).",
    )
    chat.add_argument("--json", action="store_true", help="Print result as JSON.")
    chat.set_defaults(func=_cmd_chat)

    serve_dashboard = sub.add_parser("serve-dashboard", help="Run local dashboard/API for multi-transport control.")
    serve_dashboard.add_argument("--host", default="127.0.0.1", help="Bind host.")
    serve_dashboard.add_argument("--port", type=int, default=8765, help="Bind port.")
    serve_dashboard.set_defaults(func=_cmd_serve_dashboard)

    serve_bridge = sub.add_parser(
        "serve-transport-bridge",
        help="Minimal POST-only server: /webhook/{transport} -> run_transport_event (no HTML dashboard).",
    )
    serve_bridge.add_argument("--host", default="127.0.0.1", help="Bind host, e.g. 0.0.0.0 for LAN access.")
    serve_bridge.add_argument("--port", type=int, default=16000, help="Bind port.")
    serve_bridge.set_defaults(func=_cmd_serve_transport_bridge)

    notion_queue = sub.add_parser(
        "notion-queue",
        help="Queue one pending Notion publish request.",
    )
    notion_queue.add_argument("--title", required=True, help="Request title.")
    notion_queue.add_argument("--body", default="", help="Markdown body (or use --body-file).")
    notion_queue.add_argument("--body-file", default=None, help="Path to markdown body file.")
    notion_queue.add_argument("--operator", default="user", help="Operator id.")
    notion_queue.add_argument("--persona-hint", default="", help="Optional persona hint.")
    notion_queue.add_argument("--target-hint", default="", help="Optional target page/database hint.")
    notion_queue.add_argument("--tag", action="append", default=None, help="Repeatable tag.")
    notion_queue.add_argument(
        "--priority",
        default="normal",
        choices=["low", "normal", "high"],
        help="Priority level.",
    )
    notion_queue.add_argument("--json", action="store_true", help="Print result as JSON.")
    notion_queue.set_defaults(func=_cmd_notion_queue)

    notion_queue_list = sub.add_parser("notion-queue-list", help="List queued Notion publish requests.")
    notion_queue_list.add_argument(
        "--status",
        default=None,
        help="Optional frontmatter status filter, e.g. pending.",
    )
    notion_queue_list.add_argument("--limit", type=int, default=50, help="Max rows to return.")
    notion_queue_list.add_argument("--json", action="store_true", help="Print result as JSON.")
    notion_queue_list.set_defaults(func=_cmd_notion_queue_list)

    schema = sub.add_parser("tool-schema", help="Print memory tool schema JSON.")
    schema.add_argument(
        "--name",
        default="all",
        choices=["all", "memory", "memory_search", "memory_get"],
        help="Which schema to print.",
    )
    schema.set_defaults(func=_cmd_tool_schema)

    return parser


def _build_adapter(args: argparse.Namespace) -> ObsidianVaultAdapter:
    root = resolve_vault_root(args.vault_root)
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    return adapter


def _build_runtime(
    args: argparse.Namespace,
    *,
    persona_id: str | None = None,
) -> tuple[ObsidianVaultAdapter, MemoryRuntime]:
    adapter = _build_adapter(args)
    selected_persona = persona_id if persona_id is not None else str(getattr(args, "runtime_persona", "")).strip()
    profile = runtime_profile_for_persona(adapter, selected_persona) if selected_persona else RuntimeProfile()
    runtime = MemoryRuntime(adapter, profile=profile)
    runtime.register_write_hook(write_event_logger(adapter.vault_root))
    return adapter, runtime


def _resolve_add_target(adapter: ObsidianVaultAdapter, target: str, key: str | None) -> tuple[str, MemoryType, MemorySource]:
    if target == "user":
        return adapter.resolve_path(MemoryType.USER_PROFILE, key or "USER"), MemoryType.USER_PROFILE, MemorySource.USER
    if target == "memory":
        return adapter.resolve_path(MemoryType.LONG_TERM, key or "MEMORY"), MemoryType.LONG_TERM, MemorySource.AGENT
    if target == "concept":
        if not key:
            raise ValueError("target=concept ?閬?--key")
        return adapter.resolve_path(MemoryType.CONCEPT, key), MemoryType.CONCEPT, MemorySource.AGENT
    if target == "skill":
        if not key:
            raise ValueError("target=skill ?閬?--key")
        return adapter.resolve_path(MemoryType.SKILL, key), MemoryType.SKILL, MemorySource.AGENT
    if target == "daily":
        day = key or datetime.now().strftime("%Y-%m-%d")
        return adapter.resolve_path(MemoryType.SHORT_TERM, day), MemoryType.SHORT_TERM, MemorySource.FLUSH
    if target == "path":
        if not key:
            raise ValueError("target=path ?閬?--key ??摰頝臬?")
        inferred = _infer_memory_type(key)
        return key.replace("\\", "/").lstrip("/"), inferred, MemorySource.AGENT
    raise ValueError(f"?芰 target: {target}")


def _infer_memory_type(path: str) -> MemoryType:
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized.startswith("10_Permanent/Profiles/"):
        return MemoryType.USER_PROFILE
    if normalized.startswith("11_AI_Mirror/ingestion_logs/daily_flush/"):
        return MemoryType.SHORT_TERM
    if normalized.startswith("00_System/Skills/"):
        return MemoryType.SKILL
    if normalized.startswith("70_Active_Plans/Session_Logs/"):
        return MemoryType.SESSION
    if normalized.startswith("10_Permanent/Concepts/"):
        return MemoryType.CONCEPT
    return MemoryType.LONG_TERM


def _slice_body(body: str, spec: str | None) -> str:
    if not spec:
        return body
    sep = ":" if ":" in spec else "-"
    if sep not in spec:
        raise ValueError("--lines ?澆????start-end ??start:end")
    start_raw, end_raw = spec.split(sep, 1)
    start = int(start_raw)
    end = int(end_raw)
    if start <= 0 or end < start:
        raise ValueError("--lines must satisfy: start > 0 and end >= start")
    lines = body.splitlines()
    selected = lines[start - 1 : end]
    return "\n".join(selected) + ("\n" if selected else "")


def _parse_source(raw: str) -> MemorySource:
    try:
        return MemorySource(raw)
    except ValueError as exc:
        raise ValueError(f"unsupported source: {raw}") from exc


def _parse_extras(items: list[str] | None) -> dict[str, str]:
    extras: dict[str, str] = {}
    if not items:
        return extras
    for item in items:
        if "=" not in item:
            raise ValueError(f"extra must be key=value: {item}")
        key, value = item.split("=", 1)
        extras[key.strip()] = value.strip()
    return extras


def _cmd_vault_show(args: argparse.Namespace) -> int:
    root, source = resolve_vault_root_with_source(args.vault_root)
    print(f"[OK] vault_root={root}")
    print(f"[OK] source={source}")
    print(f"[OK] user_config={user_config_path()}")
    if source == "default_workspace":
        print(f"[TIP] 可用 `memory-cli vault-set \"{root}\"` 固定預設第二大腦路徑。")
    return 0


def _cmd_vault_set(args: argparse.Namespace) -> int:
    saved = set_user_vault_root(args.path)
    print(f"[OK] saved_vault_root={saved}")
    print(f"[OK] user_config={user_config_path()}")
    print("[OK] 已儲存，後續 memory-cli 可不帶 --vault-root。")
    return 0


def _cmd_vault_clear(args: argparse.Namespace) -> int:
    removed = clear_user_vault_root()
    print(f"[OK] removed={removed}")
    print(f"[OK] user_config={user_config_path()}")
    return 0


def _cmd_brain_shell(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    overwrite_manifest = bool(args.force_manifest or args.owner_id or args.brain_id)
    manifest_path = adapter.ensure_brain_manifest(
        owner_id=args.owner_id or "owner",
        brain_id=args.brain_id,
        overwrite=overwrite_manifest,
    )
    scaffold_paths = adapter.ensure_runtime_profile_scaffold(overwrite=False)
    scope_doc = adapter.ensure_brain_scope_doc(overwrite=False)
    start_guide = adapter.absolute_path("00_System/08_Runtime_Profiles/START_HERE.md")
    bootstrap_steward = ensure_default_steward_persona(vault_root=adapter.vault_root, operator="brain-shell")
    persisted_config = None
    if args.set_default:
        persisted_config = str(set_user_vault_root(str(adapter.vault_root)))

    manifest = load_yaml_object(str(manifest_path))
    payload = {
        "vault_root": str(adapter.vault_root),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "runtime_profiles": {key: str(path) for key, path in scaffold_paths.items()},
        "scope_doc": str(scope_doc),
        "start_guide": str(start_guide),
        "bootstrap_steward": bootstrap_steward,
        "persisted_config": persisted_config,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] brain_shell_ready={adapter.vault_root}")
    print(f"[OK] brain_id={manifest.get('brain_id', '')}")
    print(f"[OK] owner_id={manifest.get('owner_id', '')}")
    print(f"[OK] manifest={manifest_path}")
    print(f"[OK] runtime_registry={scaffold_paths['registry']}")
    print(f"[OK] core_persona={scaffold_paths['core_persona']}")
    print(f"[OK] core_route={scaffold_paths['core_route']}")
    print(f"[OK] bootstrap_steward_status={bootstrap_steward.get('status', '')}")
    if bootstrap_steward.get("persona_id"):
        print(f"[OK] steward_persona={bootstrap_steward.get('persona_id', '')}")
    if bootstrap_steward.get("governance_path"):
        print(f"[OK] steward_governance={bootstrap_steward.get('governance_path', '')}")
    print(f"[OK] brain_scope_doc={scope_doc}")
    print(f"[OK] start_guide={start_guide}")
    if persisted_config:
        print(f"[OK] default_vault_saved={persisted_config}")
    return 0


def _cmd_brain_show(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    manifest_path = adapter.ensure_brain_manifest(owner_id="owner", brain_id=None, overwrite=False)
    manifest = load_yaml_object(str(manifest_path))
    registry_path = adapter.absolute_path("00_System/08_Runtime_Profiles/registry.yaml")
    registry = load_yaml_object(str(registry_path))
    payload = {
        "vault_root": str(adapter.vault_root),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "registry_path": str(registry_path),
        "registry": registry,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] vault_root={adapter.vault_root}")
    print(f"[OK] brain_id={manifest.get('brain_id', '')}")
    print(f"[OK] owner_id={manifest.get('owner_id', '')}")
    print(f"[OK] namespace={manifest.get('namespace', {}).get('range', '')}")
    print(f"[OK] default_persona={registry.get('default_persona', 'core')}")
    print(f"[OK] manifest={manifest_path}")
    print(f"[OK] registry={registry_path}")
    return 0


def _cmd_brain_seed_template(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = seed_brain_from_template(
        template_vault=Path(str(args.template_vault)),
        target_vault=adapter.vault_root,
        overwrite=bool(args.overwrite),
        include_personas=not bool(args.skip_personas),
        include_persona_skills=not bool(args.skip_persona_skills),
        include_shared_skills=bool(args.include_shared_skills),
        include_dialogue_modes=not bool(args.skip_dialogue_modes),
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] target_vault={payload.get('target_vault', '')}")
    print(f"[OK] template_vault={payload.get('template_vault', '')}")
    print(f"[OK] imported_personas={len(payload.get('imported_personas', []))}")
    print(f"[OK] copied_files={len(payload.get('copied_files', []))}")
    if payload.get("skipped_personas"):
        print(f"[OK] skipped_personas={','.join(payload.get('skipped_personas', []))}")
    print(f"[OK] registry={payload.get('registry_path', '')}")
    return 0


def _cmd_channel_bind(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    path, key = bind_channel_persona(
        adapter.vault_root,
        transport=args.transport,
        channel_id=args.channel_id,
        persona_id=args.persona,
        operator=args.operator,
    )
    payload = {
        "bindings_path": str(path),
        "key": key,
        "transport": args.transport,
        "channel_id": args.channel_id,
        "persona_id": sanitize_component(args.persona, fallback="core").lower(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] bindings_path={path}")
    print(f"[OK] key={key}")
    print(f"[OK] persona={payload['persona_id']}")
    return 0


def _cmd_channel_default_persona(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    path, persona = set_default_persona(
        adapter.vault_root,
        persona_id=args.persona,
        operator=args.operator,
    )
    payload = {
        "bindings_path": str(path),
        "default_persona": sanitize_component(args.persona, fallback="core").lower(),
        "operator": sanitize_component(args.operator, fallback="user").lower(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] bindings_path={path}")
    print(f"[OK] default_persona={persona}")
    return 0


def _cmd_channel_unbind(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    path, key, removed = unbind_channel(
        adapter.vault_root,
        transport=args.transport,
        channel_id=args.channel_id,
    )
    payload = {
        "bindings_path": str(path),
        "key": key,
        "removed": removed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] bindings_path={path}")
    print(f"[OK] key={key}")
    print(f"[OK] removed={removed}")
    return 0


def _cmd_channel_bindings(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = list_channel_bindings(adapter.vault_root)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] default_persona={payload.get('default_persona', 'core')}")
    bindings = payload.get("bindings", {})
    if not isinstance(bindings, dict):
        bindings = {}
    print(f"[OK] bindings={len(bindings)}")
    for key in sorted(bindings):
        item = bindings.get(key, {})
        if not isinstance(item, dict):
            item = {}
        print(
            f"- {key} | persona={item.get('persona_id', '')} "
            f"| updated_at={item.get('updated_at', '')}"
        )
    return 0


def _load_payload_json(payload_file: str | None, payload_json: str | None) -> dict[str, Any]:
    if payload_file and payload_json:
        raise ValueError("--payload-file ??--payload-json 鈭銝")
    raw = ""
    if payload_file:
        raw = Path(payload_file).expanduser().resolve().read_text(encoding="utf-8-sig")
    elif payload_json:
        raw = payload_json
    else:
        raise ValueError("?閬?--payload-file ??--payload-json")
    raw = raw.lstrip("\ufeff")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("payload JSON 敹???object")
    return payload


def _cmd_transport_show(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_transport_profiles(adapter.vault_root)
    if args.transport:
        profile = resolve_transport_profile(cfg, sanitize_component(args.transport, fallback="web").lower())
        payload = {"transport": profile.get("transport", ""), "profile": profile}
    else:
        payload = cfg
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.transport:
        print(f"[OK] transport={payload['transport']}")
        print(json.dumps(payload["profile"], ensure_ascii=False, indent=2))
    else:
        transports = cfg.get("transports", {})
        if not isinstance(transports, dict):
            transports = {}
        print(f"[OK] transports={len(transports)}")
        for name in sorted(transports):
            profile = resolve_transport_profile(cfg, str(name))
            print(
                f"- {name} | enabled={profile.get('enabled', True)} "
                f"| parser={profile.get('parser', 'generic')} "
                f"| use_binding={profile.get('use_binding', True)}"
            )
    return 0


def _cmd_dialogue_mode_show(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_dialogue_modes(adapter.vault_root)

    if args.resolve:
        persona = sanitize_component(str(args.persona), fallback="core").lower()
        transport = sanitize_component(str(args.transport), fallback="web").lower()
        requested_mode = str(getattr(args, "dialogue_mode", "") or "").strip() or None
        resolved = resolve_dialogue_mode(
            cfg,
            persona_id=persona,
            transport=transport,
            requested_mode=requested_mode,
        )
        payload: dict[str, Any] = {
            "persona": persona,
            "transport": transport,
            "requested_mode": requested_mode,
            "resolved": resolved,
        }
    else:
        payload = cfg

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.resolve:
        resolved = payload.get("resolved", {})
        print(f"[OK] persona={payload.get('persona', '')} transport={payload.get('transport', '')}")
        print(
            f"[OK] mode={resolved.get('mode', '')} "
            f"label={resolved.get('label', '')} source={resolved.get('source', '')}"
        )
        if resolved.get("prompt"):
            print(f"[OK] prompt={resolved.get('prompt', '')}")
        return 0

    modes = cfg.get("modes", {})
    if not isinstance(modes, dict):
        modes = {}
    print(f"[OK] modes={len(modes)}")
    for mode_id in sorted(modes):
        mode = modes.get(mode_id, {})
        if not isinstance(mode, dict):
            mode = {}
        print(f"- {mode_id} | {mode.get('label', '')}")
    return 0


def _cmd_transport_ingest(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = _load_payload_json(args.payload_file, args.payload_json)
    result = run_transport_event(
        vault_root=adapter.vault_root,
        transport=sanitize_component(args.transport, fallback="web").lower(),
        payload=payload,
        explicit_persona=args.persona,
        override_profile=args.override_profile,
        override_model=args.override_model,
        dialogue_mode=str(args.dialogue_mode).strip() or None,
        temperature=float(args.temperature),
        timeout_s=float(args.timeout),
        memory_mode=args.memory_mode,
        allow_llm_degraded=bool(args.allow_llm_degraded),
    )
    degraded = bool(result.get("degraded", False))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if bool(args.require_nondegraded) and degraded:
            return 2
        return 0

    print(result.get("response", ""))
    print("")
    print(
        f"[OK] transport={result.get('transport', '')} "
        f"persona={result.get('persona', '')} channel={result.get('channel_id', '')}"
    )
    print(
        f"[OK] llm={result.get('llm', {}).get('profile', '')} / "
        f"{result.get('llm', {}).get('model', '')}"
    )
    mode_payload = result.get("dialogue_mode", {})
    if isinstance(mode_payload, dict):
        print(
            f"[OK] dialogue_mode={mode_payload.get('mode', '')} "
            f"(source={mode_payload.get('source', '')})"
        )
    print(f"[OK] session_memory={result.get('memory_paths', {}).get('session', '')}")
    if result.get("memory_paths", {}).get("daily"):
        print(f"[OK] daily_memory={result.get('memory_paths', {}).get('daily', '')}")
    if bool(args.require_nondegraded) and degraded:
        print("[ERR] degraded response rejected by --require-nondegraded", file=sys.stderr)
        return 2
    return 0


def _cmd_task_create(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    task = create_task(
        adapter.vault_root,
        title=args.title,
        supervisor=args.supervisor,
        assignees=args.assignee,
        watchers=args.watcher,
        checklist=args.check,
        detail=args.detail,
        operator=args.operator,
        auto_cleanup=not bool(args.no_auto_cleanup),
    )
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] task_id={task.get('task_id', '')}")
    print(f"[OK] title={task.get('title', '')}")
    print(f"[OK] supervisor={task.get('supervisor', '')}")
    print(f"[OK] status={task.get('status', '')}")
    return 0


def _cmd_task_list(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    rows = list_tasks(
        adapter.vault_root,
        status=args.status,
        assignee=args.assignee,
        supervisor=args.supervisor,
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] tasks={len(rows)}")
    for item in rows:
        checklist = item.get("checklist", [])
        done_count = 0
        total_count = 0
        if isinstance(checklist, list):
            total_count = len(checklist)
            for cell in checklist:
                if isinstance(cell, dict) and bool(cell.get("done", False)):
                    done_count += 1
        print(
            f"- {item.get('task_id', '')} | {item.get('status', '')} | "
            f"supervisor={item.get('supervisor', '')} | "
            f"assignees={','.join(item.get('assignees', []))} | "
            f"checklist={done_count}/{total_count} | title={item.get('title', '')}"
        )
    return 0


def _cmd_task_update(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = set_task_status(
        adapter.vault_root,
        task_id=args.task_id,
        status=args.status,
        operator=args.operator,
        note=args.note,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] task_id={result.get('task_id', '')}")
    print(f"[OK] status={result.get('status', '')}")
    print(f"[OK] removed_from_active={result.get('removed_from_active', False)}")
    return 0


def _cmd_task_check(args: argparse.Namespace) -> int:
    if args.done and args.undone:
        raise ValueError("--done ??--undone 銝??雿輻")
    done_flag = True
    if args.undone:
        done_flag = False
    adapter = _build_adapter(args)
    result = set_task_check(
        adapter.vault_root,
        task_id=args.task_id,
        index=int(args.index),
        done=done_flag,
        operator=args.operator,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] task_id={result.get('task_id', '')}")
    print(f"[OK] checklist_index={result.get('index', 0)}")
    print(f"[OK] done={result.get('done', False)}")
    return 0


def _cmd_task_prune(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = prune_finished_tasks(adapter.vault_root, operator=args.operator)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] removed_count={result.get('removed_count', 0)}")
    removed = result.get("removed", [])
    if isinstance(removed, list):
        for task_id in removed:
            print(f"- {task_id}")
    return 0


def _cmd_skill_ingest(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = ingest_skill_file(
        adapter.vault_root,
        source_path=args.source_path,
        skill_id=args.skill_id,
        owner_persona=args.owner_persona,
        operator=args.operator,
        overwrite=bool(args.overwrite),
        scope=args.scope,
        persona_id=args.persona,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] skill_id={result.get('skill_id', '')}")
    print(f"[OK] skill_path={result.get('skill_path', '')}")
    return 0


def _cmd_skill_list(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    rows = list_skills(adapter.vault_root, scope=args.scope, persona_id=args.persona)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] skills={len(rows)}")
    for row in rows:
        persona_suffix = f" persona={row.get('persona_id', '')}" if row.get("scope", "shared") == "persona" else ""
        print(
            f"- {row.get('skill_id', '')} | scope={row.get('scope', 'shared')}{persona_suffix} | "
            f"status={row.get('status', '')} | "
            f"updated_at={row.get('updated_at', '')} | path={row.get('path', '')}"
        )
    return 0


def _cmd_skill_merge(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = merge_skills(
        adapter.vault_root,
        target_skill_id=args.target,
        source_skill_ids=args.source,
        operator=args.operator,
        archive_sources=not bool(args.no_archive_sources),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] target_skill_id={result.get('target_skill_id', '')}")
    print(f"[OK] target_skill_path={result.get('target_skill_path', '')}")
    archived = result.get("archived_sources", [])
    if isinstance(archived, list) and archived:
        print(f"[OK] archived_sources={','.join(archived)}")
    return 0


def _cmd_skill_index(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    path = refresh_skill_index(adapter.vault_root)
    payload = {"skill_index_path": path}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] skill_index_path={path}")
    return 0


def _resolve_skill_note_path(skill_id: str, *, scope: str, persona: str | None) -> str:
    sid = sanitize_component(skill_id, fallback="skill").lower()
    scope_norm = str(scope).strip().lower()
    if scope_norm == "persona":
        pid = sanitize_component(str(persona or "core"), fallback="core").lower()
        return f"00_System/Skills/_Persona/{pid}/{sid}/SKILL.md"
    return f"00_System/Skills/{sid}/SKILL.md"


def _cmd_skill_eval(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    note_path = _resolve_skill_note_path(args.skill_id, scope=args.scope, persona=args.persona)
    note = adapter.read_note(note_path)
    if note is None:
        raise ValueError(f"?曆??唳??踝?{note_path}")
    result = evaluate_skill_completeness(note.body)
    payload = {
        "skill_id": sanitize_component(args.skill_id, fallback="skill").lower(),
        "scope": args.scope,
        "persona": sanitize_component(args.persona, fallback="core").lower() if args.persona else "",
        "path": note_path,
        **result,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] skill_path={note_path}")
    print(f"[OK] score={payload['score']:.2f}")
    if payload["missing"]:
        print(f"[OK] missing={','.join(payload['missing'])}")
    else:
        print("[OK] missing=(none)")
    return 0


def _cmd_skill_promote(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = promote_persona_skill(
        adapter.vault_root,
        persona_id=args.persona,
        skill_id=args.skill_id,
        operator=args.operator,
        min_score=float(args.min_score),
        force=bool(args.force),
        archive_source=bool(args.archive_source),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] skill_id={result.get('skill_id', '')}")
    print(f"[OK] source_path={result.get('source_path', '')}")
    print(f"[OK] target_path={result.get('target_path', '')}")
    print(f"[OK] completeness_score={float(result.get('completeness_score', 0.0)):.2f}")
    return 0


def _cmd_skill_use(args: argparse.Namespace) -> int:
    if args.success and args.fail:
        raise ValueError("--success ??--fail 銝??雿輻")
    success: bool | None = None
    if args.success:
        success = True
    elif args.fail:
        success = False

    adapter = _build_adapter(args)
    result = record_skill_usage(
        adapter.vault_root,
        persona_id=args.persona,
        skill_id=args.skill_id,
        scope=args.scope,
        operator=args.operator,
        success=success,
        note=args.note,
        resolved_task_id=args.resolved_task_id,
        resolved_for=args.resolved_for,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] persona={result.get('persona_id', '')}")
    print(f"[OK] skill_id={result.get('skill_id', '')}")
    print(f"[OK] scope={result.get('scope', '')}")
    print(f"[OK] skill_path={result.get('skill_path', '')}")
    print(f"[OK] success={result.get('success', None)}")
    print(f"[OK] resolved_for={result.get('resolved_for', '')}")
    print(f"[OK] resolved_task_id={result.get('resolved_task_id', '') or '-'}")
    print(f"[OK] resolution_verified={result.get('resolution_verified', False)}")
    print(f"[OK] counts_for_growth={result.get('counts_for_growth', False)}")
    return 0


def _cmd_skill_maintain(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = run_skill_maintenance(
        adapter.vault_root,
        maintainer_persona=args.maintainer_persona,
        operator=args.operator,
        lookback_days=max(1, int(args.lookback_days)),
        min_usage=max(1, int(args.min_usage)),
        min_completeness=float(args.min_completeness),
        dry_run=bool(args.dry_run),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] report_path={result.get('report_path', '')}")
    promoted = result.get("promoted", [])
    skipped = result.get("skipped", [])
    candidates = result.get("candidates", [])
    print(f"[OK] candidates={len(candidates) if isinstance(candidates, list) else 0}")
    print(f"[OK] promoted={len(promoted) if isinstance(promoted, list) else 0}")
    print(f"[OK] skipped={len(skipped) if isinstance(skipped, list) else 0}")
    return 0


def _cmd_web_research(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = run_web_research(
        adapter.vault_root,
        query=args.query,
        operator=args.operator,
        max_results=max(1, int(args.max_results)),
        fetch_top=max(1, int(args.fetch_top)),
        timeout_s=float(args.timeout),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] query={result.get('query', '')}")
    print(f"[OK] note_path={result.get('note_path', '')}")
    print(f"[OK] fetched_count={result.get('fetched_count', 0)}")
    hits = result.get("hits", [])
    if isinstance(hits, list):
        for idx, hit in enumerate(hits, start=1):
            if not isinstance(hit, dict):
                continue
            print(f"{idx}. {hit.get('title', '')} | {hit.get('url', '')}")
    return 0


def _cmd_web_ingest_url(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = ingest_web_url(
        adapter.vault_root,
        url=args.url,
        title=args.title,
        operator=args.operator,
        timeout_s=float(args.timeout),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] url={result.get('url', '')}")
    print(f"[OK] note_path={result.get('note_path', '')}")
    return 0


def _cmd_smoke_test(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = run_smoke_suite(
        adapter.vault_root,
        with_web=bool(args.with_web),
        with_llm=bool(args.with_llm),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if bool(result.get("overall_ok", False)) else 1

    print(f"[OK] vault_root={result.get('vault_root', '')}")
    print(f"[OK] token={result.get('token', '')}")
    checks = result.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        mark = "PASS" if bool(item.get("ok", False)) else "FAIL"
        req = "required" if bool(item.get("required", True)) else "optional"
        print(
            f"- [{mark}] {item.get('name', '')} ({req}) | "
            f"{item.get('detail', '')} | path={item.get('path', '')}"
        )
    overall_ok = bool(result.get("overall_ok", False))
    print(f"[OK] overall={overall_ok}")
    return 0 if overall_ok else 1


def _cmd_compact_session(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    persona = sanitize_component(args.persona.strip(), fallback="core").lower()
    context = sanitize_component(args.context.strip(), fallback="cli")
    session = sanitize_component(args.session.strip(), fallback="default")
    result = compact_session_memory(
        adapter.vault_root,
        persona_id=persona,
        context_id=context,
        session_id=session,
        date=args.date,
        max_chars=max(600, int(args.max_chars)),
        keep_recent_turns=max(2, int(args.keep_recent_turns)),
        use_llm_summary=bool(args.use_llm_summary),
        llm_persona=sanitize_component(args.llm_persona.strip(), fallback="core").lower(),
        override_profile=args.override_profile,
        override_model=args.override_model,
        timeout_s=float(args.timeout),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"ok", "skipped"} else 1

    print(f"[OK] status={result.get('status', '')}")
    print(f"[OK] session_path={result.get('session_path', '')}")
    if result.get("daily_flush_path"):
        print(f"[OK] daily_flush_path={result.get('daily_flush_path', '')}")
    if result.get("compacted_turns") is not None:
        print(f"[OK] compacted_turns={result.get('compacted_turns', 0)}")
    if result.get("kept_turns") is not None:
        print(f"[OK] kept_turns={result.get('kept_turns', 0)}")
    if result.get("event_path"):
        print(f"[OK] event_path={result.get('event_path', '')}")
    if result.get("reason"):
        print(f"[OK] reason={result.get('reason', '')}")
    return 0


def _cmd_recall_show(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    rows = list_recall_entries(
        adapter.vault_root,
        limit=max(1, int(args.limit)),
        promoted_only=bool(args.promoted_only),
    )
    payload = {"count": len(rows), "entries": rows}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] count={payload['count']}")
    for item in rows:
        if not isinstance(item, dict):
            continue
        print(
            f"- path={item.get('path', '')} | recalls={item.get('recall_count', 0)} "
            f"| unique_queries={len(item.get('unique_query_hashes', []))} "
            f"| promoted={item.get('promoted', False)} "
            f"| target={item.get('promotion_target', '')}"
        )
    return 0


def _cmd_wikilinks_graph(args: argparse.Namespace) -> int:
    """V2 Phase A C13 — Wikilinks graph CLI: rebuild / status / neighbors."""
    from agent_memory.wikilinks_graph import (
        build_wikilinks_graph,
        default_graph_path,
        load_graph_json,
        neighbors as _neighbors,
        rebuild_and_save,
    )

    adapter = _build_adapter(args)
    vault = adapter.vault_root
    action = args.action

    if action == "rebuild":
        result = rebuild_and_save(vault)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[OK] wikilinks graph rebuilt")
            print(f"  path:       {result['graph_path']}")
            print(f"  nodes:      {result['nodes']}")
            print(f"  edges:      {result['edges']}")
            print(f"  unresolved: {result['unresolved']}")
            print(f"  built_at:   {result['built_at']}")
        return 0

    if action == "status":
        graph = load_graph_json(default_graph_path(vault))
        if graph is None:
            payload = {"ok": False, "reason": "no graph file — 用 --action rebuild 先建"}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("[INFO] 還沒建 graph. 跑: memory-cli wikilinks-graph --action rebuild")
            return 1
        payload = {
            "ok": True,
            "schema_version": graph.get("schema_version"),
            "built_at": graph.get("built_at"),
            "nodes": graph.get("nodes"),
            "edges": graph.get("edges"),
            "unresolved": graph.get("unresolved"),
            "adjacency_keys_sample": list(graph.get("adjacency", {}).keys())[:5],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[OK] graph status")
            print(f"  schema:     {payload['schema_version']}")
            print(f"  built_at:   {payload['built_at']}")
            print(f"  nodes:      {payload['nodes']}")
            print(f"  edges:      {payload['edges']}")
            print(f"  unresolved: {payload['unresolved']}")
            print(f"  sample 5 nodes:")
            for n in payload["adjacency_keys_sample"]:
                print(f"    - {n}")
        return 0

    if action == "neighbors":
        if not args.path:
            print("[ERR] --action neighbors 需要 --path")
            return 1
        graph = load_graph_json(default_graph_path(vault))
        if graph is None:
            print("[ERR] 沒 graph，先 --action rebuild")
            return 1
        nbs = _neighbors(graph, args.path, max_hops=max(1, int(args.max_hops)))
        payload = {"ok": True, "path": args.path, "max_hops": args.max_hops, "neighbors": nbs}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[OK] neighbors of {args.path} (max_hops={args.max_hops}):")
            for n in nbs:
                print(f"  - {n}")
            if not nbs:
                print("  (無)")
        return 0

    print(f"[ERR] unknown action: {action}")
    return 1


# ─── R7 C22: Curator observability + power user force-run ────────────────────


def _cmd_curator_status(args: argparse.Namespace) -> int:
    """Show curator state + should_run_now diagnosis for daily/weekly."""
    import json as _json
    from agent_memory.curator import (
        load_state, load_config, should_run_now, _now_local,
    )

    adapter = _build_adapter(args)
    vault = adapter.vault_root
    state = load_state(vault)
    config = load_config(vault)
    now = _now_local()
    daily_ok, daily_reason = should_run_now(state, config, "daily", now=now)
    weekly_ok, weekly_reason = should_run_now(state, config, "weekly", now=now)

    payload = {
        "vault_root": str(vault),
        "now_local": now.isoformat(),
        "config": {
            "daily_interval_hours": config.daily_interval_hours,
            "weekly_interval_hours": config.weekly_interval_hours,
            "min_idle_hours": config.min_idle_hours,
            "first_run_defer": config.first_run_defer,
            "paused": config.paused,
            "circuit_breaker_max_failures": config.circuit_breaker_max_failures,
        },
        "state": state.to_dict(),
        "daily": {"ok": daily_ok, "reason": daily_reason},
        "weekly": {"ok": weekly_ok, "reason": weekly_reason},
    }
    if args.json:
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("Curator state (R7 C18):")
    print(f"  now (本機時區): {payload['now_local']}")
    print(f"  last_daily_run_at  : {state.last_daily_run_at}")
    print(f"  last_weekly_run_at : {state.last_weekly_run_at}")
    print(f"  last_chat_at       : {state.last_chat_at}")
    print(f"  consecutive_failures: {state.consecutive_failures}")
    print()
    print("Should run now (AND 條件):")
    print(f"  daily  : ok={daily_ok:5} reason={daily_reason}")
    print(f"  weekly : ok={weekly_ok:5} reason={weekly_reason}")
    print()
    print("Config:")
    print(f"  daily_interval = {config.daily_interval_hours}h, weekly_interval = {config.weekly_interval_hours}h")
    print(f"  min_idle = {config.min_idle_hours}h, paused = {config.paused}")
    return 0


def _cmd_curator_force_run(args: argparse.Namespace) -> int:
    """Force-run curator (skip should_run_now)."""
    import json as _json
    from agent_memory.curator import force_run

    adapter = _build_adapter(args)
    mode = (args.mode or "daily").strip().lower()
    if mode not in ("daily", "weekly"):
        print(f"[ERR] unknown mode: {mode}")
        return 1
    result = force_run(adapter.vault_root, mode)
    if args.json:
        print(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"[OK] curator force_run ({mode}) — started_at={result.get('started_at')} ended_at={result.get('ended_at')}")
    if mode == "daily":
        print(f"  scanned_flushes: {result.get('scanned_flushes', 0)}")
        print(f"  aggregated entries: {len(result.get('aggregated', []))}")
        umbrella = result.get("umbrella", {})
        print(f"  umbrella groups: {umbrella.get('groups_count', 0)}, consolidated: {umbrella.get('consolidated_count', 0)}")
    else:
        steps = result.get("steps", {})
        promote = steps.get("promote_midterm_to_long", {})
        demote = steps.get("demote_long", {})
        skill = steps.get("skill_suggestions_scan", {})
        print(f"  promote_midterm_to_long: promoted={promote.get('promoted_count', 0)} candidates={promote.get('candidates_count', 0)}")
        print(f"  demote_long           : staled={demote.get('staled_count', 0)} archived={demote.get('archived_count', 0)}")
        print(f"  skill_suggestions_scan: new={skill.get('new_added_count', 0)} pending_total={skill.get('total_pending', 0)}")
    errors = result.get("errors", [])
    if errors:
        print(f"  errors: {errors}")
    return 0


def _cmd_skill_suggestions_list(args: argparse.Namespace) -> int:
    """List R7 C20b pending skill upgrade suggestions."""
    import json as _json
    from agent_memory.skill_suggestions import load_pending

    adapter = _build_adapter(args)
    pending = load_pending(adapter.vault_root)
    include_resolved = bool(args.include_resolved)

    visible = [
        s for s in pending
        if include_resolved or (not s.get("dismissed_at") and not s.get("promoted_to"))
    ]

    if args.json:
        print(_json.dumps(visible, ensure_ascii=False, indent=2))
        return 0

    if not visible:
        print("[INFO] 沒有 pending skill 升格提議.")
        print("  (curator weekly deep run 才會 scan; 也要 Mid_Term 有 tag 含 procedure/workflow/steps 的檔)")
        return 0

    print(f"[OK] pending skill suggestions ({len(visible)}):")
    for s in visible:
        marker = "✓" if s.get("promoted_to") else ("✗" if s.get("dismissed_at") else "·")
        print(f"  {marker} {s.get('entity_id', '')} (mention={s.get('mention_count', 0)})")
        print(f"      summary: {s.get('summary', '')[:100]}")
        print(f"      source : {s.get('source_path', '')}")
        print(f"      proposed_at: {s.get('proposed_at', '')}")
        if s.get("promoted_to"):
            print(f"      promoted_to: {s.get('promoted_to')}")
        if s.get("dismissed_at"):
            print(f"      dismissed_at: {s.get('dismissed_at')} reason={s.get('dismiss_reason', '')}")
    return 0


def _cmd_reflect(args: argparse.Namespace) -> int:
    """R9 C29 — on-demand reflect topic. 預設真 LLM call (LLM 不可用 fallback)."""
    import json as _json
    from agent_memory.reflect import reflect_topic

    adapter = _build_adapter(args)
    result = reflect_topic(adapter.vault_root, args.topic, max_match=max(1, int(args.max_match)))

    if args.json:
        print(_json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    action = result.get("action")
    if action == "created":
        print(f"[OK] reflection 已建立: {result['path']}")
        print(f"  從 {result['matches_count']} 個 .md 整理 (LLM mock_used={result.get('mock_used', False)})")
        return 0
    if action == "no_matches":
        print(f"[INFO] 沒找到跟 '{args.topic}' 相關的記憶. 試其他主題或先讓對話累積一些 Mid_Term entity.")
        return 0
    if action == "llm_failed":
        print(f"[ERR] LLM call 失敗 (matched {result.get('matches', 0)} 個 .md 但無法產 reflection).")
        print("  - 檢查 .env 內 LLM API key / endpoint, 或用 mock 跑 e2e 測試.")
        return 1
    print(f"[ERR] {result}")
    return 1


def _cmd_midterm_list(args: argparse.Namespace) -> int:
    """List Mid_Term/ entries (R7 中期記憶累積狀態)."""
    import json as _json
    from agent_memory.memory_promotion import list_midterm_entries

    adapter = _build_adapter(args)
    entries = list_midterm_entries(
        adapter.vault_root,
        min_mention_count=max(0, int(args.min_mention)),
        only_unpromoted=not bool(args.all),
    )

    if args.json:
        print(_json.dumps(entries, ensure_ascii=False, indent=2))
        return 0

    if not entries:
        print("[INFO] Mid_Term/ 還沒有 entity. 觸發條件: 對話累積 daily_flush 後 curator daily light idle 2h+24h 跑.")
        return 0

    print(f"[OK] Mid_Term entries ({len(entries)}):")
    for e in entries:
        pin_marker = " 📌" if e.get("pinned") else ""
        print(f"  {e['entity_id']:30s} mention={e['mention_count']:3d}  state={e['lifecycle_state']:8s}  last_active={e['last_activity_at']}{pin_marker}")
    return 0


def _cmd_promote_cycle(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    thresholds = PromotionThresholds(
        min_score=float(args.min_score),
        min_recall_count=max(1, int(args.min_recall)),
        min_unique_queries=max(1, int(args.min_queries)),
        min_unique_days=max(1, int(args.min_days)),
        grace_period_hours=max(0.0, float(args.grace_hours)),
    )
    result = run_promotion_cycle(
        adapter.vault_root,
        phase=args.phase,
        thresholds=thresholds,
        operator=args.operator,
        dry_run=bool(args.dry_run),
        max_promotions=max(1, int(args.max_promotions)),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    promoted = result.get("promoted", [])
    skipped = result.get("skipped", [])
    candidates = result.get("candidates", [])
    print(f"[OK] phase={result.get('phase', '')} dry_run={result.get('dry_run', False)}")
    print(f"[OK] candidates={len(candidates) if isinstance(candidates, list) else 0}")
    print(f"[OK] promoted={len(promoted) if isinstance(promoted, list) else 0}")
    print(f"[OK] skipped={len(skipped) if isinstance(skipped, list) else 0}")
    if isinstance(promoted, list):
        for item in promoted[:10]:
            if not isinstance(item, dict):
                continue
            print(
                f"- {item.get('path', '')} -> {item.get('target_path', '')} "
                f"| type={item.get('target_type', '')} | score={item.get('score', 0)}"
            )
    return 0


def _cmd_core_e2e(args: argparse.Namespace) -> int:
    adapter, runtime = _build_runtime(args, persona_id=args.persona)
    token = datetime.now().strftime("%Y%m%d-%H%M%S")
    persona = sanitize_component(args.persona.strip(), fallback="core").lower()
    context = sanitize_component(args.context.strip(), fallback="e2e")
    session = sanitize_component(args.session.strip(), fallback=f"e2e-{token}")

    candidate_path = f"11_AI_Mirror/internalised_candidates/e2e-{token}.md"
    candidate_content = (
        f"# e2e {token}\n\n"
        "## facts\n\n"
        f"- token: {token}\n"
        "- ?蝡臬蝡舀?蝔葫閰西????其?撽? recall ??promotion?n"
        "- ??瘙箇?嚗雁??Obsidian Markdown ?箄??嗥??潦n"
    )
    runtime.apply_memory_tool(
        action="replace",
        path=candidate_path,
        content=candidate_content,
        reason="core-e2e write",
        agent="core-e2e",
        source=MemorySource.MIRROR,
        tags=["e2e", "candidate"],
        extras={"token": token},
    )

    runtime.memory_search(query=f"e2e token {token}", max_results=5)
    runtime.memory_search(query=f"important decision {token}", max_results=5)

    for idx in range(8):
        append_chat_turn(
            adapter,
            persona_id=persona,
            context_id=context,
            session_id=session,
            user_message=(f"[{idx + 1}] e2e 測試對話 token={token}，請整理重點後寫入記憶。"),
            assistant_message=("收到，先摘要再進行寫入與後續 promotion 驗證。"),
        )

    compact = compact_session_memory(
        adapter.vault_root,
        persona_id=persona,
        context_id=context,
        session_id=session,
        max_chars=900,
        keep_recent_turns=3,
        use_llm_summary=False,
    )
    promote = run_promotion_cycle(
        adapter.vault_root,
        phase="light",
        thresholds=PromotionThresholds(
            min_score=0.0,
            min_recall_count=1,
            min_unique_queries=1,
            min_unique_days=1,
            grace_period_hours=0.0,
        ),
        operator="core-e2e",
        dry_run=False,
        max_promotions=5,
    )
    promoted = promote.get("promoted", [])
    overall_ok = bool(compact.get("status") == "ok" and isinstance(promoted, list) and len(promoted) > 0)
    payload = {
        "token": token,
        "persona": persona,
        "context": context,
        "session": session,
        "candidate_path": candidate_path,
        "compaction": compact,
        "promotion": promote,
        "overall_ok": overall_ok,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if overall_ok else 1

    print(f"[OK] token={token}")
    print(f"[OK] candidate_path={candidate_path}")
    print(f"[OK] compaction_status={compact.get('status', '')}")
    print(f"[OK] promoted_count={len(promoted) if isinstance(promoted, list) else 0}")
    print(f"[OK] overall={overall_ok}")
    return 0 if overall_ok else 1


def _cmd_persona_create(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    if bool(args.enable_tools) and bool(args.disable_tools):
        raise ValueError("--enable-tools and --disable-tools are mutually exclusive")
    tool_access_enabled: bool | None = None
    if bool(args.enable_tools):
        tool_access_enabled = True
    elif bool(args.disable_tools):
        tool_access_enabled = False

    result = create_persona_proposal(
        vault_root=adapter.vault_root,
        display_name=args.display_name,
        persona_id=args.persona_id,
        mission=args.mission,
        style=args.style,
        language=args.language,
        default_mode=args.default_mode,
        role_type=args.role_type,
        allow_experimental_role=bool(args.allow_experimental_role),
        include=args.include,
        exclude=args.exclude,
        allow=args.allow,
        deny=args.deny,
        tool_access_enabled=tool_access_enabled,
        operator=args.operator,
        auto_approve=bool(args.auto_approve),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona_id={result.get('persona_id', '')}")
    print(f"[OK] status={result.get('status', '')}")
    if result.get("proposal_id"):
        print(f"[OK] proposal_id={result.get('proposal_id')}")
    if result.get("proposal_path"):
        print(f"[OK] proposal_path={result.get('proposal_path')}")
    if result.get("persona_path"):
        print(f"[OK] persona_path={result.get('persona_path')}")
    if result.get("route_path"):
        print(f"[OK] route_path={result.get('route_path')}")
    if result.get("governance_path"):
        print(f"[OK] governance_path={result.get('governance_path')}")
    return 0


def _cmd_persona_approve(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = approve_persona_proposal(
        vault_root=adapter.vault_root,
        proposal_id=args.proposal_id,
        operator=args.operator,
        overwrite=bool(args.overwrite),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona_id={result.get('persona_id', '')}")
    print(f"[OK] proposal_id={result.get('proposal_id', '')}")
    print(f"[OK] status={result.get('status', '')}")
    print(f"[OK] persona_path={result.get('persona_path', '')}")
    print(f"[OK] route_path={result.get('route_path', '')}")
    print(f"[OK] registry_path={result.get('registry_path', '')}")
    if result.get("governance_path"):
        print(f"[OK] governance_path={result.get('governance_path', '')}")
    return 0


def _cmd_persona_disable(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    result = disable_persona(
        vault_root=adapter.vault_root,
        persona_id=args.persona,
        operator=args.operator,
        reason=args.reason,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona_id={result.get('persona_id', '')}")
    print(f"[OK] status={result.get('status', '')}")
    print(f"[OK] registry_path={result.get('registry_path', '')}")
    print(f"[OK] persona_path={result.get('persona_path', '')}")
    if result.get("governance_path"):
        print(f"[OK] governance_path={result.get('governance_path', '')}")
    return 0


def _cmd_persona_update(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    if bool(args.enable_tools) and bool(args.disable_tools):
        raise ValueError("--enable-tools and --disable-tools are mutually exclusive")
    tool_access_enabled: bool | None = None
    if bool(args.enable_tools):
        tool_access_enabled = True
    elif bool(args.disable_tools):
        tool_access_enabled = False

    result = update_persona_profile(
        vault_root=adapter.vault_root,
        persona_id=args.persona,
        operator=args.operator,
        display_name=args.display_name,
        mission=args.mission,
        style=args.style,
        language=args.language,
        default_mode=args.default_mode,
        role_type=args.role_type,
        allow_experimental_role=bool(args.allow_experimental_role),
        tool_access_enabled=tool_access_enabled,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona_id={result.get('persona_id', '')}")
    print(f"[OK] status={result.get('status', '')}")
    changed = result.get("changed_fields", [])
    if isinstance(changed, list):
        print(f"[OK] changed_fields={','.join(changed) if changed else '(none)'}")
    print(f"[OK] persona_path={result.get('persona_path', '')}")
    print(f"[OK] route_path={result.get('route_path', '')}")
    if result.get("governance_path"):
        print(f"[OK] governance_path={result.get('governance_path', '')}")
    return 0


def _cmd_persona_list(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = list_personas(vault_root=adapter.vault_root)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] default_persona={payload.get('default_persona', 'core')}")
    personas = payload.get("personas", {})
    if not isinstance(personas, dict):
        personas = {}
    print(f"[OK] personas={len(personas)}")
    for pid in sorted(personas):
        entry = personas.get(pid, {})
        if not isinstance(entry, dict):
            entry = {}
        status = str(entry.get("status", "unknown"))
        persona_path = str(entry.get("persona_path", ""))
        route_path = str(entry.get("route_path", ""))
        print(f"- {pid} | status={status} | persona={persona_path} | route={route_path}")
    return 0


def _cmd_persona_pack(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = export_persona_bundle(
        vault_root=adapter.vault_root,
        persona_id=str(args.persona),
        output_dir=Path(str(args.output_dir)) if args.output_dir else None,
        include_sessions=not bool(args.skip_sessions),
        include_persona_skills=not bool(args.skip_persona_skills),
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona={payload.get('persona_id', '')}")
    print(f"[OK] bundle_path={payload.get('bundle_path', '')}")
    print(f"[OK] path_count={payload.get('path_count', 0)}")
    return 0


def _cmd_persona_unpack(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    payload = import_persona_bundle(
        vault_root=adapter.vault_root,
        bundle_path=Path(str(args.package)),
        overwrite=bool(args.overwrite),
        force_active=not bool(args.keep_status),
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona={payload.get('persona_id', '')}")
    print(f"[OK] copied={payload.get('copied_count', 0)} skipped={payload.get('skipped_count', 0)}")
    print(f"[OK] registry={payload.get('registry_path', '')}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    overwrite_manifest = bool(args.force_manifest or args.owner_id or args.brain_id)
    manifest_path = adapter.ensure_brain_manifest(
        owner_id=(args.owner_id or "owner"),
        brain_id=args.brain_id,
        overwrite=overwrite_manifest,
    )
    scaffold_paths = adapter.ensure_runtime_profile_scaffold(overwrite=False)
    bootstrap_steward = ensure_default_steward_persona(vault_root=adapter.vault_root, operator="init")
    start_guide = adapter.absolute_path("00_System/08_Runtime_Profiles/START_HERE.md")

    print(f"[OK] Vault 初始化完成：{adapter.vault_root}")
    print(f"[OK] brain_manifest={manifest_path}")
    print(f"[OK] runtime_registry={scaffold_paths['registry']}")
    print(f"[OK] bootstrap_steward_status={bootstrap_steward.get('status', '')}")
    if bootstrap_steward.get("persona_id"):
        print(f"[OK] steward_persona={bootstrap_steward.get('persona_id', '')}")
    print(f"[OK] start_guide={start_guide}")
    print(f"[OK] USER={adapter.obsidian_uri('10_Permanent/Profiles/USER.md')}")
    print(f"[OK] MEMORY={adapter.obsidian_uri('10_Permanent/MEMORY.md')}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    adapter, runtime = _build_runtime(args)
    path, mtype, source = _resolve_add_target(adapter, args.target, args.key)
    if args.target == "daily":
        date_key = args.key or datetime.now().strftime("%Y-%m-%d")
        adapter.append_daily(date_key, args.content, agent=args.agent)
        print(f"[OK] added_daily={path}")
        print(f"[OK] uri={adapter.obsidian_uri(path)}")
        return 0

    result = runtime.apply_memory_tool(
        action="add",
        path=path,
        content=args.content,
        agent=args.agent,
        source=source,
        tags=[mtype.value, args.target],
        extras={"target": args.target},
    )
    print(f"[OK] added_path={result.path}")
    print(f"[OK] uri={adapter.obsidian_uri(result.path)}")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    adapter, runtime = _build_runtime(args)
    result = runtime.memory_get(path=args.path)
    if not result.ok or result.note is None:
        print(f"[ERR] ?曆??唳?獢?{args.path}", file=sys.stderr)
        return 1

    if args.body_only:
        print(_slice_body(result.note.body, args.lines), end="")
        return 0

    body = _slice_body(result.note.body, args.lines)
    text = adapter.serialize_frontmatter(_frontmatter_dict(result.note.frontmatter), body)
    print(text, end="")
    if result.citation is not None:
        print(f"\n[URI] {result.citation.obsidian_uri}")
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    adapter, runtime = _build_runtime(args)
    if args.action == "get":
        got = runtime.memory_get(path=args.path)
        if not got.ok or got.note is None:
            print(f"[ERR] ?曆??唳?獢?{args.path}", file=sys.stderr)
            return 1
        body = _slice_body(got.note.body, args.lines)
        if args.body_only:
            print(body, end="")
            return 0
        text = adapter.serialize_frontmatter(_frontmatter_dict(got.note.frontmatter), body)
        print(text, end="")
        if got.citation is not None:
            print(f"\n[URI] {got.citation.obsidian_uri}")
        return 0

    source = _parse_source(args.source)
    result = runtime.apply_memory_tool(
        action=args.action,
        path=args.path,
        content=args.content,
        reason=args.reason,
        agent=args.agent,
        source=source,
        tags=args.tag,
        extras=_parse_extras(args.extra),
    )

    print(f"[OK] action={result.action} path={result.path} status={result.message}")
    if result.note is not None:
        print(f"[OK] uri={adapter.obsidian_uri(result.path)}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    _, runtime = _build_runtime(args)
    snapshot = runtime.frozen_snapshot(user_profile_path=args.user_path, memory_path=args.memory_path)
    print(snapshot, end="")
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    _, runtime = _build_runtime(args)
    stats = runtime.reindex_search(
        include_prefixes=args.include,
        exclude_prefixes=args.exclude,
        sync_views=False,
    )
    view_payload: dict[str, int] | None = None
    if args.no_sync_views:
        view_payload = None
    else:
        view_payload = runtime.sync_user_index_views()
    payload = {
        "scanned": stats.scanned,
        "indexed": stats.indexed,
        "skipped": stats.skipped,
        "removed": stats.removed,
        "failed": stats.failed,
        "db_path": str(runtime.search_manager.db_path),
        "views": view_payload,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] db={payload['db_path']}")
    print(
        "[OK] "
        f"scanned={stats.scanned} indexed={stats.indexed} "
        f"skipped={stats.skipped} removed={stats.removed} failed={stats.failed}"
    )
    if view_payload is not None:
        print(
            "[OK] views "
            f"total={view_payload['total_notes']} active={view_payload['active_notes']} "
            f"files={view_payload['files_written']}"
        )
    else:
        print("[OK] views skipped")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    _, runtime = _build_runtime(args)
    limit = max(1, int(args.limit))
    if args.mmr and args.no_mmr:
        raise ValueError("銝??雿輻 --mmr ??--no-mmr")
    mmr_override: bool | None = None
    if args.mmr:
        mmr_override = True
    elif args.no_mmr:
        mmr_override = False
    hits = runtime.memory_search(
        query=args.query,
        max_results=limit,
        include_archived=args.include_archived,
        auto_reindex=not args.no_auto_reindex,
        strategy=args.strategy,
        use_mmr=mmr_override,
        mmr_lambda=args.mmr_lambda,
    )
    if args.json:
        payload = [
            {
                "path": hit.path,
                "snippet": hit.snippet,
                "score": hit.score,
                "source": hit.source,
                "metadata": hit.metadata,
            }
            for hit in hits
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    strategy_used = args.strategy or (
        str(hits[0].metadata.get("strategy", "hybrid")) if hits else "default"
    )
    print(f"[OK] hits={len(hits)} strategy={strategy_used}")
    for idx, hit in enumerate(hits, start=1):
        uri = str(hit.metadata.get("obsidian_uri", ""))
        snippet = " ".join(str(hit.snippet).splitlines()).strip()
        score = f"{hit.score:.4f}"
        print(f"{idx}. [{score}] {hit.path}")
        print(f"   {snippet}")
        if uri:
            print(f"   {uri}")
    return 0


def _cmd_sync_views(args: argparse.Namespace) -> int:
    _, runtime = _build_runtime(args)
    result = runtime.sync_user_index_views(output_dir=args.output_dir)
    payload = {
        "output_dir": args.output_dir,
        "stats": result,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] output_dir={args.output_dir}")
    print(
        "[OK] "
        f"total={result['total_notes']} active={result['active_notes']} "
        f"archived={result['archived_notes']} "
        f"folders={result['folder_groups']} tags={result['tag_groups']} "
        f"files={result['files_written']}"
    )
    return 0


def _cmd_serve_dashboard(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    from agent_memory.dashboard_server import serve_dashboard

    print(f"[OK] dashboard_url=http://{args.host}:{args.port}")
    print(f"[OK] vault_root={adapter.vault_root}")
    print("[OK] Press Ctrl+C to stop server.")
    serve_dashboard(adapter.vault_root, host=str(args.host), port=int(args.port))
    return 0


def _cmd_serve_transport_bridge(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    from agent_memory.transport_bridge_server import serve_transport_bridge

    print(f"[OK] transport_bridge_url=http://{args.host}:{args.port}")
    print("[OK] endpoints: POST /webhook/discord , POST /webhook/line")
    print(f"[OK] vault_root={adapter.vault_root}")
    print("[OK] Press Ctrl+C to stop server.")
    serve_transport_bridge(adapter.vault_root, host=str(args.host), port=int(args.port))
    return 0


def _cmd_notion_queue(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    body = str(args.body or "").strip()
    if args.body_file:
        path = Path(str(args.body_file)).expanduser()
        body = path.read_text(encoding="utf-8")
    if not body:
        raise SystemExit("[ERR] 隢?靘?--body ???? --body-file")
    tags = args.tag if isinstance(args.tag, list) else None
    result = queue_notion_publish(
        adapter.vault_root,
        title=str(args.title),
        body_md=body,
        operator=str(args.operator),
        persona_hint=str(args.persona_hint or ""),
        target_hint=str(args.target_hint or ""),
        tags=tags,
        priority=str(args.priority),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] notion_queue_id={result['notion_queue_id']}")
    print(f"[OK] path={result['relative_path']}")
    return 0


def _cmd_notion_queue_list(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    rows = list_notion_queue_items(
        adapter.vault_root,
        status_filter=str(args.status).strip() if args.status else None,
        limit=int(args.limit),
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(empty)")
        return 0
    for row in rows:
        print(f"- {row.get('relative_path')} | {row.get('status')} | {row.get('title')} | {row.get('notion_queue_id')}")
    return 0


def _cmd_retrieval_benchmark(args: argparse.Namespace) -> int:
    adapter, runtime = _build_runtime(args)
    # Benchmark should compare retrieval variants on a consistent index baseline.
    runtime.reindex_search(sync_views=False)
    cases = benchmark_default_cases()
    if args.cases_file and str(args.cases_file).strip():
        case_path = Path(str(args.cases_file)).expanduser()
        if not case_path.is_absolute():
            case_path = (adapter.vault_root / case_path).resolve()
        cases = load_cases_from_yaml(case_path)
    payload = run_benchmark(
        runtime,
        cases=cases,
        variants=benchmark_default_variants(),
        limit=max(1, int(args.limit)),
        include_archived=bool(args.include_archived),
        auto_reindex_each_query=bool(args.auto_reindex_each_query),
    )
    payload["cases_source"] = str(args.cases_file).strip() or "built-in"
    payload["router_path"] = str(adapter.absolute_path(RETRIEVAL_ROUTER_RELATIVE_PATH))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] benchmark_cases={len(cases)}")
    print(f"[OK] recommended_variant={payload.get('recommended', '')}")
    for row in payload.get("variants", []):
        summary = row.get("summary", {})
        print(
            "- "
            f"{row.get('variant')} | avg_ms={summary.get('avg_latency_ms')} "
            f"| top1={summary.get('top1_path_hit_rate')} "
            f"| any={summary.get('any_path_hit_rate')} "
            f"| keyword={summary.get('keyword_hit_rate')}"
        )
    return 0


def _cmd_llm_show(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_llm_router_config(adapter.vault_root)
    resolved = resolve_llm_route(
        cfg,
        persona_id=args.persona,
        override_profile=args.override_profile,
        override_model=args.override_model,
    )
    payload = {
        "router_path": str(adapter.absolute_path("00_System/08_Runtime_Profiles/llm_router.yaml")),
        "resolved": resolved,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] persona={resolved['persona_id'] or 'core'}")
    print(f"[OK] selected={resolved['selected_profile']} / {resolved['selected_model']}")
    for item in resolved["chain"]:
        key_status = "ok" if item["api_key_present"] else "missing"
        print(
            f"{item['rank']}. {item['profile']} | {item['model']} | "
            f"{item['zh_label'] or item['kind']} | key:{key_status}"
        )
    return 0


def _cmd_llm_set_default(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_llm_router_config(adapter.vault_root)
    if not isinstance(cfg.get("global_default"), dict):
        cfg["global_default"] = {}
    cfg["global_default"]["profile"] = args.profile.strip()
    cfg["global_default"]["model"] = args.model.strip()
    save_llm_router_config(adapter.vault_root, cfg)
    payload = {
        "router_path": str(adapter.absolute_path("00_System/08_Runtime_Profiles/llm_router.yaml")),
        "global_default": cfg["global_default"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] global_default={cfg['global_default']['profile']} / {cfg['global_default']['model']}")
    return 0


def _cmd_llm_set_persona(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_llm_router_config(adapter.vault_root)
    persona = args.persona.strip()
    if not persona:
        raise ValueError("persona 銝?箇征")
    if not isinstance(cfg.get("persona_overrides"), dict):
        cfg["persona_overrides"] = {}
    cfg["persona_overrides"][persona] = {
        "profile": args.profile.strip(),
        "model": args.model.strip(),
    }
    save_llm_router_config(adapter.vault_root, cfg)
    payload = {
        "router_path": str(adapter.absolute_path("00_System/08_Runtime_Profiles/llm_router.yaml")),
        "persona": persona,
        "override": cfg["persona_overrides"][persona],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(
        f"[OK] persona_override[{persona}]="
        f"{cfg['persona_overrides'][persona]['profile']} / {cfg['persona_overrides'][persona]['model']}"
    )
    return 0


def _cmd_llm_clear_persona(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_llm_router_config(adapter.vault_root)
    persona = args.persona.strip()
    removed = False
    if isinstance(cfg.get("persona_overrides"), dict) and persona in cfg["persona_overrides"]:
        del cfg["persona_overrides"][persona]
        removed = True
    save_llm_router_config(adapter.vault_root, cfg)
    payload = {
        "router_path": str(adapter.absolute_path("00_System/08_Runtime_Profiles/llm_router.yaml")),
        "persona": persona,
        "removed": removed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] persona_override_removed={removed} persona={persona}")
    return 0


def _cmd_retrieval_show(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_retrieval_router_config(adapter.vault_root)
    resolved = resolve_retrieval_route(cfg, persona_id=args.persona.strip() or None)
    payload = {
        "router_path": str(adapter.absolute_path(RETRIEVAL_ROUTER_RELATIVE_PATH)),
        "persona": args.persona.strip(),
        "resolved": resolved,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    embedding = resolved.get("embedding", {})
    search = resolved.get("search", {})
    print(f"[OK] retrieval_router={payload['router_path']}")
    print(
        f"[OK] embedding={embedding.get('mode', 'hash')} "
        f"profile={embedding.get('profile', '')} model={embedding.get('model', '')}"
    )
    print(
        f"[OK] search_strategy={search.get('default_strategy', 'hybrid')} "
        f"mmr_enabled={search.get('mmr_enabled', True)} "
        f"mmr_lambda={search.get('mmr_lambda', 0.7)}"
    )
    return 0


def _cmd_retrieval_set_embedding(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_retrieval_router_config(adapter.vault_root)
    if not isinstance(cfg.get("embedding"), dict):
        cfg["embedding"] = {}
    mode = str(args.mode).strip().lower()
    embedding = cfg["embedding"]
    embedding["mode"] = mode
    if mode == "provider":
        profile = args.profile.strip()
        model = args.model.strip()
        if not profile or not model:
            raise ValueError("mode=provider ??--profile ??--model 銝?箇征")
        embedding["profile"] = profile
        embedding["model"] = model
    embedding["timeout_s"] = max(5.0, float(args.timeout))
    save_retrieval_router_config(adapter.vault_root, cfg)
    resolved = resolve_retrieval_route(cfg, persona_id=None)
    payload = {
        "router_path": str(adapter.absolute_path(RETRIEVAL_ROUTER_RELATIVE_PATH)),
        "embedding": resolved.get("embedding", {}),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(
        f"[OK] embedding={payload['embedding'].get('mode', 'hash')} "
        f"profile={payload['embedding'].get('profile', '')} "
        f"model={payload['embedding'].get('model', '')}"
    )
    return 0


def _cmd_retrieval_set_search(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    cfg = load_retrieval_router_config(adapter.vault_root)
    if not isinstance(cfg.get("search"), dict):
        cfg["search"] = {}
    search_cfg = cfg["search"]
    search_cfg["default_strategy"] = args.default_strategy
    if args.mmr_enabled and args.mmr_disabled:
        raise ValueError("銝??閮剖? --mmr-enabled ??--mmr-disabled")
    if args.mmr_enabled:
        search_cfg["mmr_enabled"] = True
    elif args.mmr_disabled:
        search_cfg["mmr_enabled"] = False
    search_cfg["mmr_lambda"] = max(0.0, min(1.0, float(args.mmr_lambda)))
    search_cfg["mmr_candidate_multiplier"] = max(2, int(args.candidate_multiplier))
    save_retrieval_router_config(adapter.vault_root, cfg)
    resolved = resolve_retrieval_route(cfg, persona_id=None)
    payload = {
        "router_path": str(adapter.absolute_path(RETRIEVAL_ROUTER_RELATIVE_PATH)),
        "search": resolved.get("search", {}),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(
        f"[OK] strategy={payload['search'].get('default_strategy', 'hybrid')} "
        f"mmr_enabled={payload['search'].get('mmr_enabled', True)} "
        f"mmr_lambda={payload['search'].get('mmr_lambda', 0.7)} "
        f"candidate_mul={payload['search'].get('mmr_candidate_multiplier', 4)}"
    )
    return 0


def _cmd_llm_log(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    events = list_llm_route_events(
        adapter.vault_root,
        limit=max(1, int(args.limit)),
        persona_id=args.persona.strip(),
        session_id=args.session.strip(),
        transport=args.transport.strip(),
    )
    payload = {
        "ledger_path": str(adapter.absolute_path(LLM_ROUTE_EVENTS_RELATIVE_PATH)),
        "count": len(events),
        "events": events,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"[OK] ledger_path={payload['ledger_path']}")
    print(f"[OK] count={payload['count']}")
    for item in events:
        if not isinstance(item, dict):
            continue
        print(
            f"- {item.get('timestamp', '')} | persona={item.get('persona_id', '')} "
            f"| transport={item.get('transport', '') or 'n/a'} "
            f"| llm={item.get('profile', '')}/{item.get('model', '')} "
            f"| session={item.get('session_id', '')}"
        )
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    transport = sanitize_component(args.transport.strip() or "cli", fallback="cli").lower()
    channel_id = sanitize_component(args.channel_id.strip(), fallback="") if args.channel_id.strip() else ""
    explicit_persona_raw = (
        sanitize_component(args.persona.strip(), fallback="core").lower()
        if args.persona and args.persona.strip()
        else ""
    )
    message = args.message.strip()
    if not message:
        raise ValueError("message 銝?箇征")

    payload: dict[str, Any] = {
        # Keep multiple common text keys so transport-specific parsers
        # (discord/content, generic/message,text) can all resolve the turn.
        "message": message,
        "text": message,
        "content": message,
        "user_id": "cli-user",
    }
    if channel_id:
        payload["channel_id"] = channel_id

    explicit_persona: str | None
    if explicit_persona_raw:
        explicit_persona = explicit_persona_raw
    elif args.use_binding:
        explicit_persona = None
    else:
        explicit_persona = "core"

    context_override = args.context.strip()
    if context_override == "cli":
        context_override = ""
    session_override = args.session.strip()
    if session_override == "default":
        session_override = ""

    result = run_transport_event(
        vault_root=resolve_vault_root(args.vault_root),
        transport=transport,
        payload=payload,
        explicit_persona=explicit_persona,
        context_override=context_override or None,
        session_override=session_override or None,
        override_profile=args.override_profile,
        override_model=args.override_model,
        dialogue_mode=str(args.dialogue_mode).strip() or None,
        temperature=float(args.temperature),
        timeout_s=float(args.timeout),
        memory_mode=args.memory_mode,
        allow_llm_degraded=bool(args.allow_llm_degraded),
    )
    degraded = bool(result.get("degraded", False))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if bool(args.require_nondegraded) and degraded:
            return 2
        return 0

    print(result["response"])
    print("")
    print(
        f"[OK] llm={result['llm']['profile']} / {result['llm']['model']} "
        f"({result['llm']['kind']})"
    )
    if result["llm"]["fallback_failures"]:
        print(f"[OK] fallback_failures={len(result['llm']['fallback_failures'])}")
    adapter = _build_adapter(args)
    print(
        f"[OK] persona={result.get('persona', '')} "
        f"transport={result.get('transport', transport)} "
        f"channel={result.get('channel_id', channel_id) or '-'}"
    )
    mode_payload = result.get("dialogue_mode", {})
    if isinstance(mode_payload, dict):
        print(
            f"[OK] dialogue_mode={mode_payload.get('mode', '')} "
            f"(source={mode_payload.get('source', '')})"
        )
    print(f"[OK] session_memory={result['memory_paths']['session']}")
    print(f"[OK] session_uri={adapter.obsidian_uri(result['memory_paths']['session'])}")
    if result["memory_paths"]["daily"]:
        daily_path = str(result["memory_paths"]["daily"])
        print(f"[OK] daily_memory={daily_path}")
        print(f"[OK] daily_uri={adapter.obsidian_uri(daily_path)}")
    if bool(args.require_nondegraded) and degraded:
        print("[ERR] degraded response rejected by --require-nondegraded", file=sys.stderr)
        return 2
    return 0


def _derive_base_index(parent: str, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    normalized = parent.replace("\\", "/").strip("/").strip()
    if not normalized:
        return 10
    last = normalized.split("/")[-1]
    match = re.match(r"^(\d{2})_", last)
    if match:
        return int(match.group(1))
    return 10


def _compute_source_hash(adapter: ObsidianVaultAdapter, source_path: str) -> str:
    normalized = source_path.replace("\\", "/").strip().lstrip("/")
    if not normalized or normalized == "unknown":
        return "sha256:unknown"
    try:
        abs_path = adapter.absolute_path(normalized)
    except ValueError:
        return "sha256:unknown"
    if not abs_path.exists() or not abs_path.is_file():
        return "sha256:unknown"
    digest = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _cmd_allocate_folder(args: argparse.Namespace) -> int:
    adapter = _build_adapter(args)
    allocator = FolderAllocator(adapter.vault_root)
    source_hash = args.source_hash or _compute_source_hash(adapter, args.source_path)
    topic_label = args.topic_label or args.english_slug
    base_index = _derive_base_index(args.parent, args.base_index)
    decision = allocator.allocate(
        parent_relative=args.parent,
        english_slug=args.english_slug,
        zh_purpose=args.zh_purpose,
        source_path=args.source_path,
        source_hash=source_hash,
        topic_label=topic_label,
        base_index=base_index,
        reason=args.reason,
        operator=args.operator,
        override_by_user=args.override_by_user,
        dry_run=args.dry_run,
    )
    print(f"[OK] decision_id={decision.decision_id}")
    print(f"[OK] decision_type={decision.decision_type}")
    print(f"[OK] target_folder={decision.target_folder}")
    print(f"[OK] display_folder={decision.display_folder}")
    if args.dry_run:
        print("[OK] dry_run=true (no file writes)")
    else:
        print("[OK] ledger=.ai/folder_allocations.md")
    return 0


def _cmd_tool_schema(args: argparse.Namespace) -> int:
    if args.name == "all":
        print(json.dumps(tool_schema_bundle(), ensure_ascii=False, indent=2))
        return 0
    if args.name == "memory":
        print(json.dumps(memory_tool_schema(), ensure_ascii=False, indent=2))
        return 0
    if args.name == "memory_search":
        print(json.dumps(memory_search_schema(), ensure_ascii=False, indent=2))
        return 0
    if args.name == "memory_get":
        print(json.dumps(memory_get_schema(), ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"unsupported schema name: {args.name}")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
