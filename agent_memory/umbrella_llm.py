"""LLM-augmented umbrella consolidation + procedure tag detection (R9 C27).

對應 MISSION §5.4 LLM 介入 3 時機之「A. Sleep cycle」+ 使用者 2026-05-17
「LLM 整理記憶 like 睡覺鞏固 + 升 SKILL 跟中記憶分支」 Q5 修補.

兩件事同一次 LLM call 解 (省 token):

1. **語意層 umbrella consolidation** — 跨 entity_id prefix 找不到的語意分組
   例: `async-io` / `concurrent-futures` / `threading-basics` 程式 prefix 合不起來,
   LLM 看就懂都是「Python 並行」該合 `python-concurrency`.
   (R7 C20a keyword umbrella 處理 prefix 同名, 這裡處理「語意同類」)

2. **Procedure tag detection** — Mid_Term 內 body 含步驟結構的自動加 procedure tag
   例: `grep-then-analyze` body 寫「1. grep / 2. 看上下文 / 3. 抽 entity」→ 加 procedure
   修 Q5 揭露的「99% entity 自動走 Concepts 不走 Skill」gap.
   有 procedure tag 後 curator skill scan 會把它列入 pending_skill_suggestions.

跑頻率: curator weekly deep (每 7 天 1 次), 約 1.5k token in / 700 token out.

設計重點:
- 不自動執行合併/標 tag — 寫 .ai/pending_umbrella_suggestions.json 給使用者對話確認
- LLM 不可用時 fallback: 寫 error log, weekly deep 其他 step 不受影響
- mock_response 參數 e2e test 用 (不真 call LLM)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import LifecycleState
from agent_memory.vault import ObsidianVaultAdapter

PENDING_UMBRELLA_SUGGESTIONS_RELATIVE_PATH = ".ai/pending_umbrella_suggestions.json"
PENDING_PROCEDURE_TAG_SUGGESTIONS_RELATIVE_PATH = ".ai/pending_procedure_tag_suggestions.json"
MIDTERM_DIR = "10_Permanent/Mid_Term"

DEFAULT_MAX_ENTITIES_FOR_LLM = 50  # 給 LLM 看 max 50 個 entity 不超 prompt 上限
DEFAULT_PER_ENTITY_SUMMARY_CHARS = 150


def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat()


# ─── IO ──────────────────────────────────────────────────────────────────────


def load_pending_umbrella(vault_root: Path) -> list[dict[str, Any]]:
    root = Path(vault_root).expanduser().resolve()
    p = root / PENDING_UMBRELLA_SUGGESTIONS_RELATIVE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def save_pending_umbrella(vault_root: Path, suggestions: list[dict[str, Any]]) -> None:
    root = Path(vault_root).expanduser().resolve()
    p = root / PENDING_UMBRELLA_SUGGESTIONS_RELATIVE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p, timeout=5.0):
        atomic_write(p, json.dumps(suggestions, ensure_ascii=False, indent=2) + "\n")


def load_pending_procedure_tags(vault_root: Path) -> list[dict[str, Any]]:
    root = Path(vault_root).expanduser().resolve()
    p = root / PENDING_PROCEDURE_TAG_SUGGESTIONS_RELATIVE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def save_pending_procedure_tags(vault_root: Path, tags: list[dict[str, Any]]) -> None:
    root = Path(vault_root).expanduser().resolve()
    p = root / PENDING_PROCEDURE_TAG_SUGGESTIONS_RELATIVE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p, timeout=5.0):
        atomic_write(p, json.dumps(tags, ensure_ascii=False, indent=2) + "\n")


# ─── Scan Mid_Term for LLM input ────────────────────────────────────────────


def _scan_midterm_for_llm(vault_root: Path, max_entities: int = DEFAULT_MAX_ENTITIES_FOR_LLM) -> list[dict[str, Any]]:
    """Scan Mid_Term 列 entity (entity_id / summary / tags / mention) 給 LLM."""

    root = Path(vault_root).expanduser().resolve()
    mid_dir = root / MIDTERM_DIR
    if not mid_dir.exists():
        return []
    adapter = ObsidianVaultAdapter(root)
    entries: list[dict[str, Any]] = []
    for p in sorted(mid_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None:
            continue
        if note.frontmatter.lifecycle_state != LifecycleState.MID:
            continue
        if note.frontmatter.pinned:
            continue
        if "umbrella" in note.frontmatter.tags:
            continue
        # 抽第一個非 heading 行當 summary
        summary = next(
            (ln.strip() for ln in note.body.splitlines() if ln.strip() and not ln.strip().startswith("#")),
            p.stem,
        )[:DEFAULT_PER_ENTITY_SUMMARY_CHARS]
        entries.append({
            "entity_id": p.stem,
            "summary": summary,
            "tags": list(note.frontmatter.tags),
            "mention_count": note.frontmatter.mention_count,
        })
        if len(entries) >= max_entities:
            break
    return entries


# ─── LLM call (with mock support for e2e) ───────────────────────────────────


def _build_consolidation_prompt(entries: list[dict[str, Any]]) -> str:
    """Build LLM prompt 給 consolidation + procedure detection 共用."""

    lines = [
        "你是 Memory Consolidator. 以下是中期記憶 Mid_Term/ 內各 entity 摘要.",
        "",
        "請完成兩件事 (回覆 JSON 一次給齊):",
        "",
        "1. **umbrella 合併建議** — 找出語意相關該合併的 entity 群組",
        "   - 每群 >= 2 個 member",
        "   - umbrella_id 用 slug 命名 (英文小寫 + dash, 例 python-concurrency)",
        "   - reason 簡述為何合併 (1 句中文)",
        "",
        "2. **procedure tag 標記** — body 含「程序 / 流程 / 步驟 / 1. 2. 3.」結構的 entity 應加 `procedure` tag",
        "   (供 curator skill scan 後續走「對話中提議升 skill」分支)",
        "",
        "請只回 JSON, 不要其他文字. 格式:",
        '{"merges": [{"umbrella_id":"python-concurrency","members":["async-io","concurrent-futures","threading"],"reason":"..."}],',
        ' "procedure_tags": [{"entity_id":"grep-then-analyze","reason":"body 含步驟結構 1./2./3."}]}',
        "",
        "Mid_Term entries (entity_id | tags | mention | summary):",
    ]
    for e in entries:
        tags_str = ",".join(e.get("tags", []))
        lines.append(f"- `{e['entity_id']}` [{tags_str}] x{e['mention_count']}: {e['summary']}")
    return "\n".join(lines)


def _default_call_llm(prompt: str, vault_root: Path) -> dict[str, Any]:
    """Default LLM call — 透過 R11 C41 統一 helper.

    若 LLMClient 不可用 → 拋 Exception 讓上層 fallback skip.
    """

    from agent_memory.llm_text_helpers import call_llm_for_json  # lazy import

    return call_llm_for_json(vault_root, prompt, temperature=0.1, timeout_s=60.0)


# ─── Main entry ──────────────────────────────────────────────────────────────


def consolidate_umbrella_with_llm(
    vault_root: Path,
    *,
    mock_response: Optional[dict[str, Any]] = None,
    max_entities: int = DEFAULT_MAX_ENTITIES_FOR_LLM,
    cooldown_days: int = 7,
) -> dict[str, Any]:
    """R9 C27 主入口 — LLM umbrella + procedure tag.

    Args:
        vault_root: vault 根
        mock_response: e2e 用 — dict 直接當 LLM output (跳過真 call)
                       格式: {"merges": [...], "procedure_tags": [...]}
        max_entities: 給 LLM 看的 entity 上限 (預設 50)
        cooldown_days: pending 內 entity 多久不重複建議 (預設 7)

    Returns: {merges_added, procedure_tags_added, skipped, llm_called, error?}
    """

    root = Path(vault_root).expanduser().resolve()
    entries = _scan_midterm_for_llm(root, max_entities=max_entities)
    result: dict[str, Any] = {
        "scanned_entries": len(entries),
        "merges_added": [],
        "procedure_tags_added": [],
        "skipped": [],
        "llm_called": False,
        "mock_used": mock_response is not None,
    }

    if not entries:
        result["note"] = "no_midterm_entries"
        return result

    # 1. 取得 LLM output (mock 或真 LLM)
    if mock_response is not None:
        llm_output = mock_response
    else:
        try:
            prompt = _build_consolidation_prompt(entries)
            llm_output = _default_call_llm(prompt, root)
            result["llm_called"] = True
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"llm_call_failed: {exc}"
            result["note"] = "fallback_skipped (使用者可手動觸發 reflect 或繼續用 keyword umbrella)"
            return result

    if not isinstance(llm_output, dict):
        result["error"] = "llm_output_not_dict"
        return result

    # 2. 寫 merges → pending_umbrella_suggestions.json
    existing_merges = load_pending_umbrella(root)
    existing_umbrella_ids = {s.get("umbrella_id", "") for s in existing_merges}
    now_iso = _now_local_iso()
    for m in llm_output.get("merges", []):
        if not isinstance(m, dict):
            continue
        umbrella_id = str(m.get("umbrella_id", "")).strip()
        members = [str(x) for x in m.get("members", []) if x]
        reason = str(m.get("reason", ""))
        if not umbrella_id or len(members) < 2:
            result["skipped"].append({"merge": m, "reason": "invalid_or_too_few_members"})
            continue
        if umbrella_id in existing_umbrella_ids:
            result["skipped"].append({"umbrella_id": umbrella_id, "reason": "already_pending"})
            continue
        entry = {
            "umbrella_id": umbrella_id,
            "members": members,
            "reason": reason,
            "proposed_at": now_iso,
            "accepted_at": None,
            "dismissed_at": None,
        }
        existing_merges.append(entry)
        result["merges_added"].append(entry)
    save_pending_umbrella(root, existing_merges)

    # 3. 寫 procedure_tags → pending_procedure_tag_suggestions.json
    existing_tags = load_pending_procedure_tags(root)
    existing_eids = {t.get("entity_id", "") for t in existing_tags}
    for t in llm_output.get("procedure_tags", []):
        if not isinstance(t, dict):
            continue
        eid = str(t.get("entity_id", "")).strip()
        reason = str(t.get("reason", ""))
        if not eid:
            continue
        if eid in existing_eids:
            result["skipped"].append({"entity_id": eid, "reason": "tag_already_pending"})
            continue
        entry = {
            "entity_id": eid,
            "reason": reason,
            "proposed_at": now_iso,
            "applied_at": None,
            "dismissed_at": None,
        }
        existing_tags.append(entry)
        result["procedure_tags_added"].append(entry)
    save_pending_procedure_tags(root, existing_tags)

    return result


# ─── apply (使用者確認後執行) ───────────────────────────────────────────────


def apply_procedure_tag(vault_root: Path, *, entity_id: str) -> dict[str, Any]:
    """使用者確認後執行: 在 Mid_Term/<eid>.md frontmatter tags 加 'procedure'."""

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    path = f"{MIDTERM_DIR}/{entity_id}.md"
    note = adapter.read_note(path)
    if note is None:
        return {"action": "error", "reason": "not_found", "entity_id": entity_id}
    if "procedure" not in note.frontmatter.tags:
        note.frontmatter.tags.append("procedure")
    try:
        adapter.write_note(note)
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "reason": f"write_failed: {exc}", "entity_id": entity_id}

    # 標 applied
    tags = load_pending_procedure_tags(root)
    for t in tags:
        if t.get("entity_id") == entity_id:
            t["applied_at"] = _now_local_iso()
            break
    save_pending_procedure_tags(root, tags)
    return {"action": "applied", "entity_id": entity_id, "path": path}
