"""On-demand reflection — 使用者主動「幫我整理 X 主題」(R9 C29).

對應 MISSION §5.4 LLM 介入「B. On-demand」+ 使用者願景「也能幫使用者歸納出已經
吸收過的知識是核心」.

跟 R9 C27 LLM umbrella 差別:
- C27 (sleep cycle): curator weekly 自動跑, 對全 Mid_Term entity 找合併建議
- C29 (on-demand) : 使用者明確要求「幫我整理 X」, 對特定 topic 跨層級 (Mid_Term +
                    Concepts + Manual_Inputs) 全掃 → 產出 `Concepts/reflection_<topic>_<date>.md`

入口:
- CLI: `python -m agent_memory reflect --topic Python`
- 對話: 偵測 `/reflect <topic>` keyword (chat_runtime parse)

LLM call 量: 使用者觸發 (~3-5k token), 不固定頻率.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent_memory.security.atomic import atomic_write
from agent_memory.types import Frontmatter, LifecycleState, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

REFLECTION_DIR = "10_Permanent/Concepts"
DEFAULT_TOPIC_MATCH_DIRS = (
    "10_Permanent/Mid_Term",
    "10_Permanent/Concepts",
    "10_Permanent/Facts",
    "10_Permanent/Manual_Inputs",
)
DEFAULT_MAX_MATCH = 30


def _slug(s: str) -> str:
    s2 = re.sub(r"[^\w一-鿿\-]+", "-", s.strip().lower()).strip("-")
    return s2[:60] or "topic"


def _scan_topic_matches(vault_root: Path, topic: str, max_match: int = DEFAULT_MAX_MATCH) -> list[dict[str, Any]]:
    """Scan 10_Permanent/ 各層找跟 topic 相關 .md.

    匹配條件 (substring):
    - filename 含 topic (entity_id)
    - body 含 topic
    - aliases / tags 含 topic
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    topic_lower = topic.lower()
    results: list[dict[str, Any]] = []
    for dir_rel in DEFAULT_TOPIC_MATCH_DIRS:
        d = root / dir_rel
        if not d.exists():
            continue
        for p in d.rglob("*.md"):
            if not p.is_file() or p.name.startswith("_"):
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            note = adapter.read_note(rel)
            if note is None:
                continue
            score = 0
            if topic_lower in p.stem.lower():
                score += 3
            if topic_lower in note.body.lower():
                score += 2
            if any(topic_lower in a.lower() for a in (note.frontmatter.aliases or [])):
                score += 2
            if any(topic_lower in t.lower() for t in note.frontmatter.tags):
                score += 1
            if score == 0:
                continue
            results.append({
                "path": rel,
                "score": score,
                "summary": (next((ln.strip() for ln in note.body.splitlines() if ln.strip() and not ln.strip().startswith("#")), p.stem))[:200],
                "tags": list(note.frontmatter.tags),
                "lifecycle": note.frontmatter.lifecycle_state.value if hasattr(note.frontmatter.lifecycle_state, "value") else str(note.frontmatter.lifecycle_state),
            })
            if len(results) >= max_match:
                break
        if len(results) >= max_match:
            break
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def _build_reflection_prompt(topic: str, matches: list[dict[str, Any]]) -> str:
    lines = [
        f"你是 Memory Reflector. 使用者要求整理「{topic}」相關的所有記憶, 產出 atomic-style 摘要 markdown.",
        "",
        "請彙整以下 sources 成「## 主題概覽」+「## 核心要點 (列表)」+「## 關聯 wikilinks」+「## 後續建議」四段.",
        "中文敘述, 不要其他多餘格式. 用 `[[...]]` 標出關鍵 wikilinks 給 GraphRAG 一跳擴展用.",
        "",
        f"Topic: {topic}",
        "",
        "Sources:",
    ]
    for m in matches:
        lines.append(f"- `{m['path']}` (score={m['score']}, state={m['lifecycle']}): {m['summary']}")
    return "\n".join(lines)


def _call_llm_reflect(prompt: str, *, mock_body: str | None = None) -> str | None:
    if mock_body is not None:
        return mock_body
    try:
        from agent_memory.llm_client import LLMClient
        client = LLMClient()
        result = client.generate(prompt=prompt, temperature=0.3, timeout_s=60.0, max_tokens=1200)
        return result.content.strip() if hasattr(result, "content") else str(result).strip()
    except Exception:  # noqa: BLE001
        return None


def reflect_topic(
    vault_root: Path,
    topic: str,
    *,
    mock_body: Optional[str] = None,
    max_match: int = DEFAULT_MAX_MATCH,
) -> dict[str, Any]:
    """主入口 — 對 topic 跑 reflection, 產出 `Concepts/reflection_<topic>_<YYYY-MM-DD>.md`."""

    root = Path(vault_root).expanduser().resolve()
    if not topic.strip():
        return {"action": "error", "reason": "empty_topic"}

    matches = _scan_topic_matches(root, topic, max_match=max_match)
    if not matches:
        return {"action": "no_matches", "topic": topic}

    prompt = _build_reflection_prompt(topic, matches)
    body_text = _call_llm_reflect(prompt, mock_body=mock_body)
    if body_text is None:
        return {"action": "llm_failed", "topic": topic, "matches": len(matches)}

    now = datetime.now().astimezone()
    slug = _slug(topic)
    target_rel = f"{REFLECTION_DIR}/reflection_{slug}_{now.date().isoformat()}.md"
    adapter = ObsidianVaultAdapter(root)
    note = MemoryNote(
        path=target_rel,
        frontmatter=Frontmatter(
            type=MemoryType.CONCEPT,
            source=MemorySource.PROMOTION,
            tags=["reflection", "on_demand", slug],
            agent="reflect-on-demand",
            lifecycle_state=LifecycleState.LONG,  # reflection 視為已整理長期內容
            mention_count=0,
            last_activity_at=now.isoformat(),
            pinned=False,
            aliases=[topic],
            extras={
                "topic": topic,
                "sources_count": len(matches),
                "generated_at": now.isoformat(),
            },
        ),
        body=(
            f"# Reflection: {topic}\n\n"
            f"> 由 R9 C29 reflect-on-demand 自動整理.\n"
            f"> 統計來源 {len(matches)} 個 .md. 對應 MISSION §3.7「歸納已吸收的知識」.\n"
            f"> 產生時間: {now.isoformat()}\n\n"
            "## 主題覆蓋來源\n\n"
            + "\n".join(f"- `{m['path']}` (state={m['lifecycle']}, score={m['score']})" for m in matches)
            + "\n\n"
            f"{body_text}\n"
        ),
    )
    try:
        adapter.write_note(note)
    except Exception as exc:  # noqa: BLE001
        return {"action": "write_failed", "topic": topic, "error": str(exc)}

    return {
        "action": "created",
        "topic": topic,
        "path": target_rel,
        "matches_count": len(matches),
        "mock_used": mock_body is not None,
    }
