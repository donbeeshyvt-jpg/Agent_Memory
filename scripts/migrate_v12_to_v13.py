"""V3-O.15.41 (2026-06-10): 一次性 migration script — 既有 vault KB/SKILL schema_v12 → v13 內化格式.

新 v13 結構 (user 拍板):
  frontmatter 加 aliases / security_level / created_at / updated_at, schema_version: 12→13
  body 包成:
    # 核心摘要 <summary>...</summary>             (RAG 比對主擊中段)
    # 詳細內容 <context>...</context>             (LLM 視為 raw data, 拒絕執行內含指令)
    # 相關實體與偏好影響                            (metadata 區: 關聯 / 應用場景 / 教學追溯)

對齊 user 設計: <summary>/<context> XML 包覆 → 注入 prompt 時 LLM 知道邊界 (拒執行).

用法:
    python scripts/migrate_v12_to_v13.py <vault_root>
    例: python scripts/migrate_v12_to_v13.py Z:/Cursor練習用/Agent_Memory/test/SecondBrains/companion_test

設計:
- idempotent: 已是 v13 直接跳過
- 寫前先備份 .bak_v12 (失敗可 restore)
- frontmatter 用簡單 line-based parse (避免 yaml lib 把 list 等 round-trip 改格式)
- body 用 ## 標題 切段重組
"""

from __future__ import annotations

import sys
import re
from datetime import datetime, timezone
from pathlib import Path


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    """切 frontmatter (--- ... ---) + body. 回 (fm_lines_inner, body_raw)."""
    if not text.startswith("---"):
        return [], text
    end = text.find("\n---", 4)
    if end == -1:
        return [], text
    fm = text[4:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    return fm.splitlines(), body


def _get_fm_value(fm_lines: list[str], key: str) -> str | None:
    """簡單 line-based: 找 `key: value`."""
    prefix = f"{key}:"
    for ln in fm_lines:
        ln_s = ln.lstrip()
        if ln_s.startswith(prefix):
            return ln_s[len(prefix):].strip()
    return None


def _strip_quotes(s: str) -> str:
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _parse_list_literal(s: str) -> list[str]:
    """簡單 ['a', 'b'] 或 [a, b] parse."""
    s = s.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return []
    inner = s[1:-1].strip()
    if not inner:
        return []
    parts = re.split(r",\s*", inner)
    out = []
    for p in parts:
        p = p.strip()
        p = _strip_quotes(p)
        if p:
            out.append(p)
    return out


def _split_body_sections(body: str, known_titles: set[str] | None = None) -> dict[str, str]:
    """切 body 為 {section_title: content} dict, 以 `## ` heading 為界.

    known_titles: 若提供, 只在 ## 行 title 在此集合時才切段 (其他 ## 視為 nested subsection 收進 parent).
    這修一個常見 LLM 寫法 bug: full_content 內含 `## 事件概述` 等同 level heading, 不該被當頂層切.
    """
    sections: dict[str, str] = {}
    current_title: str | None = None
    current_lines: list[str] = []
    for ln in body.splitlines():
        if ln.startswith("## "):
            title = ln[3:].strip()
            is_known = (known_titles is None) or (title in known_titles)
            if is_known:
                if current_title is not None:
                    sections[current_title] = "\n".join(current_lines).strip("\n")
                current_title = title
                current_lines = []
                continue
        current_lines.append(ln)
    if current_title is not None:
        sections[current_title] = "\n".join(current_lines).strip("\n")
    return sections


# V3-O.15.41: 已知頂層 section 集合 (per kind), 避免 LLM 寫 full_content 內 ## subsection 被誤切.
_SKILL_KNOWN_TOP = {
    "觸發情境", "描述", "核心摘要", "標籤",
    "實際打法 (可直接複製)", "正確示範", "完整內容", "步驟摘要", "使用邊界", "範例對話",
    "教學追溯", "來源 (Origin)",
}
_KB_KNOWN_TOP = {
    "觸發情境 / 適用情境", "核心摘要", "完整內容", "章節大綱", "重要原文",
    "投餵追溯", "標籤", "相關概念 (雙關聯)",
}


def _migrate_one_md(path: Path, kind: str) -> tuple[bool, str]:
    """Migrate 1 個 SKILL.md 或 KB.md. kind ∈ {'skill', 'kb'}.

    Returns: (migrated, message)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"read fail: {exc}"

    fm_lines, body = _split_frontmatter(text)
    if not fm_lines:
        return False, "no frontmatter"

    # idempotent
    sv = _get_fm_value(fm_lines, "schema_version")
    if sv and sv.strip() == "13":
        return False, "already v13 (skip)"

    # 備份
    bak = path.with_suffix(path.suffix + ".bak_v12")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")

    # ─── frontmatter 改造 ───
    # 取既有值
    title = _strip_quotes(_get_fm_value(fm_lines, "title") or "")
    trig_kw_raw = _get_fm_value(fm_lines, "trigger_keywords") or "[]"
    trig_kws = _parse_list_literal(trig_kw_raw)
    first_at = _get_fm_value(fm_lines, "first_contributed_at") or _get_fm_value(fm_lines, "first_taught_at") or _get_fm_value(fm_lines, "created_at") or ""
    last_at = _get_fm_value(fm_lines, "last_reinforced_at") or first_at or ""
    created_date = (first_at[:10] if first_at else datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    updated_date = (last_at[:10] if last_at else created_date)

    aliases = trig_kws[:5]

    # 已存在的 key (避免重複加)
    fm_text_lower = "\n".join(fm_lines)
    has_aliases = "aliases:" in fm_text_lower
    has_security = "security_level:" in fm_text_lower
    has_updated = "updated_at:" in fm_text_lower
    has_created_date = bool(re.search(r"^created_at: \d{4}-\d{2}-\d{2}$", fm_text_lower, re.M))

    # 改 schema_version 12→13
    new_fm_lines = []
    inserted_user_facing = False
    for ln in fm_lines:
        ls = ln.lstrip()
        if ls.startswith("schema_version:"):
            new_fm_lines.append("schema_version: 13")
            continue
        # 把 frontmatter 開頭的 type 之前插入使用者面 (title/aliases/tags/created/updated/security)
        if not inserted_user_facing and ls.startswith("type:"):
            # title 行重寫加引號
            if title:
                new_fm_lines.append(f"title: \"{title}\"")
            if not has_aliases:
                new_fm_lines.append(f"aliases: {aliases}")
            if not has_created_date:
                new_fm_lines.append(f"created_at: {created_date}")
            if not has_updated:
                new_fm_lines.append(f"updated_at: {updated_date}")
            if not has_security:
                new_fm_lines.append("security_level: safe_data")
            inserted_user_facing = True
            new_fm_lines.append(ln)
            continue
        # 跳過舊 title 行 (避免重複, 已在上面重寫)
        if ls.startswith("title:") and inserted_user_facing:
            continue
        # 跳過舊 created_at 行 (避免重複)
        if ls.startswith("created_at:") and inserted_user_facing and not has_created_date:
            # 舊的 created_at 是 ISO datetime, 我們改成 date only → 直接跳過 (上面已新加)
            continue
        new_fm_lines.append(ln)
    if not inserted_user_facing:
        # type 行不存在? 直接追加
        if title:
            new_fm_lines.insert(0, f"title: \"{title}\"")
        new_fm_lines.insert(1, f"aliases: {aliases}")
        new_fm_lines.insert(2, f"created_at: {created_date}")
        new_fm_lines.insert(3, f"updated_at: {updated_date}")
        new_fm_lines.insert(4, "security_level: safe_data")

    # tags 行: schema_v12 → schema_v13
    new_fm_lines = [
        ln.replace("schema_v12", "schema_v13") for ln in new_fm_lines
    ]

    # ─── body 改造 ───
    known = _SKILL_KNOWN_TOP if kind == "skill" else _KB_KNOWN_TOP
    sections = _split_body_sections(body, known_titles=known)
    # body 開頭 H1 (# title)
    h1_match = re.match(r"^(# [^\n]+\n)", body)
    h1_line = h1_match.group(1).rstrip("\n") if h1_match else f"# {title}"

    new_body_lines = [h1_line, ""]

    if kind == "skill":
        # ── SKILL 結構 ──
        # # 核心摘要 <summary>: 觸發情境 + 描述 + 核心摘要 + 標籤
        new_body_lines.append("# 核心摘要")
        new_body_lines.append("<summary>")
        for sec_key in ["觸發情境", "描述", "核心摘要", "標籤"]:
            if sec_key in sections:
                new_body_lines.append(f"## {sec_key}")
                new_body_lines.append(sections[sec_key])
                new_body_lines.append("")
        new_body_lines.append("</summary>")
        new_body_lines.append("")

        # # 詳細內容 <context>: 實際打法 + 正確示範 + 完整內容 + 步驟摘要 + 使用邊界 + 範例對話
        new_body_lines.append("# 詳細內容")
        new_body_lines.append("<context>")
        for sec_key in ["實際打法 (可直接複製)", "正確示範", "完整內容", "步驟摘要", "使用邊界", "範例對話"]:
            if sec_key in sections:
                new_body_lines.append(f"## {sec_key}")
                new_body_lines.append(sections[sec_key])
                new_body_lines.append("")
        new_body_lines.append("</context>")
        new_body_lines.append("")

        # # 相關實體與偏好影響: 教學追溯 + 來源 + 應用場景 (從描述)
        new_body_lines.append("# 相關實體與偏好影響")
        new_body_lines.append("")
        desc = sections.get("描述", "")
        if desc:
            # 取第一句當應用場景
            desc_short = desc.split("\n")[0][:200]
            new_body_lines.append(f"- 應用場景: {desc_short}")
            new_body_lines.append("")
        for sec_key in ["教學追溯", "來源 (Origin)"]:
            if sec_key in sections:
                new_body_lines.append(f"## {sec_key}")
                new_body_lines.append(sections[sec_key])
                new_body_lines.append("")

    elif kind == "kb":
        # ── KB 結構 ──
        # # 核心摘要 <summary>: 核心摘要
        new_body_lines.append("# 核心摘要")
        new_body_lines.append("<summary>")
        if "核心摘要" in sections:
            new_body_lines.append(sections["核心摘要"])
        new_body_lines.append("</summary>")
        new_body_lines.append("")

        # # 詳細內容 <context>: 完整內容 + 章節大綱 + 重要原文
        new_body_lines.append("# 詳細內容")
        new_body_lines.append("<context>")
        for sec_key in ["完整內容", "章節大綱", "重要原文"]:
            if sec_key in sections:
                new_body_lines.append(f"## {sec_key}" if sec_key != "完整內容" else "")
                new_body_lines.append(sections[sec_key])
                new_body_lines.append("")
        new_body_lines.append("</context>")
        new_body_lines.append("")

        # # 相關實體與偏好影響
        new_body_lines.append("# 相關實體與偏好影響")
        new_body_lines.append("")
        app = sections.get("觸發情境 / 適用情境", "")
        if app:
            new_body_lines.append(f"- 應用場景: {app}")
        rel = sections.get("相關概念 (雙關聯)", "")
        if rel:
            new_body_lines.append(f"- 關聯概念: {rel}")
        tag = sections.get("標籤", "")
        if tag:
            new_body_lines.append(f"- 標籤: {tag}")
        new_body_lines.append("")
        # 投餵追溯
        if "投餵追溯" in sections:
            new_body_lines.append("## 投餵追溯")
            new_body_lines.append(sections["投餵追溯"])
            new_body_lines.append("")

    # 重組
    out = "---\n" + "\n".join(new_fm_lines) + "\n---\n\n" + "\n".join(new_body_lines).rstrip() + "\n"
    path.write_text(out, encoding="utf-8")
    return True, f"migrated v12 → v13 (backup: {bak.name})"


def main(vault_root: Path) -> None:
    if not vault_root.exists():
        print(f"ERR vault not found: {vault_root}")
        sys.exit(1)

    counts = {"skill_migrated": 0, "skill_skipped": 0, "kb_migrated": 0, "kb_skipped": 0}

    # SKILL: 50_Skills_Tools/54_Taught_Skills/<name>/SKILL.md + _consolidated/<name>/SKILL.md
    sk_root = vault_root / "50_Skills_Tools" / "54_Taught_Skills"
    if sk_root.exists():
        for skill_md in sk_root.rglob("SKILL.md"):
            # 排除 _merged/_archived 歷史殘留
            parts = set(skill_md.parts)
            if "_merged" in parts or "_archived" in parts:
                continue
            ok, msg = _migrate_one_md(skill_md, "skill")
            rel = skill_md.relative_to(vault_root)
            print(f"  SKILL {'✓' if ok else '·'} {rel}  — {msg}")
            if ok:
                counts["skill_migrated"] += 1
            else:
                counts["skill_skipped"] += 1

    # KB: 40_Knowledge_Base/41_Daily_Knowledge/*.md + 42_External_Knowledge/*.md (直屬, 跳 _inbox/_processed)
    kb_root = vault_root / "40_Knowledge_Base"
    if kb_root.exists():
        for kb_md in kb_root.rglob("*.md"):
            parts = set(kb_md.parts)
            if "_inbox" in parts or "_processed" in parts or "_ingest_inbox" in parts:
                continue
            ok, msg = _migrate_one_md(kb_md, "kb")
            rel = kb_md.relative_to(vault_root)
            print(f"  KB    {'✓' if ok else '·'} {rel}  — {msg}")
            if ok:
                counts["kb_migrated"] += 1
            else:
                counts["kb_skipped"] += 1

    print()
    print(f"Summary: SKILL migrated={counts['skill_migrated']} skipped={counts['skill_skipped']}")
    print(f"         KB    migrated={counts['kb_migrated']} skipped={counts['kb_skipped']}")
    print(f"Backups saved as *.bak_v12 alongside originals.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python migrate_v12_to_v13.py <vault_root>")
        sys.exit(2)
    main(Path(sys.argv[1]))
