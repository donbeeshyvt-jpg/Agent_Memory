"""SQLite + FTS search manager for markdown memories."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_memory.embedding_client import EmbeddingClient
from agent_memory.folder_labels import DIR_INFO_FILENAME, folder_display_name
from agent_memory.retrieval_routing import load_retrieval_router_config, resolve_retrieval_route
from agent_memory.security.atomic import atomic_write
from agent_memory.types import MemoryNote
from agent_memory.vault import ObsidianVaultAdapter

_SKIP_PREFIXES = (".ai/", ".obsidian/", "00_System/09_Index/")
_SKIP_FILENAMES = {
    DIR_INFO_FILENAME,
    "_SKILLS_INDEX.md",
    "_MAINTENANCE_REPORT.md",
    "TASKS.md",
    "session_compaction.md",
    "promotion_events.md",
}
_VEC_DIM = 256
_EMBED_VERSION = 2


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_relative(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/")


def _normalize_prefix(prefix: str) -> str:
    normalized = _normalize_relative(prefix)
    if normalized.endswith("/"):
        return normalized
    if normalized.endswith(".md"):
        return normalized
    return normalized + "/"


def _matches_prefix(path: str, prefix: str) -> bool:
    normalized_path = _normalize_relative(path)
    normalized_prefix = _normalize_prefix(prefix)
    if normalized_prefix.endswith(".md"):
        return normalized_path == normalized_prefix
    return normalized_path.startswith(normalized_prefix)


def _extract_title(note: MemoryNote) -> str:
    for line in note.body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return _sanitize_title(stripped.lstrip("#"), Path(note.path).stem)
    return Path(note.path).stem


def _tokenize_query(query: str) -> list[str]:
    parts = re.split(r"\s+", query.strip())
    return [p for p in parts if p]


def _extract_path_anchor_tokens(query: str) -> list[str]:
    anchors: list[str] = []
    for token in _tokenize_query(query):
        cleaned = token.strip().strip("\"'`").lower()
        if not cleaned:
            continue
        if "." in cleaned or "/" in cleaned or "_" in cleaned:
            anchors.append(cleaned)
            continue
        if cleaned.endswith(("yaml", "yml", "json", "md", "txt")) and len(cleaned) >= 5:
            anchors.append(cleaned)
    # keep order but remove duplicates
    return list(dict.fromkeys(anchors))


def _tokenize_for_embedding(text: str) -> list[str]:
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9\u4e00-\u9fff_]+", lowered)
    return [tok for tok in tokens if tok]


def _embed_text(text: str, *, dim: int = _VEC_DIM) -> list[float]:
    vec = [0.0] * dim
    for token in _tokenize_for_embedding(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vec[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vec))
    if norm <= 0:
        return vec
    return [value / norm for value in vec]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    for idx in range(len(a)):
        dot += a[idx] * b[idx]
    return max(-1.0, min(1.0, dot))


def _fts_query(query: str) -> str:
    tokens = _tokenize_query(query)
    escaped: list[str] = []
    for token in tokens:
        cleaned = token.replace('"', " ").replace("'", " ").strip()
        if cleaned:
            escaped.append(f'"{cleaned}"')
    return " AND ".join(escaped)


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload]


def _wikilink(path: str, title: str | None = None) -> str:
    no_ext = path.removesuffix(".md")
    if title and title.strip():
        return f"[[{no_ext}|{title.strip()}]]"
    return f"[[{no_ext}]]"


def _sanitize_title(raw: str, fallback: str) -> str:
    text = raw.strip() if raw else fallback
    text = text.replace("\\n", " ").replace("\n", " ")
    return " ".join(text.split()) or fallback


@dataclass(slots=True)
class SearchHit:
    """One retrieved search hit."""

    path: str
    snippet: str
    score: float
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IndexStats:
    """Index build/update counters."""

    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0


class MemorySearchManager:
    """Manage sqlite index and hybrid retrieval for markdown memories."""

    def __init__(self, adapter: ObsidianVaultAdapter, db_path: Path | None = None):
        self.adapter = adapter
        self.db_path = db_path or self.adapter.absolute_path(".ai/sqlite-index.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = True
        self._retrieval = self._load_retrieval_settings()
        self._embedding_client = EmbeddingClient(self.adapter.vault_root)
        self._provider_embedding_disabled = False
        self._embed_signature = self._build_embed_signature()
        try:
            self._init_db()
        except sqlite3.DatabaseError as exc:
            lowered = str(exc).lower()
            # R20.1 C106: 擴展 corrupt signal 白名單 (Codex 第 37 輪 R20 P2 A2 修).
            # 舊版只認 'disk i/o error' / 'database disk image is malformed' 兩條,
            # Codex 模擬「schema 損壞」拋 'malformed database schema (notes_vec) -
            # incomplete input' 沒命中白名單 → 沒走 _recover_index_db → exception
            # 往外拋. 'malformed' substring 同時抓 'database disk image is malformed' +
            # 'malformed database schema' + 'malformed disk image' 變體; 加 4 種
            # corrupt signal cover 更廣的 sqlite 損壞模式. transient lock/busy 仍 raise.
            corrupt_signals = (
                "disk i/o error",
                "malformed",
                "file is encrypted",
                "not a database",
                "incomplete input",
                "database is corrupt",
            )
            if any(s in lowered for s in corrupt_signals):
                # R20.1 C106: Force release sqlite3 fd before recovery (Windows
                # lock workaround). Windows 上 sqlite3.connect 失敗時 fd 釋放可能
                # 延遲, 讓 _recover_index_db 內 db_path.replace 撞 PermissionError.
                # gc + 短 sleep 讓 fd 進 release queue 再 recover.
                import gc as _gc_init
                import time as _time_init
                _gc_init.collect()
                _time_init.sleep(0.1)
                self._recover_index_db()
            else:
                raise

    def reindex_all(
        self,
        *,
        include_prefixes: list[str] | None = None,
        exclude_prefixes: list[str] | None = None,
    ) -> IndexStats:
        """Incrementally index all allowed markdown notes."""

        include = [_normalize_prefix(x) for x in include_prefixes or [] if x]
        exclude = [_normalize_prefix(x) for x in exclude_prefixes or [] if x]
        candidates = self._candidate_paths(include_prefixes=include, exclude_prefixes=exclude)
        stats = IndexStats()

        with self._connect() as conn:
            existing = conn.execute("SELECT path, mtime_ns FROM notes_meta").fetchall()
            existing_mtime = {str(row["path"]): int(row["mtime_ns"]) for row in existing}
            existing_vec = conn.execute("SELECT path, embed_version, embed_signature FROM notes_vec").fetchall()
            existing_vec_state = {
                str(row["path"]): (
                    int(row["embed_version"]),
                    str(row["embed_signature"] or ""),
                )
                for row in existing_vec
            }
            candidate_set = set(candidates)

            stale_paths = [path for path in existing_mtime.keys() if path not in candidate_set]
            for stale in stale_paths:
                self._remove_path_in_tx(conn, stale)
                stats.removed += 1

            for path in candidates:
                stats.scanned += 1
                try:
                    note_abs = self.adapter.absolute_path(path)
                    if not note_abs.exists() or note_abs.is_dir():
                        self._remove_path_in_tx(conn, path)
                        stats.removed += 1
                        continue

                    mtime_ns = int(note_abs.stat().st_mtime_ns)
                    vec_state = existing_vec_state.get(path, (0, ""))
                    vec_version = int(vec_state[0])
                    vec_signature = str(vec_state[1])
                    if (
                        existing_mtime.get(path) == mtime_ns
                        and vec_version == _EMBED_VERSION
                        and vec_signature == self._embed_signature
                    ):
                        stats.skipped += 1
                        continue

                    note = self.adapter.read_note(path)
                    if note is None:
                        self._remove_path_in_tx(conn, path)
                        stats.removed += 1
                        continue

                    doc = self._build_doc(note=note, mtime_ns=mtime_ns)
                    self._upsert_in_tx(conn, doc)
                    stats.indexed += 1
                except Exception:
                    stats.failed += 1

        return stats

    def index_path(self, path: str) -> bool:
        """Index or refresh one path."""

        normalized = _normalize_relative(path)
        if any(normalized.startswith(prefix) for prefix in _SKIP_PREFIXES):
            self.remove_path(normalized)
            return False
        if Path(normalized).name in _SKIP_FILENAMES:
            self.remove_path(normalized)
            return False
        with self._connect() as conn:
            try:
                note_abs = self.adapter.absolute_path(normalized)
                if not note_abs.exists() or note_abs.is_dir():
                    self._remove_path_in_tx(conn, normalized)
                    return False

                note = self.adapter.read_note(normalized)
                if note is None:
                    self._remove_path_in_tx(conn, normalized)
                    return False

                doc = self._build_doc(note=note, mtime_ns=int(note_abs.stat().st_mtime_ns))
                self._upsert_in_tx(conn, doc)
                return True
            except Exception:
                return False

    def remove_path(self, path: str) -> None:
        """Remove one note from index."""

        normalized = _normalize_relative(path)
        with self._connect() as conn:
            self._remove_path_in_tx(conn, normalized)

    def export_obsidian_views(self, *, output_dir: str = "00_System/09_Index") -> dict[str, int]:
        """Generate user-readable markdown indices for Obsidian graph browsing."""

        output_prefix = _normalize_prefix(output_dir)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT path, title, type, source, status, tags, updated
                FROM notes_meta
                ORDER BY updated DESC
                """
            ).fetchall()

        docs: list[dict[str, Any]] = []
        for row in rows:
            path = str(row["path"])
            if path.startswith(output_prefix):
                continue
            fallback_title = Path(path).stem
            docs.append(
                {
                    "path": path,
                    "title": _sanitize_title(str(row["title"] or ""), fallback_title),
                    "type": str(row["type"] or ""),
                    "source": str(row["source"] or ""),
                    "status": str(row["status"] or "active"),
                    "tags": _parse_tags(row["tags"]),
                    "updated": str(row["updated"] or ""),
                }
            )

        folder_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        tag_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in docs:
            root = doc["path"].split("/", 1)[0]
            folder_map[root].append(doc)
            for tag in doc["tags"]:
                clean_tag = str(tag).strip()
                if clean_tag:
                    tag_map[clean_tag].append(doc)

        root = self.adapter.absolute_path(output_prefix.rstrip("/"))
        root.mkdir(parents=True, exist_ok=True)

        generated_at = _utc_now_iso()
        total = len(docs)
        active = sum(1 for doc in docs if doc["status"] != "archived")
        archived = total - active

        index_md = self._render_index_md(
            generated_at=generated_at,
            total=total,
            active=active,
            archived=archived,
            folder_count=len(folder_map),
            tag_count=len(tag_map),
            output_prefix=output_prefix.rstrip("/"),
        )
        by_folder_md = self._render_by_folder_md(folder_map=folder_map, output_prefix=output_prefix.rstrip("/"))
        by_tag_md = self._render_by_tag_md(tag_map=tag_map, output_prefix=output_prefix.rstrip("/"))
        recent_md = self._render_recent_md(docs=docs[:200], output_prefix=output_prefix.rstrip("/"))
        relations_md = self._render_relations_md(
            folder_map=folder_map,
            tag_map=tag_map,
            docs=docs,
            output_prefix=output_prefix.rstrip("/"),
        )

        files = {
            "INDEX.md": index_md,
            "01_By_Folder.md": by_folder_md,
            "02_By_Tag.md": by_tag_md,
            "03_Recent_Updates.md": recent_md,
            "04_Main_Relations.md": relations_md,
        }
        for filename, content in files.items():
            atomic_write(root / filename, content)

        return {
            "total_notes": total,
            "active_notes": active,
            "archived_notes": archived,
            "folder_groups": len(folder_map),
            "tag_groups": len(tag_map),
            "files_written": len(files),
        }

    def search(
        self,
        *,
        query: str,
        max_results: int = 10,
        include_prefixes: list[str] | None = None,
        exclude_prefixes: list[str] | None = None,
        include_archived: bool = False,
        strategy: str = "",
        use_mmr: bool | None = None,
        mmr_lambda: float | None = None,
    ) -> list[SearchHit]:
        """Search indexed notes and return ranked hits with citations."""

        cleaned_query = query.strip()
        if not cleaned_query:
            return []

        search_cfg = self._retrieval.get("search", {})
        if not isinstance(search_cfg, dict):
            search_cfg = {}
        default_strategy = str(search_cfg.get("default_strategy", "hybrid")).strip().lower()
        if default_strategy not in {"hybrid", "fts", "vector"}:
            default_strategy = "hybrid"
        strategy_norm = strategy.strip().lower() if strategy.strip() else default_strategy
        if strategy_norm not in {"hybrid", "fts", "vector"}:
            strategy_norm = default_strategy

        mmr_enabled = bool(search_cfg.get("mmr_enabled", True)) if use_mmr is None else bool(use_mmr)
        try:
            mmr_lambda_value = float(search_cfg.get("mmr_lambda", 0.7)) if mmr_lambda is None else float(mmr_lambda)
        except (TypeError, ValueError):
            mmr_lambda_value = 0.7
        mmr_lambda_value = max(0.0, min(1.0, mmr_lambda_value))
        try:
            candidate_mul = int(search_cfg.get("mmr_candidate_multiplier", 4))
        except (TypeError, ValueError):
            candidate_mul = 4
        candidate_mul = max(2, candidate_mul)

        include = [_normalize_prefix(x) for x in include_prefixes or [] if x]
        exclude = [_normalize_prefix(x) for x in exclude_prefixes or [] if x]
        fetch_limit = max(max_results * (candidate_mul if mmr_enabled else 8), 64)
        query_tokens = [token.lower() for token in _tokenize_query(cleaned_query)]
        anchor_tokens = _extract_path_anchor_tokens(cleaned_query)
        query_vec: list[float] = []
        embedding_backend = "hash"
        if strategy_norm in {"hybrid", "vector"}:
            query_vec, embedding_backend = self._embed_for_query(cleaned_query)

        with self._connect() as conn:
            if strategy_norm == "vector":
                rows = self._vector_candidate_rows(conn, include_archived=include_archived, fetch_limit=fetch_limit * 6)
            else:
                rows = (
                    self._search_rows_fts(conn, cleaned_query, include_archived, fetch_limit)
                    if self._fts_enabled
                    else self._search_rows_like(conn, cleaned_query, include_archived, fetch_limit)
                )
                if strategy_norm == "hybrid" and not rows:
                    rows = self._vector_candidate_rows(conn, include_archived=include_archived, fetch_limit=fetch_limit * 6)
            if anchor_tokens:
                anchor_rows = self._path_anchor_rows(
                    conn,
                    anchor_tokens=anchor_tokens,
                    include_archived=include_archived,
                    fetch_limit=max(fetch_limit, 80),
                )
                if anchor_rows:
                    merged: dict[str, sqlite3.Row] = {}
                    for row in anchor_rows:
                        merged[str(row["path"])] = row
                    for row in rows:
                        path_key = str(row["path"])
                        if path_key not in merged:
                            merged[path_key] = row
                    rows = list(merged.values())
            vector_scores = (
                self._vector_scores_for_paths(conn, [str(row["path"]) for row in rows], query_vec)
                if query_vec
                else {}
            )
            vector_embeddings = (
                self._vectors_for_paths(conn, [str(row["path"]) for row in rows])
                if mmr_enabled and strategy_norm in {"hybrid", "vector"}
                else {}
            )

        grouped: dict[int, list[tuple[SearchHit, float, float]]] = defaultdict(list)
        for row in rows:
            path = str(row["path"])
            if not self._is_allowed_path(path, include, exclude):
                continue
            status = str(row["status"] or "")
            if not include_archived and status == "archived":
                continue

            snippet = str(row["snippet"] or "")
            if not snippet:
                snippet = str(row["title"] or path)

            raw_rank = float(row["rank"])
            fts_score = 1.0 / (1.0 + abs(raw_rank)) if strategy_norm != "vector" else 0.0
            vector_score = max(0.0, vector_scores.get(path, 0.0))
            anchor_hits = 0
            if anchor_tokens:
                lowered_path = path.lower()
                anchor_hits = sum(1 for token in anchor_tokens if token and token in lowered_path)
            anchor_bonus = min(0.9, 0.35 * float(anchor_hits))
            if strategy_norm == "fts":
                score = fts_score + anchor_bonus
            elif strategy_norm == "vector":
                score = vector_score + anchor_bonus
            else:
                score = fts_score * 0.65 + vector_score * 0.25 + anchor_bonus
            path_priority = self._path_priority(path, query_tokens)

            hit = SearchHit(
                path=path,
                snippet=snippet,
                score=score,
                source=str(row["source"] or "unknown"),
                metadata={
                    "title": str(row["title"] or ""),
                    "rank": raw_rank,
                    "status": status or "active",
                    "type": str(row["type"] or ""),
                    "tags": _parse_tags(row["tags"]),
                    "updated": str(row["updated"] or ""),
                    "obsidian_uri": self.adapter.obsidian_uri(path),
                    "strategy": strategy_norm,
                    "embedding_backend": embedding_backend,
                    "fts_score": fts_score,
                    "vector_score": vector_score,
                    "anchor_hits": anchor_hits,
                    "anchor_bonus": round(anchor_bonus, 4),
                },
            )
            grouped[path_priority].append((hit, score, raw_rank))

        ordered_hits: list[SearchHit] = []
        remaining = max(1, int(max_results))
        for priority in sorted(grouped.keys()):
            bucket = grouped[priority]
            bucket.sort(key=lambda item: (-item[1], item[2]))
            bucket_hits = [item[0] for item in bucket]
            if mmr_enabled and strategy_norm in {"hybrid", "vector"} and query_vec:
                selected = self._mmr_select(
                    hits=bucket_hits,
                    query_vec=query_vec,
                    vectors=vector_embeddings,
                    limit=remaining,
                    lambda_weight=mmr_lambda_value,
                )
            else:
                selected = bucket_hits[:remaining]
            ordered_hits.extend(selected)
            remaining = max_results - len(ordered_hits)
            if remaining <= 0:
                break

        return ordered_hits[:max_results]

    def _render_header(self, *, title: str) -> str:
        generated = _utc_now_iso()
        return (
            "---\n"
            "type: system_index\n"
            "source: agent\n"
            f"title: {title}\n"
            f"generated_at: {generated}\n"
            "schema_version: 1\n"
            "tags:\n"
            "  - index\n"
            "  - generated\n"
            "---\n\n"
        )

    def _render_index_md(
        self,
        *,
        generated_at: str,
        total: int,
        active: int,
        archived: int,
        folder_count: int,
        tag_count: int,
        output_prefix: str,
    ) -> str:
        lines = [
            self._render_header(title="記憶總索引").rstrip(),
            "# 記憶總索引",
            "",
            f"- generated_at: `{generated_at}`",
            f"- total_notes: `{total}`",
            f"- active_notes: `{active}`",
            f"- archived_notes: `{archived}`",
            f"- folder_groups: `{folder_count}`",
            f"- tag_groups: `{tag_count}`",
            "",
            "## 導覽",
            "",
            f"- [[{output_prefix}/01_By_Folder|01 By Folder]]",
            f"- [[{output_prefix}/02_By_Tag|02 By Tag]]",
            f"- [[{output_prefix}/03_Recent_Updates|03 Recent Updates]]",
            f"- [[{output_prefix}/04_Main_Relations|04 Main Relations]]",
            "",
            "## 快速入口",
            "",
            "- [[10_Permanent/MEMORY]]",
            "- [[10_Permanent/Profiles/USER]]",
            "- [[11_AI_Mirror/ingestion_logs/daily_flush]]",
            "- [[70_Active_Plans/Session_Logs]]",
            "",
        ]
        return "\n".join(lines)

    def _render_by_folder_md(self, *, folder_map: dict[str, list[dict[str, Any]]], output_prefix: str) -> str:
        lines = [
            self._render_header(title="按資料夾索引").rstrip(),
            "# 按資料夾索引",
            "",
            f"- return_to: [[{output_prefix}/INDEX|INDEX]]",
            "",
        ]
        for folder in sorted(folder_map.keys()):
            docs = sorted(folder_map[folder], key=lambda d: d["path"])
            lines.append(f"## {folder_display_name(folder)} ({len(docs)})")
            lines.append("")
            for doc in docs:
                link = _wikilink(doc["path"], doc["title"])
                lines.append(f"- {link}")
            lines.append("")
        return "\n".join(lines)

    def _render_by_tag_md(self, *, tag_map: dict[str, list[dict[str, Any]]], output_prefix: str) -> str:
        lines = [
            self._render_header(title="按標籤索引").rstrip(),
            "# 按標籤索引",
            "",
            f"- return_to: [[{output_prefix}/INDEX|INDEX]]",
            "",
        ]
        ranked_tags = sorted(tag_map.items(), key=lambda item: (-len(item[1]), item[0].lower()))
        for tag, docs in ranked_tags:
            lines.append(f"## #{tag} ({len(docs)})")
            lines.append("")
            docs_sorted = sorted(docs, key=lambda d: d["path"])
            for doc in docs_sorted:
                link = _wikilink(doc["path"], doc["title"])
                lines.append(f"- {link}")
            lines.append("")
        if not ranked_tags:
            lines.extend(["- (no tags found)", ""])
        return "\n".join(lines)

    def _render_recent_md(self, *, docs: list[dict[str, Any]], output_prefix: str) -> str:
        lines = [
            self._render_header(title="最近更新索引").rstrip(),
            "# 最近更新索引",
            "",
            f"- return_to: [[{output_prefix}/INDEX|INDEX]]",
            "",
        ]
        for doc in docs:
            link = _wikilink(doc["path"], doc["title"])
            status = doc["status"]
            updated = doc["updated"]
            lines.append(f"- {link} | status=`{status}` | updated=`{updated}`")
        if not docs:
            lines.extend(["- (no documents indexed)", ""])
        else:
            lines.append("")
        return "\n".join(lines)

    def _render_relations_md(
        self,
        *,
        folder_map: dict[str, list[dict[str, Any]]],
        tag_map: dict[str, list[dict[str, Any]]],
        docs: list[dict[str, Any]],
        output_prefix: str,
    ) -> str:
        lines = [
            self._render_header(title="主要關聯圖").rstrip(),
            "# 主要關聯圖",
            "",
            f"- return_to: [[{output_prefix}/INDEX|INDEX]]",
            "",
            "## 核心節點",
            "",
            "- [[10_Permanent/MEMORY]]",
            "- [[10_Permanent/Profiles/USER]]",
            "- [[00_System/Skills]]",
            "- [[11_AI_Mirror/ingestion_logs/daily_flush]]",
            "",
            "## 資料夾樞紐",
            "",
        ]
        ranked_folders = sorted(folder_map.items(), key=lambda item: (-len(item[1]), item[0]))[:12]
        for folder, folder_docs in ranked_folders:
            lines.append(f"### {folder_display_name(folder)} ({len(folder_docs)})")
            lines.append("")
            sample = sorted(folder_docs, key=lambda d: d["updated"], reverse=True)[:20]
            for doc in sample:
                lines.append(f"- {_wikilink(doc['path'], doc['title'])}")
            if len(folder_docs) > len(sample):
                lines.append(f"- ... {len(folder_docs) - len(sample)} more")
            lines.append("")

        lines.extend(["## 高頻標籤關聯", ""])
        ranked_tags = sorted(tag_map.items(), key=lambda item: (-len(item[1]), item[0]))[:20]
        for tag, tag_docs in ranked_tags:
            lines.append(f"### #{tag} ({len(tag_docs)})")
            lines.append("")
            sample = sorted(tag_docs, key=lambda d: d["updated"], reverse=True)[:15]
            for doc in sample:
                lines.append(f"- {_wikilink(doc['path'], doc['title'])}")
            if len(tag_docs) > len(sample):
                lines.append(f"- ... {len(tag_docs) - len(sample)} more")
            lines.append("")

        if not docs:
            lines.extend(["- (index is empty)", ""])
        return "\n".join(lines)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # Some drives (network/share/cloud sync) can reject WAL with disk I/O errors.
            try:
                conn.close()
            except Exception:
                pass
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=DELETE")
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    def _recover_index_db(self) -> None:
        # R20.1 C106: Windows file lock retry — corrupt sqlite db init 失敗時 fd
        # 釋放可能延遲, db_path.replace 撞 PermissionError [WinError 32]. 加 6 次
        # retry exponential backoff (跟 atomic.py 一致 pattern), 最後 fallback copy2+unlink
        # best-effort (不讓 backup 失敗 block recovery).
        import time as _time_recovery
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = self.db_path.with_name(f"{self.db_path.stem}.recovery-{timestamp}{self.db_path.suffix}")
        if self.db_path.exists():
            replace_succeeded = False
            last_err: OSError | None = None
            for attempt in range(6):
                try:
                    self.db_path.replace(backup)
                    replace_succeeded = True
                    break
                except OSError as exc:
                    last_err = exc
                    if attempt == 5:
                        break
                    _time_recovery.sleep(min(0.3 * (2 ** attempt), 2.0))
            if not replace_succeeded:
                # Final fallback: copy + unlink + truncate (recovery 不該因 backup 失敗 block)
                try:
                    shutil.copy2(self.db_path, backup)
                except OSError:
                    pass  # backup 失敗也繼續, recovery 主目標是重建 db
                try:
                    self.db_path.unlink(missing_ok=True)
                except OSError:
                    pass  # unlink 失敗下一步用 truncate
                # R20.1 C106 — Windows lock 末路: 若 unlink 也失敗, truncate 到 0 bytes 讓
                # sqlite3 視為 new empty db (open 'wb' 在 Windows 上比 unlink 更耐 lock).
                if self.db_path.exists():
                    for attempt in range(6):
                        try:
                            with open(self.db_path, "wb"):
                                pass  # 0-byte file
                            break
                        except OSError:
                            if attempt == 5:
                                break
                            _time_recovery.sleep(min(0.3 * (2 ** attempt), 2.0))
        for suffix in (".wal", ".shm"):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists():
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass  # sidecar cleanup best-effort
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes_meta (
                  path TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  type TEXT NOT NULL,
                  source TEXT NOT NULL,
                  status TEXT NOT NULL,
                  tags TEXT NOT NULL,
                  updated TEXT NOT NULL,
                  mtime_ns INTEGER NOT NULL,
                  indexed_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_meta_status ON notes_meta(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_meta_updated ON notes_meta(updated)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes_vec (
                  path TEXT PRIMARY KEY,
                  embedding TEXT NOT NULL,
                  updated TEXT NOT NULL,
                  embed_version INTEGER NOT NULL DEFAULT 1,
                  embed_backend TEXT NOT NULL DEFAULT 'hash',
                  embed_signature TEXT NOT NULL DEFAULT 'hash'
                )
                """
            )
            vec_columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(notes_vec)").fetchall()]
            if "embed_version" not in vec_columns:
                conn.execute("ALTER TABLE notes_vec ADD COLUMN embed_version INTEGER NOT NULL DEFAULT 1")
            if "embed_backend" not in vec_columns:
                conn.execute("ALTER TABLE notes_vec ADD COLUMN embed_backend TEXT NOT NULL DEFAULT 'hash'")
            if "embed_signature" not in vec_columns:
                conn.execute("ALTER TABLE notes_vec ADD COLUMN embed_signature TEXT NOT NULL DEFAULT 'hash'")

            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='notes_fts' AND type IN ('table', 'virtual table')"
            ).fetchone()
            if existing is not None:
                sql = str(existing["sql"] or "").upper()
                self._fts_enabled = "VIRTUAL TABLE" in sql and "FTS5" in sql
                return

            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE notes_fts USING fts5(path, title, body, tags, tokenize='unicode61')"
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notes_fts (
                      path TEXT PRIMARY KEY,
                      title TEXT NOT NULL,
                      body TEXT NOT NULL,
                      tags TEXT NOT NULL
                    )
                    """
                )
                self._fts_enabled = False

    def _load_retrieval_settings(self) -> dict[str, Any]:
        try:
            config = load_retrieval_router_config(self.adapter.vault_root)
            return resolve_retrieval_route(config, persona_id=None)
        except Exception:
            return {
                "embedding": {
                    "mode": "hash",
                    "profile": "",
                    "model": "",
                    "timeout_s": 20.0,
                },
                "search": {
                    "default_strategy": "hybrid",
                    "mmr_enabled": True,
                    "mmr_lambda": 0.7,
                    "mmr_candidate_multiplier": 4,
                },
            }

    def _build_embed_signature(self) -> str:
        embed = self._retrieval.get("embedding", {})
        if not isinstance(embed, dict):
            return "hash"
        mode = str(embed.get("mode", "hash")).strip().lower()
        if mode != "provider":
            return "hash"
        profile = str(embed.get("profile", "")).strip()
        model = str(embed.get("model", "")).strip()
        return f"provider:{profile}:{model}"

    def _embed_for_index(self, text: str) -> tuple[list[float], str]:
        embed = self._retrieval.get("embedding", {})
        if not isinstance(embed, dict):
            embed = {}
        mode = str(embed.get("mode", "hash")).strip().lower()
        if mode == "provider" and not self._provider_embedding_disabled:
            profile = str(embed.get("profile", "")).strip()
            model = str(embed.get("model", "")).strip()
            try:
                timeout_s = float(embed.get("timeout_s", 20.0))
            except (TypeError, ValueError):
                timeout_s = 20.0
            if profile and model:
                try:
                    vectors = self._embedding_client.embed_texts(
                        texts=[text],
                        profile=profile,
                        model=model,
                        timeout_s=timeout_s,
                    )
                    if vectors and isinstance(vectors[0], list):
                        return [float(x) for x in vectors[0]], "provider"
                except Exception:
                    self._provider_embedding_disabled = True
        return _embed_text(text), "hash"

    def _embed_for_query(self, text: str) -> tuple[list[float], str]:
        return self._embed_for_index(text)

    def _candidate_paths(
        self,
        *,
        include_prefixes: list[str],
        exclude_prefixes: list[str],
    ) -> list[str]:
        paths: list[str] = []
        for note_path in self.adapter.vault_root.rglob("*.md"):
            if not note_path.is_file():
                continue
            relative = str(note_path.relative_to(self.adapter.vault_root)).replace("\\", "/")
            if any(relative.startswith(prefix) for prefix in _SKIP_PREFIXES):
                continue
            if note_path.name in _SKIP_FILENAMES:
                continue
            if include_prefixes and not any(_matches_prefix(relative, pref) for pref in include_prefixes):
                continue
            if any(_matches_prefix(relative, pref) for pref in exclude_prefixes):
                continue
            paths.append(relative)
        paths.sort()
        return paths

    def _build_doc(self, *, note: MemoryNote, mtime_ns: int) -> dict[str, Any]:
        embed_source = f"{_extract_title(note)}\n{note.body[:12000]}"
        vector, backend = self._embed_for_index(embed_source)
        return {
            "path": note.path,
            "title": _extract_title(note),
            "body": note.body,
            "type": note.frontmatter.type.value,
            "source": note.frontmatter.source.value,
            "status": note.frontmatter.status,
            "tags": json.dumps(note.frontmatter.tags, ensure_ascii=False),
            "vector_embedding": json.dumps(vector, ensure_ascii=False),
            "vector_backend": backend,
            "vector_signature": self._embed_signature,
            "updated": note.frontmatter.updated.isoformat(),
            "mtime_ns": int(mtime_ns),
            "indexed_at": _utc_now_iso(),
        }

    def _upsert_in_tx(self, conn: sqlite3.Connection, doc: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO notes_meta(path, title, type, source, status, tags, updated, mtime_ns, indexed_at)
            VALUES(:path, :title, :type, :source, :status, :tags, :updated, :mtime_ns, :indexed_at)
            ON CONFLICT(path) DO UPDATE SET
              title=excluded.title,
              type=excluded.type,
              source=excluded.source,
              status=excluded.status,
              tags=excluded.tags,
              updated=excluded.updated,
              mtime_ns=excluded.mtime_ns,
              indexed_at=excluded.indexed_at
            """,
            doc,
        )

        if self._fts_enabled:
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (doc["path"],))
            conn.execute(
                "INSERT INTO notes_fts(path, title, body, tags) VALUES(?, ?, ?, ?)",
                (doc["path"], doc["title"], doc["body"], doc["tags"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO notes_fts(path, title, body, tags)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  title=excluded.title,
                  body=excluded.body,
                  tags=excluded.tags
                """,
                (doc["path"], doc["title"], doc["body"], doc["tags"]),
            )
        conn.execute(
            """
            INSERT INTO notes_vec(path, embedding, updated, embed_version, embed_backend, embed_signature)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              embedding=excluded.embedding,
              updated=excluded.updated,
              embed_version=excluded.embed_version,
              embed_backend=excluded.embed_backend,
              embed_signature=excluded.embed_signature
            """,
            (
                doc["path"],
                doc["vector_embedding"],
                doc["updated"],
                _EMBED_VERSION,
                doc["vector_backend"],
                doc["vector_signature"],
            ),
        )

    def _remove_path_in_tx(self, conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM notes_meta WHERE path = ?", (path,))
        conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
        conn.execute("DELETE FROM notes_vec WHERE path = ?", (path,))

    def _search_rows_fts(
        self,
        conn: sqlite3.Connection,
        query: str,
        include_archived: bool,
        fetch_limit: int,
    ) -> list[sqlite3.Row]:
        compiled = _fts_query(query)
        if not compiled:
            return []
        return conn.execute(
            """
            SELECT
              notes_meta.path AS path,
              notes_meta.title AS title,
              snippet(notes_fts, 2, '[', ']', ' ... ', 24) AS snippet,
              bm25(notes_fts) AS rank,
              notes_meta.updated AS updated,
              notes_meta.status AS status,
              notes_meta.tags AS tags,
              notes_meta.type AS type,
              notes_meta.source AS source
            FROM notes_fts
            JOIN notes_meta ON notes_meta.path = notes_fts.path
            WHERE notes_fts MATCH ?
              AND (? = 1 OR notes_meta.status != 'archived')
            ORDER BY bm25(notes_fts) ASC
            LIMIT ?
            """,
            (compiled, 1 if include_archived else 0, fetch_limit),
        ).fetchall()

    def _search_rows_like(
        self,
        conn: sqlite3.Connection,
        query: str,
        include_archived: bool,
        fetch_limit: int,
    ) -> list[sqlite3.Row]:
        token = f"%{query.lower()}%"
        return conn.execute(
            """
            SELECT
              notes_meta.path AS path,
              notes_meta.title AS title,
              substr(notes_fts.body, 1, 240) AS snippet,
              999.0 AS rank,
              notes_meta.updated AS updated,
              notes_meta.status AS status,
              notes_meta.tags AS tags,
              notes_meta.type AS type,
              notes_meta.source AS source
            FROM notes_fts
            JOIN notes_meta ON notes_meta.path = notes_fts.path
            WHERE (
              lower(notes_meta.path) LIKE ?
              OR lower(notes_fts.title) LIKE ?
              OR lower(notes_fts.body) LIKE ?
              OR lower(notes_fts.tags) LIKE ?
            )
              AND (? = 1 OR notes_meta.status != 'archived')
            ORDER BY notes_meta.updated DESC
            LIMIT ?
            """,
            (token, token, token, token, 1 if include_archived else 0, fetch_limit),
        ).fetchall()

    def _vector_candidate_rows(
        self,
        conn: sqlite3.Connection,
        *,
        include_archived: bool,
        fetch_limit: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT
              notes_meta.path AS path,
              notes_meta.title AS title,
              notes_meta.title AS snippet,
              999.0 AS rank,
              notes_meta.updated AS updated,
              notes_meta.status AS status,
              notes_meta.tags AS tags,
              notes_meta.type AS type,
              notes_meta.source AS source
            FROM notes_meta
            WHERE (? = 1 OR notes_meta.status != 'archived')
            ORDER BY notes_meta.updated DESC
            LIMIT ?
            """,
            (1 if include_archived else 0, max(64, int(fetch_limit))),
        ).fetchall()

    def _path_anchor_rows(
        self,
        conn: sqlite3.Connection,
        *,
        anchor_tokens: list[str],
        include_archived: bool,
        fetch_limit: int,
    ) -> list[sqlite3.Row]:
        tokens = [tok.strip().lower() for tok in anchor_tokens if tok and tok.strip()]
        if not tokens:
            return []
        where_parts = ["lower(notes_meta.path) LIKE ?" for _ in tokens]
        sql = (
            """
            SELECT
              notes_meta.path AS path,
              notes_meta.title AS title,
              notes_meta.title AS snippet,
              0.0 AS rank,
              notes_meta.updated AS updated,
              notes_meta.status AS status,
              notes_meta.tags AS tags,
              notes_meta.type AS type,
              notes_meta.source AS source
            FROM notes_meta
            WHERE (
            """
            + " OR ".join(where_parts)
            + """
            )
              AND (? = 1 OR notes_meta.status != 'archived')
            ORDER BY notes_meta.updated DESC
            LIMIT ?
            """
        )
        params: list[Any] = [f"%{tok}%" for tok in tokens]
        params.append(1 if include_archived else 0)
        params.append(max(16, int(fetch_limit)))
        return conn.execute(sql, params).fetchall()

    def _vector_scores_for_paths(
        self,
        conn: sqlite3.Connection,
        paths: list[str],
        query_vec: list[float],
    ) -> dict[str, float]:
        if not paths or not query_vec:
            return {}
        unique_paths = list(dict.fromkeys(paths))
        placeholders = ",".join(["?"] * len(unique_paths))
        rows = conn.execute(
            f"SELECT path, embedding FROM notes_vec WHERE path IN ({placeholders})",
            unique_paths,
        ).fetchall()
        scores: dict[str, float] = {}
        for row in rows:
            path = str(row["path"])
            raw_embedding = str(row["embedding"] or "[]")
            try:
                embedding = json.loads(raw_embedding)
            except json.JSONDecodeError:
                continue
            if not isinstance(embedding, list):
                continue
            try:
                vec = [float(x) for x in embedding]
            except (TypeError, ValueError):
                continue
            if len(vec) != len(query_vec):
                continue
            scores[path] = _cosine_similarity(query_vec, vec)
        return scores

    def _vectors_for_paths(self, conn: sqlite3.Connection, paths: list[str]) -> dict[str, list[float]]:
        if not paths:
            return {}
        unique_paths = list(dict.fromkeys(paths))
        placeholders = ",".join(["?"] * len(unique_paths))
        rows = conn.execute(
            f"SELECT path, embedding FROM notes_vec WHERE path IN ({placeholders})",
            unique_paths,
        ).fetchall()
        vectors: dict[str, list[float]] = {}
        for row in rows:
            path = str(row["path"])
            raw_embedding = str(row["embedding"] or "[]")
            try:
                embedding = json.loads(raw_embedding)
            except json.JSONDecodeError:
                continue
            if not isinstance(embedding, list):
                continue
            try:
                vec = [float(x) for x in embedding]
            except (TypeError, ValueError):
                continue
            vectors[path] = vec
        return vectors

    def _mmr_select(
        self,
        *,
        hits: list[SearchHit],
        query_vec: list[float],
        vectors: dict[str, list[float]],
        limit: int,
        lambda_weight: float,
    ) -> list[SearchHit]:
        if limit <= 0 or not hits:
            return []
        if len(hits) <= limit:
            return hits

        selected: list[SearchHit] = []
        remaining = list(hits)
        while remaining and len(selected) < limit:
            if not selected:
                first = max(
                    remaining,
                    key=lambda item: float(item.metadata.get("vector_score", item.score)),
                )
                selected.append(first)
                remaining.remove(first)
                continue

            best_item: SearchHit | None = None
            best_score = -10_000.0
            for cand in remaining:
                query_sim = float(cand.metadata.get("vector_score", cand.score))
                cand_vec = vectors.get(cand.path, [])
                max_sim_selected = 0.0
                if cand_vec:
                    for picked in selected:
                        picked_vec = vectors.get(picked.path, [])
                        if len(cand_vec) != len(picked_vec) or not picked_vec:
                            continue
                        sim = _cosine_similarity(cand_vec, picked_vec)
                        if sim > max_sim_selected:
                            max_sim_selected = sim
                mmr_score = lambda_weight * query_sim - (1.0 - lambda_weight) * max_sim_selected
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_item = cand
            if best_item is None:
                break
            selected.append(best_item)
            remaining.remove(best_item)
        return selected

    def _path_priority(self, path: str, query_tokens: list[str]) -> int:
        if not query_tokens:
            return 1
        lowered = path.lower()
        return 0 if any(token in lowered for token in query_tokens) else 1

    def _is_allowed_path(self, path: str, include: list[str], exclude: list[str]) -> bool:
        normalized = _normalize_relative(path)
        if include and not any(_matches_prefix(normalized, pref) for pref in include):
            return False
        if any(_matches_prefix(normalized, pref) for pref in exclude):
            return False
        return True
