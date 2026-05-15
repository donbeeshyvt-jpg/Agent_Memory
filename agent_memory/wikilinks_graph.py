"""Wikilinks graph builder for GraphRAG / Obsidian-native multi-hop retrieval.

V2 Phase A Round 4 C13. 對應藍圖 §1.2 + 8.2 「GraphRAG / Wikilinks 圖建構」.

Parse `[[link]]` / `[[link|display]]` / `[[link#section]]` from all vault .md files,
build adjacency dict {source_path: {target_paths}} for multi-hop retrieval.

不依賴 LLM — 純 regex parse + path resolve.

用途:
1. Dynamic memory-context fence (C6) 一跳擴展: 命中 A → 連帶看 A 的 wikilinks
2. Obsidian graph view 跟系統的 retrieval 一致 (使用者看得到的關聯 == AI 看得到的)
3. 未來 entity_graph 內化 (Phase B 規劃中) 的基礎
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# regex: [[target]] or [[target|alias]] or [[target#section]] or [[target.md]]
# 不含 ! (那是 ![[...]] 嵌入語法, 之後也許要區分)
_WIKILINK_RE = re.compile(r"(?<!\!)\[\[([^\[\]\|#]+)(?:#[^\[\]\|]+)?(?:\|[^\[\]]*)?\]\]")

# 排除目錄 (raw zones — 不參與 graph)
_EXCLUDED_PREFIXES = ("20_Literature/", "80_Fleeting/", "90_Daily_Journal/", ".ai/")


def extract_wikilinks(text: str) -> list[str]:
    """Extract raw wikilink targets from markdown body. 回傳 list[str], 含原始 target (未 resolve)."""
    if not text:
        return []
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]


def _normalize_link_target(raw: str) -> str:
    """Normalize wikilink target to a candidate relative path. 把 [[Foo]] 轉 'Foo.md'."""
    target = raw.strip()
    if not target:
        return ""
    target = target.replace("\\", "/")
    if not target.endswith(".md"):
        target = target + ".md"
    return target


def _build_basename_index(vault_root: Path) -> dict[str, list[str]]:
    """Build basename → list[full relative path] index for resolving wikilinks.

    Obsidian wikilinks 常只寫檔名 (`[[USER]]`), 沒寫完整路徑.
    需要從 vault 內找出所有 `*.md` basename 對應位置.
    """
    index: dict[str, list[str]] = {}
    for md in vault_root.rglob("*.md"):
        try:
            rel = md.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        # 跳過 excluded zones
        if any(rel.startswith(p) for p in _EXCLUDED_PREFIXES):
            continue
        # 跳過 _DIR_INFO 與隱藏檔
        if md.name.startswith("_") or md.name.startswith("."):
            continue
        basename = md.stem.lower()
        index.setdefault(basename, []).append(rel)
    return index


def resolve_wikilink(
    raw: str,
    *,
    vault_root: Path,
    source_path: str,
    basename_index: dict[str, list[str]],
) -> str:
    """Resolve a raw wikilink target to a vault-relative path.

    優先順序:
    1. 完整相對路徑 (e.g. `[[10_Permanent/USER.md]]`) — 直接 normalize
    2. basename 比對 (e.g. `[[USER]]`) — 從 basename_index 找
    3. 同目錄相對 (e.g. source 是 `10_Permanent/USER.md`, link `[[notes]]` → `10_Permanent/notes.md`)
    回傳 "" 表示無法 resolve.
    """
    target = _normalize_link_target(raw)
    if not target:
        return ""

    # 1. 完整路徑
    candidate = vault_root / target
    if candidate.exists():
        return target

    # 2. basename 比對
    bare = Path(target).stem.lower()
    matches = basename_index.get(bare, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # 多個同名 → 優先選 source 同目錄 / 同前綴
        source_dir = str(Path(source_path).parent).replace("\\", "/")
        for m in matches:
            if m.startswith(source_dir + "/"):
                return m
        return matches[0]  # fallback 第一個

    # 3. 同目錄相對
    source_dir = Path(source_path).parent
    rel_candidate = (source_dir / target).as_posix()
    if (vault_root / rel_candidate).exists():
        return rel_candidate

    return ""


def build_wikilinks_graph(vault_root: Path) -> dict[str, Any]:
    """Build full wikilinks graph for vault.

    Returns:
        {
            "schema_version": 1,
            "built_at": iso8601,
            "nodes": int,
            "edges": int,
            "unresolved": int,
            "adjacency": { "<from_rel>": ["<to_rel>", ...], ... },
            "reverse":   { "<to_rel>":   ["<from_rel>", ...], ... }
        }
    """
    vault_root = Path(vault_root).resolve()
    if not vault_root.is_dir():
        raise ValueError(f"vault root not found: {vault_root}")

    basename_index = _build_basename_index(vault_root)
    adjacency: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {}
    edges = 0
    unresolved = 0

    for md in vault_root.rglob("*.md"):
        try:
            rel = md.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if any(rel.startswith(p) for p in _EXCLUDED_PREFIXES):
            continue
        if md.name.startswith("_") or md.name.startswith("."):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        raw_links = extract_wikilinks(text)
        if not raw_links:
            continue
        out_set: set[str] = set()
        for raw in raw_links:
            resolved = resolve_wikilink(raw, vault_root=vault_root, source_path=rel, basename_index=basename_index)
            if resolved and resolved != rel:  # 不算自連
                out_set.add(resolved)
            elif not resolved:
                unresolved += 1
        if out_set:
            adjacency[rel] = sorted(out_set)
            edges += len(out_set)
            for target in out_set:
                reverse.setdefault(target, []).append(rel)

    # 排序 reverse 的 list
    reverse = {k: sorted(set(v)) for k, v in reverse.items()}

    return {
        "schema_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "vault_root": str(vault_root),
        "nodes": len(adjacency) + len(reverse),  # 粗略, 不去重 (一個檔可能在 adjacency 跟 reverse 都有)
        "edges": edges,
        "unresolved": unresolved,
        "adjacency": adjacency,
        "reverse": reverse,
    }


def save_graph_json(graph: dict[str, Any], graph_path: Path) -> Path:
    """Save graph to JSON file. 預設位置: <vault>/.ai/wikilinks_graph.json."""
    graph_path = Path(graph_path)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(graph, ensure_ascii=False, indent=2)
    graph_path.write_text(raw, encoding="utf-8")
    return graph_path


def load_graph_json(graph_path: Path) -> dict[str, Any] | None:
    """Load graph from JSON. 回 None 表示不存在."""
    graph_path = Path(graph_path)
    if not graph_path.exists():
        return None
    try:
        return json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def neighbors(graph: dict[str, Any], node: str, *, max_hops: int = 1) -> list[str]:
    """Return all nodes reachable from `node` within `max_hops` (both directions).

    Used by GraphRAG-aware retrieval to expand SearchHit with related notes.
    """
    if not graph:
        return []
    adjacency = graph.get("adjacency", {}) or {}
    reverse = graph.get("reverse", {}) or {}
    visited: set[str] = {node}
    frontier: set[str] = {node}
    for _hop in range(max_hops):
        next_frontier: set[str] = set()
        for n in frontier:
            for nb in adjacency.get(n, []):
                if nb not in visited:
                    next_frontier.add(nb)
            for nb in reverse.get(n, []):
                if nb not in visited:
                    next_frontier.add(nb)
        visited.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    visited.discard(node)
    return sorted(visited)


def default_graph_path(vault_root: Path) -> Path:
    """`<vault>/.ai/wikilinks_graph.json`."""
    return Path(vault_root) / ".ai" / "wikilinks_graph.json"


def rebuild_and_save(vault_root: Path) -> dict[str, Any]:
    """One-shot: build + save + return summary stats (no adjacency)."""
    graph = build_wikilinks_graph(vault_root)
    path = save_graph_json(graph, default_graph_path(vault_root))
    return {
        "ok": True,
        "graph_path": str(path),
        "schema_version": graph["schema_version"],
        "built_at": graph["built_at"],
        "nodes": graph["nodes"],
        "edges": graph["edges"],
        "unresolved": graph["unresolved"],
    }
