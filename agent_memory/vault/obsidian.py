"""Concrete Obsidian vault adapter for Agent Memory."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import yaml

from agent_memory.folder_labels import (
    canonical_dir_info_targets,
    ensure_dir_info_file,
)
from agent_memory.channel_bindings import ensure_channel_bindings_file
from agent_memory.llm_routing import ensure_llm_router_file
from agent_memory.persona_governance import ensure_persona_governance_file
from agent_memory.retrieval_routing import ensure_retrieval_router_file
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.security.scanner import scan_memory_content
from agent_memory.transport_profiles import ensure_transport_profiles_file
from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault.adapter import VaultAdapter

_READONLY_PREFIXES = ("20_Literature/", "80_Fleeting/", "90_Daily_Journal/")
_BRAIN_MANIFEST_RELATIVE_PATH = "00_System/08_Runtime_Profiles/brain_manifest.yaml"
_PROFILE_REGISTRY_RELATIVE_PATH = "00_System/08_Runtime_Profiles/registry.yaml"
_CORE_PERSONA_RELATIVE_PATH = "00_System/08_Runtime_Profiles/personas/core.md"
_CORE_ROUTE_RELATIVE_PATH = "00_System/08_Runtime_Profiles/routes/core.yaml"
_START_GUIDE_RELATIVE_PATH = "00_System/08_Runtime_Profiles/START_HERE.md"

_LAYER_TO_DIR = {
    MemoryType.USER_PROFILE: "10_Permanent/Profiles",
    MemoryType.LONG_TERM: "10_Permanent",
    MemoryType.SHORT_TERM: "11_AI_Mirror/ingestion_logs/daily_flush",
    MemoryType.SKILL: "00_System/Skills",
    MemoryType.SESSION: "70_Active_Plans/Session_Logs",
    MemoryType.CONCEPT: "10_Permanent/Concepts",
}

_SKELETON_DIRS = (
    "00_System",
    "00_System/08_Runtime_Profiles",
    "00_System/08_Runtime_Profiles/personas",
    "00_System/08_Runtime_Profiles/routes",
    "00_System/08_Runtime_Profiles/proposals",
    "00_System/09_Index",
    "00_System/Skills",
    "00_System/Skills/_Persona",
    "10_Permanent",
    "10_Permanent/Profiles",
    "10_Permanent/Facts",
    "10_Permanent/Concepts",
    "11_AI_Mirror",
    "11_AI_Mirror/90_to_80",
    "11_AI_Mirror/external_ingest",
    "11_AI_Mirror/external_ingest/notion_queue",
    "11_AI_Mirror/external_ingest/web_research",
    "11_AI_Mirror/template_normalized",
    "11_AI_Mirror/internalised_candidates",
    "11_AI_Mirror/ingestion_logs",
    "11_AI_Mirror/ingestion_logs/daily_flush",
    "20_Literature",
    "30_Programming",
    "40_Gaming",
    "50_Media",
    "60_Other_Domains",
    "70_Active_Plans",
    "70_Active_Plans/Task_Board",
    "70_Active_Plans/Session_Logs",
    "80_Fleeting",
    "90_Daily_Journal",
    "99_Archive",
    ".ai",
    ".obsidian",
)

_KEY_RE = re.compile(r"[^a-zA-Z0-9._/-]+")
_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: str | None, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    text = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return fallback


def _normalize_key(key: str) -> str:
    cleaned = _KEY_RE.sub("-", key.strip())
    cleaned = cleaned.replace("--", "-").strip("-")
    return cleaned or "untitled"


def _normalize_id(raw: str, *, fallback: str) -> str:
    cleaned = _ID_RE.sub("-", raw.strip()).strip("-").lower()
    return cleaned or fallback


def _yaml_text(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip() + "\n"


def _normalize_relative(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def _is_readonly_raw_path(path: str) -> bool:
    normalized = _normalize_relative(path)
    return any(normalized.startswith(prefix) for prefix in _READONLY_PREFIXES)


class ObsidianVaultAdapter(VaultAdapter):
    """Read/write markdown notes in Obsidian vault format."""

    def __init__(self, vault_root: Path):
        self._root = Path(vault_root).expanduser().resolve()

    @property
    def vault_root(self) -> Path:
        return self._root

    def ensure_skeleton(self) -> None:
        for rel in _SKELETON_DIRS:
            (self._root / rel).mkdir(parents=True, exist_ok=True)

        self._bootstrap_defaults()

    def _bootstrap_defaults(self) -> None:
        ai_dir = self._root / ".ai"

        ledger = ai_dir / "ingestion_ledger.json"
        if not ledger.exists():
            atomic_write(
                ledger,
                '{\n  "schema_version": 1,\n  "jobs": []\n}\n',
            )

        alloc = ai_dir / "folder_allocations.md"
        if not alloc.exists():
            atomic_write(
                alloc,
                "# Folder Allocation Ledger\n\n"
                "- ?桃?嚗???AI ?芸???鞈?憭曇?摨?瘙箇??n"
                "- 閬?嚗?閮撣喋?撖急?獢頂蝯晞n"
                "- 閬?嚗??冗憿舐內?澆??箏???`NN_EnglishSlug / 蝜葉?券牧??n",
            )

        if self.read_note("10_Permanent/Profiles/USER.md") is None:
            note = MemoryNote(
                path="10_Permanent/Profiles/USER.md",
                frontmatter=Frontmatter(
                    type=MemoryType.USER_PROFILE,
                    source=MemorySource.USER,
                    tags=["profile", "baseline"],
                ),
                body="# USER\n\n## ?末\n\n- 嚗?鋆?\n",
            )
            self.write_note(note)

        if self.read_note("10_Permanent/MEMORY.md") is None:
            note = MemoryNote(
                path="10_Permanent/MEMORY.md",
                frontmatter=Frontmatter(
                    type=MemoryType.LONG_TERM,
                    source=MemorySource.AGENT,
                    tags=["memory", "baseline"],
                ),
                body="# MEMORY\n\n## 蝛拙?閮\n\n- 嚗?鋆?\n",
            )
            self.write_note(note)

        system_readme = self._root / "00_System" / "README.md"
        if not system_readme.exists():
            atomic_write(
                system_readme,
                "# 00_System\n\n甇文?靽?蝟餌絞閬??untime 閮剖??kills ?犖?潸楝?晞n",
            )

        index_readme = self._root / "00_System" / "09_Index" / "README.md"
        if not index_readme.exists():
            atomic_write(
                index_readme,
                "# 09_Index\n\n甇文???AI ?芸??Ｙ??犖憿霈蝝Ｗ?閬?嚗??? FTS ??嚗n",
            )

        ensure_llm_router_file(self._root, overwrite=False)
        ensure_retrieval_router_file(self._root, overwrite=False)
        ensure_channel_bindings_file(self._root, overwrite=False)
        ensure_transport_profiles_file(self._root, overwrite=False)
        ensure_persona_governance_file(self._root, overwrite=False)
        self.ensure_runtime_profile_scaffold(overwrite=False)
        self.ensure_brain_manifest(owner_id="owner", brain_id=None, overwrite=False)
        self.ensure_brain_scope_doc(overwrite=False)
        start_guide = self.absolute_path(_START_GUIDE_RELATIVE_PATH)
        if not start_guide.exists():
            atomic_write(
                start_guide,
                "# Start Here\n\n"
                "## First Run Checklist\n\n"
                "1. Confirm LLM route with `llm-show --persona core`.\n"
                "2. Bootstrap first operator persona (`steward`) for setup and maintenance.\n"
                "3. Create personas with tool policy:\n"
                "   - Tools ON: `persona-create --display-name <name> --mission <text> --enable-tools --auto-approve`\n"
                "   - Tools OFF: `persona-create --display-name <name> --mission <text> --disable-tools --auto-approve`\n"
                "4. Update tool access later:\n"
                "   - `persona-update --persona <id> --enable-tools`\n"
                "   - `persona-update --persona <id> --disable-tools`\n\n"
                "## Notes\n\n"
                "- Supervision and capabilities are stored in `persona_governance.yaml`.\n"
                "- Personas keep independent memory and skills while sharing one second-brain namespace.\n",
            )

        for folder_rel, purpose in canonical_dir_info_targets().items():
            ensure_dir_info_file(self._root, folder_rel, zh_purpose=purpose, overwrite=False)

    def ensure_runtime_profile_scaffold(self, *, overwrite: bool = False) -> dict[str, Path]:
        """Ensure minimal persona/route registry exists for profile isolation."""

        registry_abs = self.absolute_path(_PROFILE_REGISTRY_RELATIVE_PATH)
        persona_abs = self.absolute_path(_CORE_PERSONA_RELATIVE_PATH)
        route_abs = self.absolute_path(_CORE_ROUTE_RELATIVE_PATH)

        registry_abs.parent.mkdir(parents=True, exist_ok=True)
        persona_abs.parent.mkdir(parents=True, exist_ok=True)
        route_abs.parent.mkdir(parents=True, exist_ok=True)

        now = _iso(_now())
        registry_payload = {
            "schema_version": 1,
            "default_persona": "core",
            "personas": {
                "core": {
                    "persona_path": "personas/core.md",
                    "route_path": "routes/core.yaml",
                    "status": "active",
                }
            },
            "updated_at": now,
        }
        if overwrite or not registry_abs.exists():
            atomic_write(registry_abs, _yaml_text(registry_payload))

        persona_md = (
            "---\n"
            "type: system\n"
            "persona_id: core\n"
            "display_name: Core\n"
            "mission: 蝬剜?蝚砌?憭扯銝?湔扯?摰??\n"
            "style: concise\n"
            "language: zh-Hant\n"
            "schema_version: 1\n"
            "status: active\n"
            "---\n\n"
            "# Persona: Core\n\n"
            "- ?芸?蝬剜? 00~99 蝚砌?憭扯?賢???瑽??氬n"
            "- 銝?乩耨??20/80/90 ????n"
            "- ???∪??撣喟雁?餈賣滲?批?瘚??n"
        )
        if overwrite or not persona_abs.exists():
            atomic_write(persona_abs, persona_md)

        route_payload = {
            "schema_version": 1,
            "persona_id": "core",
            "default_mode": "core",
            "memory_scope": {
                "include": [
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
                    "99_Archive/",
                ],
                "exclude": [],
            },
            "write_scope": {
                "allow": [
                    "00_System/Skills/",
                    "10_Permanent/",
                    "11_AI_Mirror/",
                    "70_Active_Plans/",
                ],
                "deny": [
                    "20_Literature/",
                    "80_Fleeting/",
                    "90_Daily_Journal/",
                ],
            },
            "guardrails": {
                "path_priority_over_metadata": True,
                "immutable_sources": [
                    "20_Literature/",
                    "80_Fleeting/",
                    "90_Daily_Journal/",
                ],
            },
            "updated_at": now,
        }
        if overwrite or not route_abs.exists():
            atomic_write(route_abs, _yaml_text(route_payload))

        return {
            "registry": registry_abs,
            "core_persona": persona_abs,
            "core_route": route_abs,
        }

    def ensure_brain_manifest(
        self,
        *,
        owner_id: str = "owner",
        brain_id: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Ensure one manifest exists for this brain instance."""

        manifest_abs = self.absolute_path(_BRAIN_MANIFEST_RELATIVE_PATH)
        manifest_abs.parent.mkdir(parents=True, exist_ok=True)
        if manifest_abs.exists() and not overwrite:
            return manifest_abs

        now = _iso(_now())
        resolved_owner = _normalize_id(owner_id, fallback="owner")
        resolved_brain = _normalize_id(
            brain_id or f"brain-{_normalize_id(self._root.name, fallback='vault')}-{uuid.uuid4().hex[:8]}",
            fallback="brain-default",
        )
        payload = {
            "schema_version": 1,
            "brain_id": resolved_brain,
            "owner_id": resolved_owner,
            "vault_name": self._root.name,
            "namespace": {
                "range": "00-99",
                "is_second_brain_root": True,
                "rule": "00~99 namespace belongs to one second-brain instance.",
            },
            "core_paths": {
                "system": "00_System/",
                "permanent": "10_Permanent/",
                "ai_mirror": "11_AI_Mirror/",
                "session_logs": "70_Active_Plans/Session_Logs/",
            },
            "governance": {
                "raw_readonly_zones": [
                    "20_Literature/",
                    "80_Fleeting/",
                    "90_Daily_Journal/",
                ],
                "portable_repoint_enabled": True,
            },
            "created_at": now,
            "updated_at": now,
        }
        atomic_write(manifest_abs, _yaml_text(payload))
        return manifest_abs

    def ensure_brain_scope_doc(self, *, overwrite: bool = False) -> Path:
        """Ensure human-readable namespace statement exists."""

        scope_abs = self.absolute_path("00_System/00_Brain_Scope.md")
        if scope_abs.exists() and not overwrite:
            return scope_abs
        content = (
            "# 00~99 Second-Brain Namespace\n\n"
            "- This vault uses `00~99` as one complete second-brain namespace.\n"
            "- `00_System` stores runtime profiles and governance settings.\n"
            "- Repointing to another vault must regenerate `brain_manifest.yaml` to get new `owner_id`/`brain_id`.\n"
            "- `20_Literature/`, `80_Fleeting/`, and `90_Daily_Journal/` are treated as raw read-only zones.\n"
        )
        atomic_write(scope_abs, content)
        return scope_abs

    def resolve_path(self, layer: MemoryType, key: str) -> str:
        key = key.strip()

        if layer is MemoryType.USER_PROFILE:
            return f"{_LAYER_TO_DIR[layer]}/{_normalize_key(key or 'USER')}.md"
        if layer is MemoryType.LONG_TERM:
            if key.upper() == "MEMORY":
                return "10_Permanent/MEMORY.md"
            return f"10_Permanent/Facts/{_normalize_key(key)}.md"
        if layer is MemoryType.SHORT_TERM:
            return f"{_LAYER_TO_DIR[layer]}/{_normalize_key(key)}.md"
        if layer is MemoryType.SKILL:
            return f"{_LAYER_TO_DIR[layer]}/{_normalize_key(key)}/SKILL.md"
        if layer is MemoryType.SESSION:
            if "/" in key:
                date_key, sid = key.split("/", 1)
            else:
                date_key, sid = datetime.now().strftime("%Y-%m-%d"), key
            return f"{_LAYER_TO_DIR[layer]}/{_normalize_key(date_key)}/{_normalize_key(sid)}.md"
        if layer is MemoryType.CONCEPT:
            return f"{_LAYER_TO_DIR[layer]}/{_normalize_key(key)}.md"

        raise ValueError(f"Unsupported memory layer: {layer}")

    def absolute_path(self, relative: str) -> Path:
        relative = _normalize_relative(relative)
        candidate = (self._root / relative).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"Path escapes vault root: {relative}") from exc
        return candidate

    def read_note(self, path: str) -> Optional[MemoryNote]:
        normalized = _normalize_relative(path)
        absolute = self.absolute_path(normalized)
        if not absolute.exists() or absolute.is_dir():
            return None
        content = absolute.read_text(encoding="utf-8")
        metadata, body = self.parse_frontmatter(content)
        frontmatter = self._dict_to_frontmatter(metadata)
        return MemoryNote(path=normalized, frontmatter=frontmatter, body=body)

    def list_notes(self, layer: MemoryType) -> list[str]:
        base = self._root / _LAYER_TO_DIR[layer]
        if not base.exists():
            return []
        notes = [
            str(path.relative_to(self._root)).replace("\\", "/")
            for path in base.rglob("*.md")
            if path.is_file()
        ]
        notes.sort()
        return notes

    def write_note(self, note: MemoryNote, *, lock_timeout: float = 5.0) -> None:
        normalized = _normalize_relative(note.path)
        if _is_readonly_raw_path(normalized):
            raise PermissionError(f"Readonly raw zone cannot be overwritten: {normalized}")

        reject_reason = scan_memory_content(note.body)
        if reject_reason:
            raise ValueError(f"Memory content blocked by scanner: {reject_reason}")

        note.frontmatter.updated = _now()
        note.frontmatter.char_count = len(note.body)

        target = self.absolute_path(normalized)
        metadata = self._frontmatter_to_dict(note.frontmatter)
        text = self.serialize_frontmatter(metadata, note.body)

        with file_lock(target, timeout=lock_timeout):
            atomic_write(target, text)

    def append_daily(self, date: str, entry: str, *, agent: str = "agent") -> None:
        date = date.strip()
        path = self.resolve_path(MemoryType.SHORT_TERM, date)
        existing = self.read_note(path)
        stamp = datetime.now().strftime("%H:%M:%S")
        block = f"## {stamp} [{agent}]\n\n{entry.strip()}\n"

        if existing is None:
            note = MemoryNote(
                path=path,
                frontmatter=Frontmatter(
                    type=MemoryType.SHORT_TERM,
                    source=MemorySource.FLUSH,
                    agent=agent,
                    tags=["daily_flush"],
                    extras={"date": date},
                ),
                body=f"# {date} ?剜?閮\n\n{block}",
            )
        else:
            note = MemoryNote(
                path=path,
                frontmatter=existing.frontmatter,
                body=f"{existing.body.rstrip()}\n\n{block}",
            )
        self.write_note(note)

    def archive_note(self, path: str, *, reason: str = "") -> None:
        note = self.read_note(path)
        if note is None:
            return
        note.frontmatter.status = "archived"
        if reason:
            note.frontmatter.extras["archive_reason"] = reason
        self.write_note(note)

    def delete_note(self, path: str) -> bool:
        normalized = _normalize_relative(path)
        if _is_readonly_raw_path(normalized):
            return False
        target = self.absolute_path(normalized)
        if not target.exists() or target.is_dir():
            return False
        target.unlink()
        return True

    def parse_frontmatter(self, content: str) -> tuple[dict, str]:
        text = content.replace("\r\n", "\n").replace("\r", "\n")
        if not text.startswith("---\n"):
            return {}, text

        splitter = "\n---\n"
        idx = text.find(splitter, 4)
        if idx < 0:
            return {}, text

        raw_meta = text[4:idx]
        body = text[idx + len(splitter) :]
        loaded = yaml.safe_load(raw_meta) or {}
        if not isinstance(loaded, dict):
            loaded = {}
        return loaded, body

    def serialize_frontmatter(self, metadata: dict, body: str) -> str:
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        if body and not body.endswith("\n"):
            body += "\n"

        dumped = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{dumped}\n---\n\n{body}"

    def obsidian_uri(self, path: str) -> str:
        normalized = _normalize_relative(path)
        vault_name = quote(self.vault_root.name, safe="")
        file_part = normalized.removesuffix(".md")
        file_encoded = quote(file_part, safe="")
        return f"obsidian://open?vault={vault_name}&file={file_encoded}"

    def _dict_to_frontmatter(self, payload: dict) -> Frontmatter:
        now = _now()
        mtype_raw = str(payload.get("type", MemoryType.LONG_TERM.value))
        source_raw = str(payload.get("source", MemorySource.AGENT.value))

        try:
            mtype = MemoryType(mtype_raw)
        except ValueError:
            mtype = MemoryType.LONG_TERM

        try:
            source = MemorySource(source_raw)
        except ValueError:
            source = MemorySource.AGENT

        extras = payload.get("extras", {})
        if not isinstance(extras, dict):
            extras = {}

        tags = payload.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        return Frontmatter(
            type=mtype,
            source=source,
            created=_parse_iso(payload.get("created"), now),
            updated=_parse_iso(payload.get("updated"), now),
            agent=str(payload.get("agent", "agent-memory-core")),
            status=str(payload.get("status", "active")),
            schema_version=int(payload.get("schema_version", 1)),
            tags=[str(t) for t in tags],
            char_count=int(payload.get("char_count", 0)),
            extras={str(k): v for k, v in extras.items()},
        )

    def _frontmatter_to_dict(self, fm: Frontmatter) -> dict:
        return {
            "type": fm.type.value,
            "source": fm.source.value,
            "created": _iso(fm.created),
            "updated": _iso(fm.updated),
            "agent": fm.agent,
            "status": fm.status,
            "schema_version": fm.schema_version,
            "tags": fm.tags,
            "char_count": fm.char_count,
            "extras": fm.extras,
        }

