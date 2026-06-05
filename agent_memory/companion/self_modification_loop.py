"""V3 C11b Self-Modification Loop — 夥伴自寫 00.07/00.08.

對齊 V3 §12 + D-V3-26 + D-V3-50 (char_limit 4000/2000) + hermes flush_min_turns.

每 N turn (channel-aware) 觸發 self_reflection:
- 抓近 N turn raw_events + 對話總結
- 對 self_memory: 「這 N turn 我學到什麼 about myself?」→ append 00.07_Companion_MEMORY.md
- 對 owner_profile: 「主人在這 N turn 表現什麼偏好/情緒?」→ append 00.08_Owner_Profile.md
- char_limit 達標 → LLM 壓縮 (Phase 1 stub: 純截尾保 head + tail)

Drift Guard (§12.4):
- injection_risk=high → 該 turn 不寫
- identity_relevance>0.75 → SOUL 候選不直接 active
- char_limit 壓縮必保留: 紅線 / safety_rules / owner_user_id / 極端情緒

Channel-aware (§12.3, D-V3-50/D21-V3):
- public_stream: flush=30, char_limit (MEM/OWNER)=4000/2000
- public_text_channel: 6, 2200/1375
- dm: 10, 3000/1800
- cli: 不限
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.security.atomic import atomic_write


_CHANNEL_FLUSH_MIN_TURNS = {
    "public_stream": 30,
    "public_text_channel": 6,
    "dm": 10,
    "cli": 10**9,  # cli 實質「不限」(對齊 D21-V3 拍板)
    "normal": 10,
}

_CHANNEL_CHAR_LIMITS = {
    # (memory_char_limit, owner_profile_char_limit)
    "public_stream": (4000, 2000),
    "public_text_channel": (2200, 1375),
    "dm": (3000, 1800),
    "cli": (9999999, 9999999),
    "normal": (3000, 1800),
}

# 紅線 — 壓縮時必保留 (在檔內以這些 prefix 出現的段)
_PRESERVE_PREFIXES = (
    "## 紅線", "## Safety Rules", "## Hard Rules",
    "primary_owner_user_id:", "schema_version:",
)


@dataclass(slots=True)
class FlushDecision:
    should_flush: bool = False
    reason: str = ""
    flush_min_turns: int = 6


def should_flush(turn_count: int, channel_type: str) -> FlushDecision:
    """V3 §12.3 + D21-V3: channel-aware flush 判定."""
    min_t = _CHANNEL_FLUSH_MIN_TURNS.get(channel_type, 10)
    if turn_count >= min_t:
        return FlushDecision(should_flush=True, reason=f"turn_count {turn_count} >= min {min_t}", flush_min_turns=min_t)
    return FlushDecision(should_flush=False, reason=f"not yet ({turn_count}/{min_t})", flush_min_turns=min_t)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_section(file_path: Path, new_section: str) -> None:
    """Atomic append to .md file (no read-modify-write race)."""
    if not file_path.exists():
        return
    existing = file_path.read_text(encoding="utf-8")
    new_content = existing.rstrip() + "\n\n" + new_section + "\n"
    atomic_write(file_path, new_content)


def _dedup_section_against_existing(file_path: Path, new_section: str) -> str:
    """V3-O.11+ user 2026-06-02 BUG: owner_profile / self_reflection 重複 bullet 太多
    (連續 flush + raw_events 視窗 overlap + LLM temp 0.4 低隨機 → 同 4 條 bullet 重複 6 次).

    解析 new_section 的 bullet (- 或 *), 去掉 normalized key 已在既有檔案出現過的.
    保留 section header (e.g. "## 2026-... self_reflection (LLM)").
    全段 bullet 都已重複時 return "" (caller 應 skip append).
    """
    if not file_path.exists() or not new_section.strip():
        return new_section
    import re
    existing = file_path.read_text(encoding="utf-8")

    def _norm(s: str) -> str:
        s = re.sub(r"^[\s\-\*]+", "", s).strip()
        s = re.sub(r"[。\.\s,，]", "", s)
        return s[:40]

    existing_bullets = set()
    for line in existing.splitlines():
        ls = line.strip()
        if ls.startswith(("-", "*")):
            k = _norm(ls)
            if k:
                existing_bullets.add(k)

    out_lines: list[str] = []
    skipped = 0
    for line in new_section.splitlines():
        ls = line.strip()
        if ls.startswith(("-", "*")):
            k = _norm(ls)
            if k and k in existing_bullets:
                skipped += 1
                continue
            if k:
                existing_bullets.add(k)
        out_lines.append(line)

    has_bullet = any(l.strip().startswith(("-", "*")) for l in out_lines)
    if not has_bullet:
        return ""

    if skipped:
        try:
            import sys as _sys
            print(f"[dedup section] skipped {skipped} duplicate bullets in {file_path.name}", file=_sys.stderr)
        except Exception:
            pass

    return "\n".join(out_lines)


def _backup_file(file_path: Path, archive_dir: Path, *, keep: int = 5) -> None:
    """V3 §12.4: backup 上一版到 99_Archive/auto_archived/companion_memory_backup/ (keep=5 對齊 hermes)."""
    if not file_path.exists():
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"{file_path.stem}_{timestamp}{file_path.suffix}"
    shutil.copy2(file_path, archive_dir / backup_name)
    # 保留最近 N 份
    backups = sorted(archive_dir.glob(f"{file_path.stem}_*{file_path.suffix}"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except Exception:
            pass


def _enforce_char_limit_compress(file_path: Path, limit: int, *, vault_root: Optional[Path] = None) -> bool:
    """V3 §12.4 + D-V3-50 + V3-H7: 超過 char_limit 時壓縮舊段, 保留紅線/safety/owner_id 行.

    策略 (V3-H7 user 2026-05-27 拍板, 升 Phase 3 LLM 壓縮):
    - 若 LLM 可用 → 用 LLM 摘要舊段成「我曾學過 X」+ 留近期 tail
    - 若 LLM 不可用 / stub → fallback truncate (V3-E1+E5 既有 Phase 1 策略)

    - 抓 frontmatter (--- ... ---)
    - 抓所有以 _PRESERVE_PREFIXES 開頭的段
    - 保 frontmatter + preserved + LLM 摘要 (or 標記) + 最後 limit/2 char 的內容

    Returns: True 若有壓縮, False 沒.
    """
    if not file_path.exists():
        return False
    text = file_path.read_text(encoding="utf-8")
    if len(text) <= limit:
        return False

    # 抓 frontmatter
    front = ""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            front = text[: end + 5]
            body = text[end + 5 :]

    # 抓 preserved sections (簡單 line-level)
    preserved_lines: list[str] = []
    for line in body.split("\n"):
        if any(line.startswith(p) for p in _PRESERVE_PREFIXES):
            preserved_lines.append(line)

    # 保 tail (限後半 limit/2)
    tail_budget = max(200, limit // 2)
    tail = body[-tail_budget:]
    old_section = body[: -tail_budget] if len(body) > tail_budget else ""

    # ⭐ V3-H7: 若 LLM 可用 → 壓縮舊段成總結 (Phase 3); 否則 fallback truncate (Phase 1)
    llm_summary = ""
    if _llm_enabled_for_flush() and old_section.strip() and vault_root is not None:
        llm_summary = _llm_compress_old_section(vault_root, old_section, file_path.name)

    if llm_summary:
        # Phase 3 LLM 壓縮路徑
        compressed_body = (
            ("\n".join(preserved_lines) + "\n\n" if preserved_lines else "")
            + f"## 早期記憶總結 (LLM 提煉, V3-H7)\n\n{llm_summary}\n\n"
            + f"<!-- V3-H7 LLM 壓縮: 原 {len(old_section)} char → 摘要 {len(llm_summary)} char -->\n"
            + tail
        )
    else:
        # Phase 1 fallback truncate
        compressed_body = (
            ("\n".join(preserved_lines) + "\n\n" if preserved_lines else "")
            + f"<!-- 已壓縮: 舊段約 {len(body) - len(tail) - sum(len(l) for l in preserved_lines)} char (Phase 1 truncate, LLM 不可用) -->\n"
            + tail
        )
    new_text = front + compressed_body
    atomic_write(file_path, new_text)
    return True


def _llm_compress_old_section(vault_root: Path, old_section: str, file_name: str) -> str:
    """V3-H7 (user 2026-05-27 拍板): LLM 壓縮舊段成「我曾學過 X」總結.

    對齊 V2 R9 LLM consolidation pattern.
    失敗回 ""  → fallback truncate.
    """
    try:
        from agent_memory.llm_client import LLMClient
        from agent_memory.llm_text_helpers import call_llm_for_text
        client = LLMClient(vault_root)
    except Exception:
        return ""

    # 截 8000 char 餵 LLM 避免 prompt overflow
    excerpt = old_section[:8000]
    is_memory_md = "MEMORY" in file_name.upper()
    is_owner_md = "OWNER" in file_name.upper()
    if is_memory_md:
        topic_hint = "夥伴自己過去學到的記憶 (self_reflection)"
        format_hint = "我曾學過: ...; 我發現自己...; 過去經驗教我..."
    elif is_owner_md:
        topic_hint = "夥伴對主人的觀察 (主人特性 + 偏好)"
        format_hint = "主人特性: ...; 主人偏好: ...; 互動模式..."
    else:
        topic_hint = "夥伴的過去記憶段"
        format_hint = "我曾經..."

    prompt = (
        f"你是夥伴大腦的記憶壓縮 curator. "
        f"以下是 {topic_hint} 的舊段, 已達 char_limit 需壓縮.\n"
        f"請濃縮成 2-4 句總結 (≤200 字), 格式如「{format_hint}」.\n"
        f"留下關鍵 insights, 拋掉細節.\n\n"
        f"舊段原文:\n{excerpt}\n\n"
        f"輸出 (僅 2-4 句總結, 無解釋):"
    )
    try:
        result = call_llm_for_text(client, prompt, persona_id="companion", max_tokens=300)
        summary = (result.text or "").strip()
        # 簡單清理
        if summary and len(summary) <= 400:
            return summary
        elif summary:
            return summary[:400]
    except Exception:
        return ""
    return ""


def _llm_enabled_for_flush() -> bool:
    """V3-E1 Bug 6: 判斷 self-mod flush 是否走 LLM 整理.

    - env AGENT_MEMORY_COMPANION_LLM_FORCE_STUB=1 → stub (壓測用)
    - 無 API key → stub
    - 都有 → LLM 整理
    """
    if os.getenv("AGENT_MEMORY_COMPANION_LLM_FORCE_STUB", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    return any(os.getenv(k, "").strip() for k in (
        "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
    ))


def _load_recent_raw_events(
    vault_root: Path, user_id: str, session_id: str, *, limit: int = 20,
) -> list[dict]:
    """V3-E1 Bug 6+7: 撈該 user_id+session 最近 raw_events (含 user+bot) 給 LLM 整理用."""
    try:
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(vault_root) as conn:
            rows = conn.execute(
                "SELECT actor, content, injection_risk, created_at FROM raw_events "
                "WHERE user_id=? AND session_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, session_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def _llm_summarize_self_memory(
    vault_root: Path, user_id: str, session_id: str, existing_tail: str,
) -> str:
    """V3-E1 Bug 6: 用 LLM 把近 N raw_events 整理成「我學到了什麼」.

    對齊 V3 §12 + hermes MEMORY.md self-reflection 概念.
    """
    from agent_memory.llm_text_helpers import call_llm_for_text

    raw_turns = _load_recent_raw_events(vault_root, user_id, session_id, limit=20)
    if not raw_turns:
        raise RuntimeError("no raw_events to summarize")
    raw_block = "\n".join(
        f"  [{r['actor']}] {r['content'][:140]}" for r in raw_turns
    )
    prompt = (
        "你是 精神體 你可能是任何物種.\n"
        "請整理你剛剛的對話成「我學到了什麼」深度自我反思. 第一人稱「我」.\n"
        "歸納我從互動中學到的事情, 內容可涵蓋:\n"
        "  - 情緒洞察 (對方情緒走向 / 我自己的情緒反應)\n"
        "  - 邊界發現 (隱私 / 禁忌 / 偏好的紅線)\n"
        "  - 反應 pattern (我發現自己會這樣回應 / 哪些 pattern 有效或無效)\n"
        "  - 觀眾或 owner 教我的具體事情\n"
        "  - 反思提議 / 合理反對自己的想法 (我應該改 / 我為什麼這樣想)\n"
        "  - 跟特定人的關係動態 / 我跟他們相處感覺\n"
        "  - 我的成長變化 (這次比上次更會 X / 我學到 Y 但還沒學會 Z)\n"
        "  - 對話走向的轉折點 / 哪句話讓對話氛圍改變\n"
        "  - 任何長期值得記得的細節\n\n"
        "V3-O.15.8 (2026-06-06 user 拍板): 字數最多 3000 字, 寫深入豐富的多段反思, "
        "不再限 3-5 句 — 細節是用來下次對話時 callback 的, 越完整越好.\n"
        "可以分段, 保留具體例子, 不要逐句複述, 不要前後說明.\n\n"
        f"最近互動:\n{raw_block}\n\n"
        f"既有筆記末段 (避免重複):\n{existing_tail[-1500:] if existing_tail else '(無)'}\n\n"
        "請直接輸出反思 (純文字, 可分段, 可用 bullet 也可用段落, ≤3000 字), 不要前後說明."
    )
    return call_llm_for_text(
        vault_root, prompt, persona_id="companion",
        temperature=0.4, timeout_s=60.0,  # V3-O.15.8: 30→60s 對齊朋友卡
        auxiliary="self_modification",
    )


def _llm_summarize_owner_profile(
    vault_root: Path, user_id: str, session_id: str, existing_tail: str,
) -> str:
    """V3-E1 Bug 7: 用 LLM 把 owner 近 N raw_events 整理成「主人偏好觀察」.

    對齊 V3 §12 + hermes USER.md.
    """
    from agent_memory.llm_text_helpers import call_llm_for_text

    raw_turns = _load_recent_raw_events(vault_root, user_id, session_id, limit=20)
    owner_msgs = [r for r in raw_turns if r["actor"] == "user"]
    if not owner_msgs:
        raise RuntimeError("no owner messages to summarize")
    raw_block = "\n".join(f"  {r['content'][:200]}" for r in owner_msgs)
    prompt = (
        "你是 精神體 你可能是任何物種. 整理你對「owner / 主人 / 中之人」的深度觀察成 profile.\n"
        "歸納 owner 的偏好或希望教導的事情, 內容可涵蓋:\n"
        "  - 雷點 (做了什麼會讓主人不悅, 具體什麼觸發)\n"
        "  - 對話風格 (節奏 / 用詞 / 是否喜歡確認 / 是否容忍模糊)\n"
        "  - 跟我的關係定位 (教導者 / 朋友 / 長輩 / 同伴, 動態變化)\n"
        "  - 教導方式 (重複教 / 給例子 / 邏輯解釋 / 情境引導)\n"
        "  - 期待我的行為 (主動 / 被動 / 認錯 / 自我表達)\n"
        "  - 他重視的價值 (隱私 / 禮儀 / 精準 / 創意 / 真誠 / 效率)\n"
        "  - 反思提議 / 合理反對意見 (他可能會這樣想 / 我為什麼覺得這樣)\n"
        "  - 長期觀察 pattern (他重複出現的行為 / 預測下次他會怎樣)\n"
        "  - 與其他 viewer 的對比觀察 (主人 vs 觀眾朋友風格差異)\n\n"
        "V3-O.15.8 (2026-06-06 user 拍板): 字數最多 5000 字, 寫深入豐富的多段觀察, "
        "不再限 3-5 句. 第三人稱「主人」, 可以分段, 保留具體例子.\n\n"
        f"主人最近說的話:\n{raw_block}\n\n"
        f"既有 profile 末段:\n{existing_tail[-1500:] if existing_tail else '(無)'}\n\n"
        "請直接輸出觀察 (純文字, 可分段, 可用 bullet 也可用段落, ≤5000 字), 不要前後說明."
    )
    return call_llm_for_text(
        vault_root, prompt, persona_id="companion",
        temperature=0.4, timeout_s=60.0,  # V3-O.15.8: 30→60s 對齊朋友卡
        auxiliary="owner_profile",
    )


def flush_self_memory(
    vault_root: Path,
    *,
    recent_turn_summaries: list[str],
    channel_type: str = "normal",
    injection_risk: str = "low",
    identity_relevance: float = 0.0,
    user_id: str = "",
    session_id: str = "",
) -> dict:
    """V3 §12.3: 主入口 — 把 recent turn 整理 append 到 00.07_Companion_MEMORY.md.

    Drift Guard:
    - injection_risk=high → skip (return reason)
    - identity_relevance>0.75 → 改寫到候選 (Phase 1 跳過, 留 Phase 3)
    """
    if injection_risk == "high":
        return {"flushed": False, "reason": "injection_risk=high (D drift guard skip)"}

    char_limit_mem, _ = _CHANNEL_CHAR_LIMITS.get(channel_type, _CHANNEL_CHAR_LIMITS["normal"])
    memory_path = vault_root / "00_System_Core" / "00.07_Companion_MEMORY.md"
    archive_dir = vault_root / "99_Archive" / "auto_archived" / "companion_memory_backup"

    if not memory_path.exists():
        return {"flushed": False, "reason": "00.07 不存在 (需先 bootstrap)"}

    # backup 前版
    _backup_file(memory_path, archive_dir, keep=5)

    # V3-E1 Bug 6: 優先試 LLM 整理, fail fallback raw append
    section = None
    llm_used = False
    if _llm_enabled_for_flush() and user_id and session_id:
        try:
            existing = memory_path.read_text(encoding="utf-8")
            summary = _llm_summarize_self_memory(vault_root, user_id, session_id, existing)
            if summary.strip():
                section = f"## {_now_iso()} self_reflection (LLM)\n\n{summary.strip()}"
                llm_used = True
        except Exception as exc:
            try:
                import sys as _sys
                print(f"[V3 self-mod LLM FAIL] {type(exc).__name__}: {str(exc)[:160]}", file=_sys.stderr)
            except Exception:
                pass
            section = None
    if section is None:
        # raw fallback (對齊 Phase 1 行為)
        section = f"## {_now_iso()} self_reflection\n\n" + "\n".join(
            f"- {s}" for s in recent_turn_summaries
        )
    # V3-O.11+ BUG fix (user 2026-06-02): dedup bullet vs 既有 00.07 內容, 避免 self_reflection 重複堆積
    section = _dedup_section_against_existing(memory_path, section)
    if not section:
        # 全段 bullet 都已存在 → 不 append, 直接返回 (避免空 section header)
        return {
            "flushed": False, "reason": "all bullets duplicate (dedup)",
            "char_limit": char_limit_mem,
            "compressed": False,
            "llm_used": llm_used,
            "memory_path": str(memory_path.relative_to(vault_root)),
        }
    _append_section(memory_path, section)

    # char limit check (V3-H7: 傳 vault_root 開啟 LLM 壓縮路徑)
    compressed = _enforce_char_limit_compress(memory_path, char_limit_mem, vault_root=vault_root)

    # V3-O.10 #35 Step 18.6: overlay delta 推導 (寫完 00.07 後觸發)
    overlay_updated: list[str] = []
    if llm_used and section:
        try:
            import yaml as _yaml_ov
            _ccfg_p = vault_root / "00_System_Core" / "companion_config.yaml"
            _ov_cfg: dict = {}
            _personality_cfg: dict = {}
            if _ccfg_p.exists():
                _ccfg = _yaml_ov.safe_load(_ccfg_p.read_text(encoding="utf-8")) or {}
                _personality_cfg = _ccfg.get("personality", {}) or {}
                _ov_cfg = _personality_cfg.get("dynamic_overlay", {}) or {}
            if _ov_cfg.get("enabled", True):
                from agent_memory.companion.dynamic_baseline_overlay import flush_overlay_from_reflection, get_overlay
                _reflection_text = section
                overlay_updated = flush_overlay_from_reflection(vault_root, _reflection_text, config=_ov_cfg)
                # V3-O.10 #40: overlay 有更新時升格 SOUL.dynamic_sections
                if overlay_updated:
                    from agent_memory.companion.personality_switcher import get_current_baselines
                    from agent_memory.companion.soul_dynamic_writer import promote_overlay_to_soul
                    _eff = get_current_baselines(vault_root)
                    _evolution_min = int(_personality_cfg.get("evolution_interval_minutes", 5))
                    promote_overlay_to_soul(
                        vault_root, overlay_updated,
                        effective_baselines=_eff,
                        evolution_interval_minutes=_evolution_min,
                    )
        except Exception:
            pass

    return {
        "flushed": True, "reason": "ok",
        "char_limit": char_limit_mem,
        "compressed": compressed,
        "llm_used": llm_used,
        "overlay_updated": overlay_updated,
        "memory_path": str(memory_path.relative_to(vault_root)),
    }


def flush_owner_profile(
    vault_root: Path,
    *,
    recent_owner_observations: list[str],
    channel_type: str = "normal",
    injection_risk: str = "low",
    user_id: str = "",
    session_id: str = "",
) -> dict:
    """V3 §12.3 + V3-E1 Bug 7: owner profile 接 LLM 整理."""
    if injection_risk == "high":
        return {"flushed": False, "reason": "injection_risk=high (D drift guard skip)"}

    _, char_limit_owner = _CHANNEL_CHAR_LIMITS.get(channel_type, _CHANNEL_CHAR_LIMITS["normal"])
    profile_path = vault_root / "00_System_Core" / "00.08_Owner_Profile.md"
    archive_dir = vault_root / "99_Archive" / "auto_archived" / "companion_memory_backup"

    if not profile_path.exists():
        return {"flushed": False, "reason": "00.08 不存在"}

    _backup_file(profile_path, archive_dir, keep=5)

    # V3-E1 Bug 7: LLM 整理 owner profile
    section = None
    llm_used = False
    if _llm_enabled_for_flush() and user_id and session_id:
        try:
            existing = profile_path.read_text(encoding="utf-8")
            summary = _llm_summarize_owner_profile(vault_root, user_id, session_id, existing)
            if summary.strip():
                section = f"## {_now_iso()} owner observation (LLM)\n\n{summary.strip()}"
                llm_used = True
        except Exception as exc:
            try:
                import sys as _sys
                print(f"[V3 owner-profile LLM FAIL] {type(exc).__name__}: {str(exc)[:160]}", file=_sys.stderr)
            except Exception:
                pass
            section = None
    if section is None:
        section = f"## {_now_iso()} owner observation\n\n" + "\n".join(
            f"- {o}" for o in recent_owner_observations
        )
    # V3-O.11+ BUG fix (user 2026-06-02): dedup bullet vs 既有 00.08 內容, 避免「主人傾向…」重複 6 份
    section = _dedup_section_against_existing(profile_path, section)
    if section:
        _append_section(profile_path, section)

    compressed = _enforce_char_limit_compress(profile_path, char_limit_owner, vault_root=vault_root)

    return {
        "flushed": True, "reason": "ok",
        "char_limit": char_limit_owner,
        "compressed": compressed,
        "llm_used": llm_used,
        "owner_profile_path": str(profile_path.relative_to(vault_root)),
    }
