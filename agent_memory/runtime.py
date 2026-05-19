"""Memory runtime and tool-facing operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from agent_memory.memory_promotion import record_recall_hits
from agent_memory.provider import BuiltinMemoryProvider, MemoryProvider
from agent_memory.search import IndexStats, MemorySearchManager, SearchHit
from agent_memory.types import MemoryNote, MemorySource
from agent_memory.vault import ObsidianVaultAdapter

WriteHook = Callable[["MemoryWriteEvent"], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_relative(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/")


@dataclass(slots=True)
class RuntimeProfile:
    """Route/profile boundary for runtime reads and writes."""

    name: str = "default"
    read_include: list[str] = field(
        default_factory=lambda: [
            "00_System/",
            "10_Permanent/",
            "11_AI_Mirror/",
            "20_Literature/",
            "30_Programming/",
            "40_Gaming/",
            "50_Media/",
            "60_Other_Domains/",
            "70_Active_Plans/",
            "80_Fleeting/",
            "90_Daily_Journal/",
        ]
    )
    read_exclude: list[str] = field(default_factory=list)
    write_allow: list[str] = field(
        default_factory=lambda: [
            "00_System/Skills/",
            "10_Permanent/",
            "11_AI_Mirror/",
            "70_Active_Plans/",
        ]
    )
    write_deny: list[str] = field(
        default_factory=lambda: [
            "20_Literature/",
            "80_Fleeting/",
            "90_Daily_Journal/",
        ]
    )

    def can_read(self, path: str) -> bool:
        normalized = _normalize_relative(path)
        if not any(normalized.startswith(prefix) for prefix in self.read_include):
            return False
        if any(normalized.startswith(prefix) for prefix in self.read_exclude):
            return False
        return True

    def can_write(self, path: str) -> bool:
        normalized = _normalize_relative(path)
        if any(normalized.startswith(prefix) for prefix in self.write_deny):
            return False
        if not any(normalized.startswith(prefix) for prefix in self.write_allow):
            return False
        return True


@dataclass(slots=True)
class MemoryWriteEvent:
    """Event emitted after add/replace/remove write operations."""

    action: str
    path: str
    timestamp: str
    agent: str
    reason: str = ""
    before_status: str | None = None
    after_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryCitation:
    """Obsidian citation payload for one note."""

    path: str
    obsidian_uri: str
    type: str
    source: str
    status: str
    updated: str
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemoryOperationResult:
    """Standard runtime operation result payload."""

    ok: bool
    action: str
    path: str
    note: MemoryNote | None = None
    message: str = ""


@dataclass(slots=True)
class MemoryGetResult:
    """Read result with citation metadata."""

    ok: bool
    path: str
    note: MemoryNote | None = None
    citation: MemoryCitation | None = None
    message: str = ""


class MemoryRuntime:
    """Runtime orchestrator for memory providers and policy checks."""

    def __init__(
        self,
        adapter: ObsidianVaultAdapter,
        *,
        provider: MemoryProvider | None = None,
        profile: RuntimeProfile | None = None,
        search_manager: MemorySearchManager | None = None,
        sync_user_views: bool = True,
    ):
        self.adapter = adapter
        self.provider = provider or BuiltinMemoryProvider(adapter)
        self.profile = profile or RuntimeProfile()
        self.search_manager = search_manager or MemorySearchManager(adapter)
        self.sync_user_views = sync_user_views
        self._hooks: list[WriteHook] = []

    def register_write_hook(self, callback: WriteHook) -> None:
        self._hooks.append(callback)

    def apply_memory_tool(
        self,
        *,
        action: str,
        path: str,
        content: str | None = None,
        reason: str = "",
        agent: str = "agent-memory-core",
        source: MemorySource = MemorySource.AGENT,
        tags: list[str] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> MemoryOperationResult:
        normalized = _normalize_relative(path)
        before = self.provider.get_memory(path=normalized)

        if action == "get":
            if not self.profile.can_read(normalized):
                raise PermissionError(f"讀取越界：{normalized}")
            note = self.provider.get_memory(path=normalized)
            return MemoryOperationResult(ok=note is not None, action=action, path=normalized, note=note, message="ok")

        if not self.profile.can_write(normalized):
            raise PermissionError(f"寫入越界或唯讀路徑：{normalized}")

        if action == "add":
            if content is None or not content.strip():
                raise ValueError("add 需要 content")
            note = self.provider.add_memory(
                path=normalized,
                content=content,
                agent=agent,
                source=source,
                tags=tags,
                extras=extras,
            )
            self.search_manager.index_path(normalized)
            self._sync_user_views()
            self._emit_write_event(
                MemoryWriteEvent(
                    action="add",
                    path=normalized,
                    timestamp=_now_iso(),
                    agent=agent,
                    reason=reason,
                    before_status=before.frontmatter.status if before else None,
                    after_status=note.frontmatter.status if note else None,
                    metadata={"provider": self.provider.name},
                )
            )
            return MemoryOperationResult(ok=True, action=action, path=normalized, note=note, message="added")

        if action == "replace":
            if content is None or not content.strip():
                raise ValueError("replace 需要 content")
            note = self.provider.replace_memory(
                path=normalized,
                content=content,
                agent=agent,
                source=source,
                tags=tags,
                extras=extras,
            )
            self.search_manager.index_path(normalized)
            self._sync_user_views()
            self._emit_write_event(
                MemoryWriteEvent(
                    action="replace",
                    path=normalized,
                    timestamp=_now_iso(),
                    agent=agent,
                    reason=reason,
                    before_status=before.frontmatter.status if before else None,
                    after_status=note.frontmatter.status if note else None,
                    metadata={"provider": self.provider.name},
                )
            )
            return MemoryOperationResult(ok=True, action=action, path=normalized, note=note, message="replaced")

        if action == "remove":
            note = self.provider.remove_memory(path=normalized, reason=reason, agent=agent)
            if note is None:
                self.search_manager.remove_path(normalized)
            else:
                self.search_manager.index_path(normalized)
            self._sync_user_views()
            # Unified spec: remove must also trigger on_memory_write hooks.
            self._emit_write_event(
                MemoryWriteEvent(
                    action="remove",
                    path=normalized,
                    timestamp=_now_iso(),
                    agent=agent,
                    reason=reason,
                    before_status=before.frontmatter.status if before else None,
                    after_status=note.frontmatter.status if note else None,
                    metadata={"provider": self.provider.name},
                )
            )
            return MemoryOperationResult(
                ok=note is not None,
                action=action,
                path=normalized,
                note=note,
                message="removed" if note else "not_found",
            )

        raise ValueError(f"未知 memory action：{action}")

    def memory_get(self, *, path: str) -> MemoryGetResult:
        """Read one note with citation payload."""

        normalized = _normalize_relative(path)
        if not self.profile.can_read(normalized):
            raise PermissionError(f"讀取越界：{normalized}")

        note = self.provider.get_memory(path=normalized)
        if note is None:
            return MemoryGetResult(ok=False, path=normalized, note=None, citation=None, message="not_found")

        citation = MemoryCitation(
            path=normalized,
            obsidian_uri=self.adapter.obsidian_uri(normalized),
            type=note.frontmatter.type.value,
            source=note.frontmatter.source.value,
            status=note.frontmatter.status,
            updated=note.frontmatter.updated.isoformat(),
            tags=note.frontmatter.tags,
        )
        return MemoryGetResult(ok=True, path=normalized, note=note, citation=citation, message="ok")

    def reindex_search(
        self,
        *,
        include_prefixes: list[str] | None = None,
        exclude_prefixes: list[str] | None = None,
        sync_views: bool = True,
    ) -> IndexStats:
        """Incrementally rebuild sqlite index under current profile scope."""

        stats = self.search_manager.reindex_all(
            include_prefixes=include_prefixes or self.profile.read_include,
            exclude_prefixes=exclude_prefixes or self.profile.read_exclude,
        )
        if sync_views:
            self._sync_user_views()
        return stats

    def memory_search(
        self,
        *,
        query: str,
        max_results: int = 10,
        include_archived: bool = False,
        auto_reindex: bool = True,
        strategy: str = "hybrid",
        use_mmr: bool | None = None,
        mmr_lambda: float | None = None,
        min_score: float = 0.1,
    ) -> list[SearchHit]:
        """Run scoped retrieval with path-first filtering.

        R14 C53 修補:
        - raw zones (20/80/90) hardcoded exclude — 即使 profile.read_include 含, retrieval 也不該命中
          (使用者私人區, AI 不該透過 RAG 引用; 但單檔直接讀仍 OK 由 files.read_file 另外控制)
        - min_score 門檻 (預設 0.1) — 低於門檻 hits 不回, 避免 no-hit 查詢仍回 top-k 灌爆 prompt
        """

        # R14 C53 T6.3: 永遠把 raw zones 從 retrieval 排除 (避免 RAG 污染 prompt)
        retrieval_exclude = list(self.profile.read_exclude or [])
        _RAW_ZONES_EXCLUDE = ("20_Literature/", "80_Fleeting/", "90_Daily_Journal/")
        for prefix in _RAW_ZONES_EXCLUDE:
            if prefix not in retrieval_exclude:
                retrieval_exclude.append(prefix)

        if auto_reindex:
            self.reindex_search(sync_views=False)
        hits = self.search_manager.search(
            query=query,
            max_results=max_results,
            include_prefixes=self.profile.read_include,
            exclude_prefixes=retrieval_exclude,
            include_archived=include_archived,
            strategy=strategy,
            use_mmr=use_mmr,
            mmr_lambda=mmr_lambda,
        )
        # R14 C53 T6.4: min_score 門檻 — 過濾雜訊 hit
        if min_score > 0 and hits:
            hits = [h for h in hits if float(getattr(h, "score", 0.0)) >= min_score]
        if hits:
            try:
                record_recall_hits(self.adapter.vault_root, query=query, hits=hits, phase="light")
            except Exception:
                # recall tracker should not block retrieval
                pass
        return hits

    def sync_user_index_views(self, *, output_dir: str = "00_System/09_Index") -> dict[str, int]:
        """Build user-facing Obsidian index pages from sqlite metadata."""

        return self.search_manager.export_obsidian_views(output_dir=output_dir)

    def frozen_snapshot(
        self,
        *,
        user_profile_path: str = "10_Permanent/Profiles/USER.md",
        memory_path: str = "10_Permanent/MEMORY.md",
    ) -> str:
        """Build frozen snapshot block for system prompt injection."""

        user = self.memory_get(path=user_profile_path).note
        mem = self.memory_get(path=memory_path).note

        user_body = user.body.strip() if user else "(missing)"
        mem_body = mem.body.strip() if mem else "(missing)"
        return (
            "<USER_PROFILE_SNAPSHOT>\n"
            f"{user_body}\n"
            "</USER_PROFILE_SNAPSHOT>\n\n"
            "<AGENT_MEMORY_SNAPSHOT>\n"
            f"{mem_body}\n"
            "</AGENT_MEMORY_SNAPSHOT>\n"
        )

    def _emit_write_event(self, event: MemoryWriteEvent) -> None:
        for hook in self._hooks:
            hook(event)

    def _sync_user_views(self) -> None:
        if not self.sync_user_views:
            return
        try:
            self.sync_user_index_views()
        except Exception:
            # Non-critical: user index views should not block memory writes.
            return
