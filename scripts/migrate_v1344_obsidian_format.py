"""V3-O.15.44 (2026-06-10 user 拍板): Obsidian 格式三修 migration.

1. 朋友卡: 檔名 <uid>.md → <第一次建卡暱稱>.md (重複 → 暱稱_1), contributor_link 改連新檔名.
2. SKILL/KB: frontmatter aliases/tags flow list → block list (每項一行一個 chip),
   tags `trigger:xxx` → `trigger/xxx` (Obsidian tag 禁冒號), 加 `related:` 強關聯 [[]] block.
3. body: 關聯概念逗號串 → 每項一行自己的 [[]]; SKILL 來源 [[a;b]] → 拆開.
4. 跨檔 link: SKILL 卡 contributor_link 指向 22_Casual_Viewers/<uid> → 改新檔名 stem.
5. 全部改過的檔 + 朋友卡 reindex FTS5 (朋友卡從沒 index 過, 一次補).

用法:
    python scripts/migrate_v1344_obsidian_format.py <vault_root>

idempotent: 已是 block list / 已改名的直接跳過. 朋友卡 rename 前不留 .bak (rename 可逆).
⚠ 必須在 bridge 停機時跑 (舊 code 會把 uid 檔名寫回來).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BAD_FILENAME = '\\/:*?"<>|#^[]'


def _safe_card_name(name: str) -> str:
    out = "".join(("_" if c in _BAD_FILENAME else c) for c in (name or "").strip())
    return out.strip(" ._")[:60]


def _parse_flow_list(s: str) -> list[str]:
    s = s.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return []
    inner = s[1:-1].strip()
    if not inner:
        return []
    out = []
    for p in re.split(r",\s*", inner):
        p = p.strip()
        if (p.startswith("'") and p.endswith("'")) or (p.startswith('"') and p.endswith('"')):
            p = p[1:-1]
        if p:
            out.append(p)
    return out


def _block_list(key: str, items: list[str], *, quote: bool = True) -> list[str]:
    vals = [str(x).strip() for x in items if str(x).strip()]
    if not vals:
        return [f"{key}: []"]
    out = [f"{key}:"]
    for v in vals:
        v_safe = v.replace('"', "'")
        out.append(f'  - "{v_safe}"' if quote else f"  - {v_safe}")
    return out


def _fm_get(text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}: (.*)$", text, re.M)
    return m.group(1).strip() if m else ""


# ─── SKILL / KB 格式修 ──────────────────────────────────────────


def migrate_card_format(path: Path, kind: str) -> bool:
    """kind ∈ {skill, kb}. 回傳是否有改動."""
    text = path.read_text(encoding="utf-8")
    orig = text

    if not text.startswith("---"):
        return False
    fm_end = text.find("\n---", 4)
    if fm_end == -1:
        return False
    fm = text[4:fm_end]
    body = text[fm_end + 4:]

    fm_lines_out: list[str] = []
    has_related = bool(re.search(r"^related:", fm, re.M))
    emotional_origin = _fm_get(fm, "emotional_origin")

    for ln in fm.splitlines():
        stripped = ln.strip()
        # aliases / tags flow → block
        m = re.match(r"^(aliases|tags): (\[.*\])$", ln)
        if m:
            key, flow = m.group(1), m.group(2)
            items = _parse_flow_list(flow)
            if key == "tags":
                items = [i.replace("trigger:", "trigger/") for i in items]
                fm_lines_out.extend(_block_list(key, items, quote=False))
            else:
                fm_lines_out.extend(_block_list(key, items, quote=True))
            continue
        # tags block 既有項目裡的 trigger: → trigger/
        if re.match(r"^\s+- .*trigger:", ln):
            fm_lines_out.append(ln.replace("trigger:", "trigger/"))
            continue
        fm_lines_out.append(ln)

    # 加 related: 強關聯 (security_level 行後面) — skill 用 emotional_origin, kb 用 related_concept_ids
    if not has_related:
        rel_items: list[str] = []
        if kind == "skill" and emotional_origin:
            rel_items = [f"[[{o.strip()}]]" for o in emotional_origin.split(";") if o.strip()]
        elif kind == "kb":
            rc = _fm_get(fm, "related_concept_ids")
            rel_items = [f"[[{c}]]" for c in _parse_flow_list(rc)]
        rel_block = _block_list("related", rel_items)
        out2 = []
        inserted = False
        for ln in fm_lines_out:
            out2.append(ln)
            if not inserted and ln.startswith("security_level:"):
                out2.extend(rel_block)
                inserted = True
        if not inserted:  # 沒 security_level 行 → 加在最後
            out2.extend(rel_block)
        fm_lines_out = out2

    # ─── body 修 ───
    # 1) 關聯概念逗號串 → 每項一行
    def _fix_rel_line(m: re.Match) -> str:
        links = re.findall(r"\[\[([^\]]+)\]\]", m.group(1))
        return "- 關聯概念:\n" + "\n".join(f"  - [[{l}]]" for l in links)

    body = re.sub(r"^- 關聯概念: (.+)$", _fix_rel_line, body, flags=re.M)

    # 2) SKILL 來源 [[a;b]] → 拆開每項
    def _fix_origin(m: re.Match) -> str:
        inner = m.group(1)
        note = m.group(2) or ""
        items = [o.strip() for o in inner.split(";") if o.strip()]
        if len(items) <= 1:
            return m.group(0)
        bullets = "\n".join(f"- [[{o}]]" for o in items)
        return f"{bullets}\n- {note.strip()}" if note.strip() else bullets

    body = re.sub(r"^- \[\[([^\]]*;[^\]]*)\]\] ?(\(.*\))?$", _fix_origin, body, flags=re.M)

    new_text = "---\n" + "\n".join(fm_lines_out) + "\n---" + body
    if new_text != orig:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


# ─── 朋友卡 rename ──────────────────────────────────────────────


def migrate_friend_cards(vault_root: Path) -> list[tuple[str, str]]:
    """rename uid.md → 暱稱.md. 回傳 [(old_rel, new_rel), ...] 給 reindex + 跨檔 link 修."""
    renames: list[tuple[str, str]] = []
    for tier_rel in ("20_Audience_Graph/22_Casual_Viewers", "20_Audience_Graph/21_VIP_Viewers"):
        d = vault_root / tier_rel
        if not d.exists():
            continue
        used = {p.stem for p in d.glob("*.md")}
        for p in sorted(d.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            uid = _fm_get(text, "user_id")
            name = _fm_get(text, "display_name")
            # 檔名已經不是 uid (已改名過 / 手動命名) → 只確保 contributor_link 正確
            if p.stem != uid:
                continue
            base = _safe_card_name(name)
            if not base or base == uid:
                continue  # 沒暱稱可用, 保持 uid
            new_stem = base
            n = 0
            while new_stem in used:
                n += 1
                new_stem = f"{base}_{n}"
            used.discard(p.stem)
            used.add(new_stem)
            new_path = d / f"{new_stem}.md"
            # contributor_link 自連 → 新 stem
            text = re.sub(
                r'^contributor_link: "\[\[[^|\]]+\|',
                f'contributor_link: "[[{new_stem}|',
                text, flags=re.M,
            )
            new_path.write_text(text, encoding="utf-8")
            p.unlink()
            renames.append((f"{tier_rel}/{p.name}", f"{tier_rel}/{new_path.name}"))
            print(f"  FRIEND renamed: {p.stem} → {new_stem}  ({name})")
    return renames


def fix_cross_links(vault_root: Path, renames: list[tuple[str, str]]) -> int:
    """SKILL/KB 卡裡指向舊 uid 檔名的 wikilink → 新檔名."""
    if not renames:
        return 0
    mapping = {}
    for old_rel, new_rel in renames:
        old_stem = Path(old_rel).stem
        new_stem = Path(new_rel).stem
        old_dir = str(Path(old_rel).parent).replace("\\", "/")
        mapping[f"[[{old_dir}/{old_stem}|"] = f"[[{old_dir}/{new_stem}|"
        mapping[f"[[{old_stem}|"] = f"[[{new_stem}|"
    fixed = 0
    roots = [vault_root / "50_Skills_Tools", vault_root / "40_Knowledge_Base"]
    for root in roots:
        if not root.exists():
            continue
        for md in root.rglob("*.md"):
            if "_processed" in md.parts or "_inbox" in md.parts:
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            new = text
            for old, repl in mapping.items():
                new = new.replace(old, repl)
            if new != text:
                md.write_text(new, encoding="utf-8")
                fixed += 1
                print(f"  LINK fixed: {md.relative_to(vault_root)}")
    return fixed


def main(vault_root: Path) -> None:
    if not vault_root.exists():
        print(f"ERR vault not found: {vault_root}")
        sys.exit(1)

    changed_paths: list[str] = []

    # 1) SKILL
    sk_root = vault_root / "50_Skills_Tools" / "54_Taught_Skills"
    n_skill = 0
    if sk_root.exists():
        for md in sk_root.rglob("SKILL.md"):
            if "_merged" in md.parts or "_archived" in md.parts:
                continue
            if migrate_card_format(md, "skill"):
                n_skill += 1
                rel = str(md.relative_to(vault_root))
                changed_paths.append(rel)
                print(f"  SKILL ✓ {rel}")

    # 2) KB
    kb_root = vault_root / "40_Knowledge_Base"
    n_kb = 0
    if kb_root.exists():
        for md in kb_root.rglob("*.md"):
            if "_inbox" in md.parts or "_processed" in md.parts:
                continue
            if migrate_card_format(md, "kb"):
                n_kb += 1
                rel = str(md.relative_to(vault_root))
                changed_paths.append(rel)
                print(f"  KB    ✓ {rel}")

    # 3) 朋友卡 rename
    renames = migrate_friend_cards(vault_root)

    # 4) 跨檔 link
    n_links = fix_cross_links(vault_root, renames)

    # 5) reindex: 改過的 SKILL/KB + 全部朋友卡 (從沒 index 過, 一次補)
    try:
        from agent_memory.search import MemorySearchManager
        from agent_memory.vault import ObsidianVaultAdapter
        mgr = MemorySearchManager(ObsidianVaultAdapter(vault_root))
        n_ix = 0
        for old_rel, _ in renames:
            mgr.remove_path(old_rel.replace("\\", "/"))
        for tier_rel in ("20_Audience_Graph/22_Casual_Viewers", "20_Audience_Graph/21_VIP_Viewers"):
            d = vault_root / tier_rel
            if not d.exists():
                continue
            for p in d.glob("*.md"):
                if mgr.index_path(f"{tier_rel}/{p.name}"):
                    n_ix += 1
        for rel in changed_paths:
            if mgr.index_path(rel.replace("\\", "/")):
                n_ix += 1
        print(f"  reindexed: {n_ix} files (friend cards + changed cards)")
    except Exception as exc:
        print(f"  reindex FAIL {type(exc).__name__}: {str(exc)[:120]} (fallback substring 仍可撈)")

    print()
    print(f"Summary: SKILL fixed={n_skill}  KB fixed={n_kb}  FRIEND renamed={len(renames)}  links fixed={n_links}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python migrate_v1344_obsidian_format.py <vault_root>")
        sys.exit(2)
    main(Path(sys.argv[1]))
