"""User profile gap analysis — 對話中主動問使用者「你還沒告訴我什麼」(R8 C24).

對應 MISSION §3.7 主動歸納 + 使用者願景「也能幫使用者歸納出已經吸收過的知識」.

核心: 不只 agent 自己進化, 還主動發現「USER.md 有空欄位 / Mid_Term 累積但 USER.md 沒紀錄」
然後對話中提一個問題請使用者補.

策略 (簡單先行, 不依賴 LLM):
1. USER.md 空欄位偵測 — regex 找「（請填寫）」「(待補)」「(尚未設定)」等佔位符
2. Mid_Term entity 不在 USER.md mention — 高頻 entity 但 USER.md 沒提及 → 可能是個重要事實沒被 USER.md 紀錄

寫到: .ai/pending_user_gaps.json
chat_runtime 結尾偶爾貼一個 gap 問題 (per_gap 7 天 cooldown, 1 個 response 最多 1 個)

跟 R7 C20b skill 提議邏輯一致 (per_entity_cooldown_days / auto_dismiss_after_days),
但 footer 文案不同 — 用「？」開頭表示主動提問, 不是「💡」提議.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import LifecycleState
from agent_memory.vault import ObsidianVaultAdapter

PENDING_USER_GAPS_RELATIVE_PATH = ".ai/pending_user_gaps.json"
USER_PROFILE_PATH = "10_Permanent/Profiles/USER.md"
MIDTERM_DIR = "10_Permanent/Mid_Term"

# USER.md 佔位符 pattern (簡單 regex 偵測「未填」)
_PLACEHOLDER_RES = [
    re.compile(r"[（(]\s*請填寫\s*[)）]"),
    re.compile(r"[（(]\s*待補\s*[)）]"),
    re.compile(r"[（(]\s*尚未設定\s*[)）]"),
    re.compile(r"[（(]\s*未填\s*[)）]"),
    re.compile(r"\[填入.*?\]"),
    re.compile(r"\[TODO.*?\]", re.IGNORECASE),
]

# 偵測 USER.md 內 section heading 開頭 (用 `## ` 抓 section, 給 gap 上下文)
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Gap intent parse — 使用者回應 "稍後" / "跳過" / "不要" → dismiss
_DISMISS_RE = re.compile(r"^(?:稍後|跳過|不要|別問|不|skip|later|no)", re.IGNORECASE)
_MAX_DISMISS_INPUT_LEN = 10  # 同 skill_suggestions, 短輸入限制


def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ─── IO ──────────────────────────────────────────────────────────────────────


def load_pending_gaps(vault_root: Path) -> list[dict[str, Any]]:
    root = Path(vault_root).expanduser().resolve()
    path = root / PENDING_USER_GAPS_RELATIVE_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    except Exception:  # noqa: BLE001
        return []


def save_pending_gaps(vault_root: Path, gaps: list[dict[str, Any]]) -> None:
    root = Path(vault_root).expanduser().resolve()
    path = root / PENDING_USER_GAPS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, timeout=5.0):
        atomic_write(path, json.dumps(gaps, ensure_ascii=False, indent=2) + "\n")


# ─── Scan: USER.md placeholders + Mid_Term entity mismatch ──────────────────


def _scan_user_placeholders(user_body: str) -> list[dict[str, Any]]:
    """從 USER.md body 抓「（請填寫）」之類佔位 + 上下文 section."""

    findings: list[dict[str, Any]] = []
    lines = user_body.split("\n")
    current_section = ""
    for idx, line in enumerate(lines):
        m_sec = _SECTION_HEADING_RE.match(line)
        if m_sec:
            current_section = m_sec.group(1).strip()
            continue
        for placeholder_re in _PLACEHOLDER_RES:
            if placeholder_re.search(line):
                # 用 section + 行內 prefix 當 gap key (例如「個人簡介 - 偏好稱呼」)
                line_text = line.strip()
                # 移除 markdown list 前綴 - / *
                cleaned = re.sub(r"^[-*+]\s+", "", line_text)
                # 用「:」「：」「－」找 label
                label_match = re.match(r"(.+?)\s*[:：－]\s*", cleaned)
                gap_label = label_match.group(1).strip() if label_match else cleaned[:30]
                findings.append({
                    "kind": "placeholder",
                    "section": current_section,
                    "label": gap_label,
                    "line_no": idx + 1,
                    "raw_line": line_text[:100],
                    "gap_id": f"placeholder:{current_section}:{gap_label}",
                })
                break
    return findings


def _scan_midterm_vs_user(adapter: ObsidianVaultAdapter, user_body: str, min_mention: int = 3) -> list[dict[str, Any]]:
    """高頻 Mid_Term entity 但 USER.md 沒提及 → 可能是個重要事實."""

    findings: list[dict[str, Any]] = []
    mid_dir = adapter.vault_root / MIDTERM_DIR
    if not mid_dir.exists():
        return findings
    user_lower = user_body.lower()
    for path_obj in sorted(mid_dir.glob("*.md")):
        if path_obj.name.startswith("_"):
            continue
        eid = path_obj.stem
        # 跳過 umbrella 本身, 看子節點即可
        rel = str(path_obj.relative_to(adapter.vault_root)).replace("\\", "/")
        note = adapter.read_note(rel)
        if note is None or note.frontmatter.pinned:
            continue
        if "umbrella" in note.frontmatter.tags:
            continue
        if note.frontmatter.lifecycle_state != LifecycleState.MID:
            continue
        if note.frontmatter.mention_count < min_mention:
            continue
        # USER.md 內含 entity_id 或 aliases → 已記過, skip
        if eid.lower() in user_lower:
            continue
        if any(alias.lower() in user_lower for alias in (note.frontmatter.aliases or [])):
            continue
        findings.append({
            "kind": "midterm_not_in_user",
            "entity_id": eid,
            "mention_count": note.frontmatter.mention_count,
            "source_path": rel,
            "gap_id": f"midterm_not_in_user:{eid}",
        })
    return findings


def scan_user_gaps(
    vault_root: Path,
    *,
    cooldown_days: int = 7,
    min_midterm_mention: int = 3,
) -> dict[str, Any]:
    """Scan USER.md + Mid_Term → append 到 pending_user_gaps.json.

    Skip:
        - 同 gap_id 7 天 cooldown 內已 proposed / dismissed
    """

    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    user_note = adapter.read_note(USER_PROFILE_PATH)
    pending = load_pending_gaps(root)
    now = datetime.now().astimezone()
    cooldown = timedelta(days=cooldown_days)

    findings: list[dict[str, Any]] = []
    if user_note is not None:
        findings.extend(_scan_user_placeholders(user_note.body))
        findings.extend(_scan_midterm_vs_user(adapter, user_note.body, min_mention=min_midterm_mention))
    else:
        # USER.md 不存在本身就是 gap
        findings.append({
            "kind": "user_md_missing",
            "gap_id": "user_md_missing:root",
            "section": "(root)",
            "label": "USER.md 不存在",
        })

    by_id: dict[str, dict[str, Any]] = {s.get("gap_id", ""): s for s in pending if s.get("gap_id")}
    new_added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for f in findings:
        gid = f.get("gap_id", "")
        if not gid:
            continue
        existing = by_id.get(gid)
        if existing:
            proposed_at = _parse_iso(existing.get("proposed_at"))
            dismissed_at = _parse_iso(existing.get("dismissed_at"))
            resolved = existing.get("resolved_at")
            if resolved:
                skipped.append({"gap_id": gid, "reason": "already_resolved"})
                continue
            if dismissed_at and (now - dismissed_at) < cooldown:
                skipped.append({"gap_id": gid, "reason": "dismissed_in_cooldown"})
                continue
            if proposed_at and not dismissed_at and (now - proposed_at) < cooldown:
                skipped.append({"gap_id": gid, "reason": "pending_in_cooldown"})
                continue

        entry = dict(f)
        entry["proposed_at"] = now.isoformat()
        entry["dismissed_at"] = None
        entry["resolved_at"] = None
        if existing:
            for i, s in enumerate(pending):
                if s.get("gap_id") == gid:
                    pending[i] = entry
                    break
        else:
            pending.append(entry)
        new_added.append(entry)

    save_pending_gaps(root, pending)
    return {"new_added": new_added, "skipped": skipped, "total_pending": len(pending)}


# ─── chat_runtime hook: pick + parse + record ────────────────────────────────


def pick_next_gap(
    vault_root: Path,
    *,
    auto_dismiss_days: int = 14,
) -> Optional[dict[str, Any]]:
    """挑下一個未 resolved / 未 dismissed gap 給 chat_runtime 貼 footer."""

    root = Path(vault_root).expanduser().resolve()
    pending = load_pending_gaps(root)
    if not pending:
        return None
    now = datetime.now().astimezone()
    cutoff = timedelta(days=auto_dismiss_days)
    changed = False
    selected: dict[str, Any] | None = None

    for g in pending:
        if g.get("resolved_at") or g.get("dismissed_at"):
            continue
        proposed_at = _parse_iso(g.get("proposed_at"))
        if proposed_at and (now - proposed_at) > cutoff:
            g["dismissed_at"] = now.isoformat()
            g["dismiss_reason"] = "auto_timeout"
            changed = True
            continue
        if selected is None:
            selected = g

    if changed:
        save_pending_gaps(root, pending)
    return selected


def parse_gap_intent(text: str) -> str:
    """Parse 使用者回應 → 'dismiss' / 'none' / (其他視為 answer).

    這跟 skill_suggestions 不一樣: 我們不對「答案」做 parse,
    答案的 LLM 處理交給對話本身 (agent 看到使用者回答自然會用 [TOOL]memory 寫進 USER.md).

    這個 parse 只判斷 dismiss intent (使用者明確說「稍後/跳過/不要問」).
    """

    if not text:
        return "none"
    stripped = text.strip()
    if len(stripped) > _MAX_DISMISS_INPUT_LEN:
        return "none"
    if _DISMISS_RE.match(stripped):
        return "dismiss"
    return "none"


def dismiss_gap(vault_root: Path, *, gap_id: str, reason: str = "user_dismissed") -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    pending = load_pending_gaps(root)
    now = datetime.now().astimezone()
    for g in pending:
        if g.get("gap_id") != gap_id:
            continue
        g["dismissed_at"] = now.isoformat()
        g["dismiss_reason"] = reason
        save_pending_gaps(root, pending)
        return {"action": "dismissed", "gap_id": gap_id}
    return {"action": "not_found", "gap_id": gap_id}


def mark_gap_resolved(vault_root: Path, *, gap_id: str) -> dict[str, Any]:
    """當 agent / curator 偵測到使用者已回答 (USER.md 被更新 / Mid_Term entity 已寫入 USER.md) 標 resolved.

    R8 C24 第一版不自動偵測 resolved — 留給未來 curator 自動 audit.
    本函式給 caller 主動標用.
    """

    root = Path(vault_root).expanduser().resolve()
    pending = load_pending_gaps(root)
    now = datetime.now().astimezone()
    for g in pending:
        if g.get("gap_id") != gap_id:
            continue
        g["resolved_at"] = now.isoformat()
        save_pending_gaps(root, pending)
        return {"action": "resolved", "gap_id": gap_id}
    return {"action": "not_found", "gap_id": gap_id}


def build_gap_footer(gap: dict[str, Any]) -> str:
    """Build chat response 末端的 gap 問題 footer."""

    kind = gap.get("kind", "")
    if kind == "placeholder":
        section = gap.get("section", "")
        label = gap.get("label", "")
        return (
            "\n\n---\n"
            f"❓ 我注意到 USER.md 的「{section} / {label}」還沒填.\n"
            f"  可以告訴我你的{label}嗎? (我會自動 [TOOL]memory 寫進 USER.md)\n"
            f"  或回「稍後」之後再問."
        )
    if kind == "midterm_not_in_user":
        eid = gap.get("entity_id", "")
        mc = gap.get("mention_count", 0)
        return (
            "\n\n---\n"
            f"❓ 我們最近對話常提到「{eid}」({mc} 次了) 但你的 USER.md 沒紀錄,\n"
            f"  要不要告訴我這對你來說是什麼? 我可以寫進 USER.md baseline.\n"
            f"  或回「稍後」之後再問."
        )
    if kind == "user_md_missing":
        return (
            "\n\n---\n"
            "❓ USER.md 還沒建立, 你願意先告訴我一些基本資訊嗎?\n"
            "  (偏好稱呼 / 主要身份 / 使用語言 / 回覆語氣 / 偏好工具)"
        )
    # 預設
    return (
        "\n\n---\n"
        f"❓ 我發現一個 gap: {gap.get('label', gap.get('entity_id', gap.get('gap_id', '')))}.\n"
        f"  要不要補一下? 或回「稍後」."
    )
