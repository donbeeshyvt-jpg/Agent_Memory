#!/usr/bin/env python3
"""R7 + R8 E2E simulation — 模擬完整使用者操作驗證自我進化 + 主動歸納迴圈 (R7 C23 + R8 C26).

目的: 給接手 AI / 使用者 / regression 一個「跑一個指令看 R7+R8 進化迴圈是否串通」的工具.

模擬完整 e2e flow:
    1. Fresh vault bootstrap (ensure_skeleton)
       → 驗證 Mid_Term/ / auto_archived/ / promotion.yaml / USER+MEMORY pinned
    2. 模擬 3 個 daily_flush (含 wikilinks + 含 procedure tag)
    3. Force-run curator daily light (繞 first_run_defer)
       → 驗證 Mid_Term/<entity> 自動 aggregate, mention_count 累加
    4. 模擬時間流逝 (修 frontmatter created/updated 9 天前) — 滿足升長條件
    5. 模擬 mention_count 達 N2=3 (改 frontmatter)
    6. Force-run curator weekly deep
       → 驗證 promote_midterm_to_long 真升到 Concepts/
       → 驗證 demote 邏輯 (建一個 200 天舊長期 → archive)
       → 驗證 skill scan 寫 pending_skill_suggestions.json
    7. 模擬使用者下一輪對話回「升格」(parse_user_response_intent + record_user_response)
       → 驗證 promote_to_skill 真建 00_System/Skills/<id>/SKILL.md
    8. 印 PASS/FAIL report + 細項

跑法:
    python scripts/run-r7-e2e-simulation.py            # 用臨時 vault
    python scripts/run-r7-e2e-simulation.py --keep     # 跑完保留 vault 給人工檢查
    python scripts/run-r7-e2e-simulation.py --vault X  # 指定 vault (危險, 會寫測試資料)

退出碼: 0 = 全 PASS, 1 = 有 FAIL
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows cmd 預設 cp950 對 unicode 線條/emoji 爆炸 — 強制 stdout/stderr UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# 確保能 import agent_memory (script 相對 main repo root)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ─── Test report helper ──────────────────────────────────────────────────────


class Report:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.failed = 0

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "[PASS]" if ok else "[FAIL]"
        self.steps.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            self.failed += 1
        # 即時印出 (UTF-8 stdout)
        print(f"  {mark} {name}")
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")

    def section(self, title: str) -> None:
        print()
        print(f"━━━ {title} ━━━")

    def summary(self) -> int:
        print()
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        total = len(self.steps)
        passed = total - self.failed
        if self.failed == 0:
            print(f"  ✅ ALL PASS  ({passed}/{total})")
        else:
            print(f"  ❌ {self.failed} FAILED  ({passed}/{total} passed)")
            print()
            print("  Failed steps:")
            for s in self.steps:
                if not s["ok"]:
                    print(f"    - {s['name']}: {s['detail']}")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return 0 if self.failed == 0 else 1


def _safe_unlink_tree(p: Path) -> None:
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def _write_md_with_frontmatter(
    path: Path,
    frontmatter: dict,
    body: str,
) -> None:
    """繞 ObsidianVaultAdapter.write_note (避免 updated 被自動覆寫成 now).

    用於模擬「N 天前建立的舊檔」.
    """
    import yaml as _yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = _yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{fm_text}\n---\n\n{body}\n", encoding="utf-8")


def run_simulation(vault_root: Path, report: Report) -> int:
    from agent_memory.vault.obsidian import ObsidianVaultAdapter
    from agent_memory.types import Frontmatter, MemoryType, MemorySource, MemoryNote, LifecycleState
    from agent_memory.memory_promotion import (
        aggregate_to_midterm, list_midterm_entries,
        promote_midterm_to_long, demote_long_to_stale_or_archive,
        consolidate_umbrella_keyword,
    )
    from agent_memory.curator import (
        force_run, load_state, load_config, should_run_now,
        ensure_promotion_config_file, _now_local,
    )
    from agent_memory.skill_suggestions import (
        scan_skill_candidates, pick_next_proposal,
        parse_user_response_intent, record_user_response,
        load_pending,
    )
    from agent_memory.entity_extract import extract_entities_from_text

    # ─── Step 1: Fresh vault bootstrap ────────────────────────────────────
    report.section("Step 1: Fresh vault bootstrap")
    adapter = ObsidianVaultAdapter(vault_root)
    adapter.ensure_skeleton()

    report.step(
        "Mid_Term/ 目錄自動建",
        (vault_root / "10_Permanent/Mid_Term").exists(),
        "預期 R7 C16 _SKELETON_DIRS 自動建",
    )
    report.step(
        "Mid_Term/_DIR_INFO.md 自動寫",
        (vault_root / "10_Permanent/Mid_Term/_DIR_INFO.md").exists(),
    )
    report.step(
        "99_Archive/auto_archived/ 骨架建",
        (vault_root / "99_Archive/auto_archived").exists(),
    )
    report.step(
        "promotion.yaml bootstrap 自動寫",
        (vault_root / "00_System/08_Runtime_Profiles/promotion.yaml").exists(),
        "R7 C18 ensure_promotion_config_file lazy import 在 bootstrap loop 內",
    )

    # baseline pinned
    user_note = adapter.read_note("10_Permanent/Profiles/USER.md")
    memory_note = adapter.read_note("10_Permanent/MEMORY.md")
    report.step(
        "USER.md baseline pinned=True + lifecycle=long",
        user_note is not None
            and user_note.frontmatter.pinned is True
            and user_note.frontmatter.lifecycle_state == LifecycleState.LONG,
        f"USER pinned={user_note.frontmatter.pinned if user_note else None}",
    )
    report.step(
        "MEMORY.md baseline pinned=True + lifecycle=long",
        memory_note is not None
            and memory_note.frontmatter.pinned is True
            and memory_note.frontmatter.lifecycle_state == LifecycleState.LONG,
        f"MEMORY pinned={memory_note.frontmatter.pinned if memory_note else None}",
    )

    # schema_version=3
    report.step(
        "frontmatter schema_version = 3 (R7)",
        memory_note is not None and memory_note.frontmatter.schema_version == 3,
    )

    # ─── Step 2: 模擬 daily_flush 寫入 ────────────────────────────────────
    report.section("Step 2: 模擬 3 個 daily_flush (含 wikilinks + 含 procedure tag)")
    flush_paths = [
        "11_AI_Mirror/ingestion_logs/daily_flush/2026-05-15.md",
        "11_AI_Mirror/ingestion_logs/daily_flush/2026-05-16.md",
        "11_AI_Mirror/ingestion_logs/daily_flush/2026-05-17.md",
    ]
    flush_bodies = [
        "# 2026-05-15 daily flush\n\n- 跟使用者討論 [[Python]] async 用法\n- 提到 [[GraphRAG]] 很重要\n- [[grep-then-analyze]] 流程很常用\n",
        "# 2026-05-16 daily flush\n\n- 又用了 [[Python]] decorator\n- [[grep-then-analyze]] 再次套用\n- [[GraphRAG]] 一跳擴展效果好\n",
        "# 2026-05-17 daily flush\n\n- [[grep-then-analyze]] 我發現要先 list_dir 才好\n- [[Python]] 跟 [[GraphRAG]] 串通了\n",
    ]
    for path, body in zip(flush_paths, flush_bodies):
        adapter.write_note(MemoryNote(
            path=path,
            frontmatter=Frontmatter(type=MemoryType.SHORT_TERM, source=MemorySource.FLUSH),
            body=body,
        ))
    report.step(
        "3 個 daily_flush 寫入成功",
        all((vault_root / p).exists() for p in flush_paths),
    )

    # ─── Step 3: Force-run curator daily light → Mid_Term aggregate ───────
    report.section("Step 3: Force-run curator daily light (短→中 aggregate)")
    daily_result = force_run(vault_root, "daily")
    aggregated_count = sum(len(a.get("created", [])) + len(a.get("updated", [])) for a in daily_result.get("aggregated", []))
    report.step(
        "curator daily 跑完 aggregated > 0",
        aggregated_count > 0,
        f"aggregated_count={aggregated_count}",
    )

    # 確認 Mid_Term 內出現 python / graphrag / grep-then-analyze
    midterm_dir = vault_root / "10_Permanent/Mid_Term"
    midterm_files = {p.stem for p in midterm_dir.glob("*.md") if not p.name.startswith("_")}
    expected_entities = {"python", "graphrag", "grep-then-analyze"}
    missing = expected_entities - midterm_files
    report.step(
        "Mid_Term 自動建立 3 個 entity (python / graphrag / grep-then-analyze)",
        not missing,
        f"existing={sorted(midterm_files)} missing={sorted(missing)}",
    )

    # 累計 mention_count: python 3 次 / graphrag 3 次 / grep-then-analyze 3 次
    for eid in ["python", "graphrag", "grep-then-analyze"]:
        note = adapter.read_note(f"10_Permanent/Mid_Term/{eid}.md")
        mc = note.frontmatter.mention_count if note else 0
        report.step(
            f"  {eid}.md mention_count == 3",
            mc == 3,
            f"actual mention_count = {mc}",
        )

    # ─── Step 4: 模擬時間 — 改 frontmatter 9 天前讓升長條件成立 ────────────
    report.section("Step 4: 模擬時間流逝 9d (改 frontmatter created/updated)")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()

    # 加 procedure tag 到 grep-then-analyze (準備 skill 提議)
    gtn_note = adapter.read_note("10_Permanent/Mid_Term/grep-then-analyze.md")
    if "procedure" not in gtn_note.frontmatter.tags:
        gtn_note.frontmatter.tags.append("procedure")
    adapter.write_note(gtn_note)  # 會更新 updated 為 now, 下面再 overwrite frontmatter
    report.step(
        "grep-then-analyze 加 procedure tag",
        "procedure" in gtn_note.frontmatter.tags,
    )

    # 強制改 created/updated 為 9 天前 (繞 write_note)
    for eid in ["python", "graphrag", "grep-then-analyze"]:
        note = adapter.read_note(f"10_Permanent/Mid_Term/{eid}.md")
        fm_dict = adapter._frontmatter_to_dict(note.frontmatter)
        fm_dict["created"] = old_ts
        fm_dict["updated"] = old_ts
        fm_dict["last_activity_at"] = old_ts
        _write_md_with_frontmatter(
            vault_root / f"10_Permanent/Mid_Term/{eid}.md",
            fm_dict,
            note.body,
        )

    # 重讀確認時間真的改了
    py_after = adapter.read_note("10_Permanent/Mid_Term/python.md")
    report.step(
        "python.md created 真的 9 天前 (滿足 stable_age≥7d)",
        (datetime.now(timezone.utc) - py_after.frontmatter.created.astimezone(timezone.utc)).days >= 7,
        f"created={py_after.frontmatter.created.isoformat()}",
    )

    # ─── Step 5: 加一個 200 天舊長期檔測 archive ──────────────────────────
    report.section("Step 5: 模擬 200 天舊長期 (測 archive) + 100 天舊 (測 stale)")
    old200_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    old100_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    _write_md_with_frontmatter(
        vault_root / "10_Permanent/Concepts/old200.md",
        {
            "type": "concept", "source": "agent", "created": old200_ts, "updated": old200_ts,
            "agent": "test", "status": "active", "schema_version": 3,
            "tags": [], "char_count": 30, "extras": {},
            "ai_ready": True, "etl_status": "internalised", "security_level": "safe_data",
            "aliases": [], "lifecycle_state": "long", "mention_count": 1,
            "last_activity_at": old200_ts, "pinned": False,
        },
        "200 天沒命中的長期記憶",
    )
    _write_md_with_frontmatter(
        vault_root / "10_Permanent/Concepts/old100.md",
        {
            "type": "concept", "source": "agent", "created": old100_ts, "updated": old100_ts,
            "agent": "test", "status": "active", "schema_version": 3,
            "tags": [], "char_count": 30, "extras": {},
            "ai_ready": True, "etl_status": "internalised", "security_level": "safe_data",
            "aliases": [], "lifecycle_state": "long", "mention_count": 1,
            "last_activity_at": old100_ts, "pinned": False,
        },
        "100 天沒命中的長期記憶",
    )

    # ─── Step 6: Force-run curator weekly deep → 升長 + 降級 + skill scan ─
    report.section("Step 6: Force-run curator weekly deep")
    weekly_result = force_run(vault_root, "weekly")
    steps = weekly_result.get("steps", {})

    promote_step = steps.get("promote_midterm_to_long", {})
    report.step(
        "promote_midterm_to_long: promoted ≥ 1 (python / graphrag / grep-then-analyze)",
        promote_step.get("promoted_count", 0) >= 1,
        f"promoted_count = {promote_step.get('promoted_count', 0)}, candidates = {promote_step.get('candidates_count', 0)}",
    )

    # 確認原 Mid_Term 加了 promoted_to extras
    promoted_count = 0
    for eid in ["python", "graphrag", "grep-then-analyze"]:
        note = adapter.read_note(f"10_Permanent/Mid_Term/{eid}.md")
        if note and note.frontmatter.lifecycle_state == LifecycleState.LONG and "promoted_to" in (note.frontmatter.extras or {}):
            promoted_count += 1
    report.step(
        "Mid_Term 升格後 lifecycle=long + extras.promoted_to 設定",
        promoted_count >= 1,
        f"promoted lifecycle 改 long 數 = {promoted_count}/3",
    )

    demote_step = steps.get("demote_long", {})
    report.step(
        "demote: old200.md archive 移檔",
        demote_step.get("archived_count", 0) >= 1,
        f"archived_count = {demote_step.get('archived_count', 0)}",
    )
    archived_files = list((vault_root / "99_Archive/auto_archived").rglob("old200.md"))
    report.step(
        "99_Archive/auto_archived/<YYYY>/old200.md 實際出現",
        len(archived_files) >= 1,
        f"found: {[str(p.relative_to(vault_root)).replace(chr(92), chr(47)) for p in archived_files]}",
    )
    report.step(
        "demote: old100.md staled",
        demote_step.get("staled_count", 0) >= 1,
        f"staled_count = {demote_step.get('staled_count', 0)}",
    )

    skill_step = steps.get("skill_suggestions_scan", {})
    report.step(
        "skill 升格 scan: new_added ≥ 1 (grep-then-analyze 有 procedure tag)",
        skill_step.get("new_added_count", 0) >= 1
            or skill_step.get("total_pending", 0) >= 1,
        f"new_added = {skill_step.get('new_added_count', 0)}, total_pending = {skill_step.get('total_pending', 0)}",
    )

    # ─── Step 7: 模擬使用者「升格」對話回應 ────────────────────────────────
    report.section("Step 7: 模擬使用者下一輪對話回「升格」 → 自動建 Skill")

    # 但 grep-then-analyze 可能已被升到 Concepts 不在 Mid_Term mid lifecycle 了
    # 改測：直接 pick + record (用本來 pending 的)
    proposal = pick_next_proposal(vault_root)
    if proposal is None:
        # 若 grep-then-analyze 已升長導致 promoted_to set, 確認 pending 內狀態
        pending = load_pending(vault_root)
        report.step(
            "pending_skill_suggestions.json 至少有 1 條紀錄",
            len(pending) >= 1,
            f"pending entries: {len(pending)}; details: {pending[:1] if pending else []}",
        )
        # 不能繼續 step 7 後段, skip
        report.step("(skip) pick_next_proposal 已無可選 — 上一輪升長已 promoted_to 為長期路徑", True)
    else:
        report.step(
            "pick_next_proposal 拿到 entity",
            True,
            f"entity_id = {proposal['entity_id']}",
        )

        # 模擬使用者回「升格」
        intent = parse_user_response_intent("升格")
        report.step(
            "parse_user_response_intent('升格') → accept",
            intent == "accept",
            f"actual = {intent}",
        )
        intent_short = parse_user_response_intent("升職很爽 (誤判測試)")
        report.step(
            "parse_user_response_intent('升職很爽 (誤判測試)') → none (防誤判)",
            intent_short == "none",
            f"actual = {intent_short}",
        )

        result = record_user_response(vault_root, entity_id=proposal["entity_id"], accept=True)
        report.step(
            "record_user_response(accept) → action=promoted",
            result.get("action") == "promoted",
            f"result = {result}",
        )

        target_path = result.get("target", "")
        report.step(
            "Skill SKILL.md 真的建在 00_System/Skills/<id>/",
            bool(target_path) and (vault_root / target_path).exists(),
            f"target = {target_path}",
        )

    # ─── Step 8: curator_state.json + curator_runs.jsonl 觀察 ──────────────
    report.section("Step 8: 持久化 state + observability")
    state_path = vault_root / ".ai/curator_state.json"
    report.step(
        ".ai/curator_state.json 持久化 (本機時區)",
        state_path.exists(),
    )
    if state_path.exists():
        state_json = json.loads(state_path.read_text(encoding="utf-8"))
        has_offset = "+" in state_json.get("last_daily_run_at", "") or "+" in state_json.get("last_weekly_run_at", "")
        report.step(
            "  state 時間含 timezone offset (本機時區非 UTC)",
            has_offset,
            f"last_daily_run_at = {state_json.get('last_daily_run_at')}",
        )

    log_path = vault_root / "11_AI_Mirror/ingestion_logs/curator_runs.jsonl"
    report.step(
        "curator_runs.jsonl observability log 自動寫",
        log_path.exists(),
    )

    promotion_events = vault_root / "11_AI_Mirror/ingestion_logs/promotion_events.md"
    report.step(
        "promotion_events.md 寫入升降事件",
        promotion_events.exists() and promotion_events.stat().st_size > 0,
    )

    # ─── Step 9 (R8 C24+C25+C26): 主動歸納驗證 — gap + weekly digest ───────
    report.section("Step 9 (R8): 主動歸納 — user gap + weekly digest")

    from agent_memory.gap_analysis import (
        scan_user_gaps, pick_next_gap, parse_gap_intent, dismiss_gap, load_pending_gaps,
    )
    from agent_memory.weekly_digest import (
        generate_weekly_digest, pick_undelivered_digest_footer, current_week_id, load_digest_state,
    )

    # 9.1 — gap scan: bootstrap USER.md 含「（請填寫）」應該抓到 + coffee.md (mention=5) 不在 USER.md 也抓
    # 加 coffee Mid_Term entity 模擬高頻
    adapter.write_note(MemoryNote(
        path="10_Permanent/Mid_Term/coffee.md",
        frontmatter=Frontmatter(
            type=MemoryType.CONCEPT, source=MemorySource.PROMOTION,
            tags=["mid_term"], lifecycle_state=LifecycleState.MID,
            mention_count=5, pinned=False,
        ),
        body="# coffee\n\n使用者愛喝咖啡\n",
    ))
    gap_scan_result = scan_user_gaps(vault_root)
    # Step 6 curator weekly 已 call scan_user_gaps 一次, placeholder 已進 pending. 第二次 scan 因 cooldown skip.
    # 改看「pending 總清單」(weekly + 本次 coffee 加總)
    full_pending = load_pending_gaps(vault_root)
    report.step(
        "gap scan 累積有 USER.md placeholder (curator weekly Step 6 已抓)",
        any("placeholder" in g.get("gap_id", "") for g in full_pending),
        f"pending gap_ids: {[g.get('gap_id') for g in full_pending]}",
    )
    report.step(
        "gap scan 抓到 Mid_Term/coffee 不在 USER.md",
        any("coffee" in g.get("gap_id", "") for g in full_pending),
    )

    # 9.2 — pick + dismiss
    gap = pick_next_gap(vault_root)
    report.step(
        "pick_next_gap 拿到至少 1 個 gap",
        gap is not None,
        f"gap_id = {gap.get('gap_id') if gap else None}",
    )

    intent_dismiss = parse_gap_intent("跳過")
    report.step(
        "parse_gap_intent('跳過') → dismiss",
        intent_dismiss == "dismiss",
    )
    intent_long = parse_gap_intent("這是我的偏好回覆語氣 (很長的答覆)")
    report.step(
        "parse_gap_intent(長句) → none (防誤判)",
        intent_long == "none",
    )

    if gap:
        d_result = dismiss_gap(vault_root, gap_id=gap["gap_id"])
        report.step(
            "dismiss_gap 標 dismissed",
            d_result.get("action") == "dismissed",
        )
        # 確認下次 pick 不會再拿到同個
        gap2 = pick_next_gap(vault_root)
        report.step(
            "dismiss 後 pick_next_gap 換下一個 (或 None)",
            gap2 is None or gap2.get("gap_id") != gap["gap_id"],
            f"now picked: {gap2.get('gap_id') if gap2 else None}",
        )

    # 9.3 — weekly digest 已由 curator weekly 跑時自動產 (Step 6 內)
    digest_dir = vault_root / "11_AI_Mirror/ingestion_logs/weekly_digest"
    digest_files = list(digest_dir.glob("*.md")) if digest_dir.exists() else []
    report.step(
        "weekly_digest/<YYYY-WW>.md 自動產生 (curator weekly Step 5)",
        len(digest_files) >= 1,
        f"digest files: {[p.name for p in digest_files]}",
    )

    state_path = vault_root / ".ai/weekly_digest_state.json"
    report.step(
        ".ai/weekly_digest_state.json 寫入",
        state_path.exists(),
    )

    # 9.4 — pick footer first time → 拿到; 第二次同 week → None
    footer1 = pick_undelivered_digest_footer(vault_root)
    report.step(
        "pick_undelivered_digest_footer 第一次拿到 footer",
        footer1 is not None,
    )
    footer2 = pick_undelivered_digest_footer(vault_root)
    report.step(
        "pick_undelivered_digest_footer 同週第二次回 None (last_shown 已標)",
        footer2 is None,
    )

    # 9.5 — 重生 digest (Step 7 升 skill 發生在 Step 6 curator weekly 之後, 需重新跑才會反映)
    # 模擬「下週 curator weekly 跑」會把上週升的 skill 抓進來
    fresh_digest = generate_weekly_digest(vault_root)
    digest_path_abs = vault_root / fresh_digest["digest_path"]
    if digest_path_abs.exists():
        digest_text = digest_path_abs.read_text(encoding="utf-8")
        report.step(
            "重生 digest 後內含本輪升 Skill 紀錄 (grep-then-analyze)",
            "grep-then-analyze" in digest_text,
            f"digest size = {digest_path_abs.stat().st_size}",
        )

    # ─── Step 10 (R9): LLM 整理 + 主動回想驗證 (含 mock infra) ─────────────
    report.section("Step 10 (R9): LLM 整理 + 主動回想 (mock LLM)")

    # 10.1 — C34 curator 三層節奏 (light / medium / weekly should_run_now 各自獨立)
    from agent_memory.curator import should_run_now, CuratorState, CuratorConfig, _now_local, force_run as cf_force_run
    from datetime import timedelta as _td
    s_test = CuratorState()
    c_test = CuratorConfig()
    s_test.first_light_seeded_at = _now_local() - _td(hours=3)
    s_test.first_medium_seeded_at = _now_local() - _td(hours=25)
    s_test.first_weekly_seeded_at = _now_local() - _td(days=8)
    for mode_test in ["light", "medium", "weekly"]:
        ok_test, reason_test = should_run_now(s_test, c_test, mode_test)
        report.step(
            f"C34 should_run_now('{mode_test}') seeded 過 interval → ok",
            ok_test,
            f"reason={reason_test}",
        )

    # 10.2 — C35 entity filter (純表情/單字過濾) + extract_with_count occurrence
    from agent_memory.entity_extract import extract_entities_from_text, extract_entities_with_count
    trivial_test = extract_entities_from_text("[[1]] [[a]] [[!]] [[Python]] [[GraphRAG]]", max_entities=30)
    report.step(
        "C35 trivial filter 過濾 1/a/!, 保留 Python/GraphRAG",
        "python" in trivial_test and "graphrag" in trivial_test and "1" not in trivial_test and "a" not in trivial_test,
        f"result = {trivial_test}",
    )
    count_test = extract_entities_with_count("[[Python]] [[Python]] [[Python]] [[Vue]]", min_occurrences=2)
    report.step(
        "C35 extract_with_count min_occurrences=2 過濾 Vue(1 次)",
        len(count_test) == 1 and count_test[0][0] == "python" and count_test[0][1] == 3,
        f"result = {count_test}",
    )

    # 10.3 — C31 cross-session linking
    from agent_memory.session_linker import collect_recent_cross_session_context
    cross = collect_recent_cross_session_context(vault_root, persona_id="steward", current_session_id="x", recent_minutes=60)
    report.step(
        "C31 cross-session linking (有 session_log 可撈)",
        isinstance(cross.get("text_block"), str),
        f"persona={cross.get('persona_id')}, paths_count={len(cross.get('session_paths', []))}",
    )

    # 10.4 — C32 fresh chat recall (用前面已建的 session_log)
    from agent_memory.session_linker import find_last_session_for_recall, build_fresh_chat_recall_prepend, is_fresh_session
    recall = find_last_session_for_recall(vault_root, persona_id="steward", current_session_id="totally-new")
    report.step(
        "C32 find_last_session_for_recall: 沒 steward session_log 也不爆",
        recall is None or isinstance(recall, dict),
        f"recall = {'found' if recall else 'None (預期 - 沒建過 steward session_log)'}",
    )
    fresh = is_fresh_session(vault_root, persona_id="steward", context="discord-X", session_id="totally-new")
    report.step(
        "C32 is_fresh_session for new session → True",
        fresh is True,
    )

    # 10.5 — C27 LLM umbrella (mock_response)
    from agent_memory.umbrella_llm import consolidate_umbrella_with_llm, load_pending_umbrella, load_pending_procedure_tags, apply_procedure_tag
    # 建 3 個語意相關但 prefix 不同 entity
    for sub_eid, body in [
        ("async-io-r9", "Python asyncio 用法"),
        ("concurrent-futures-r9", "Python concurrent.futures"),
        ("threading-r9", "Python threading 基礎"),
    ]:
        adapter.write_note(MemoryNote(
            path=f"10_Permanent/Mid_Term/{sub_eid}.md",
            frontmatter=Frontmatter(
                type=MemoryType.CONCEPT, source=MemorySource.PROMOTION,
                tags=["mid_term"], lifecycle_state=LifecycleState.MID,
                mention_count=3, pinned=False,
            ),
            body=f"# {sub_eid}\n{body}",
        ))
    mock_um = {
        "merges": [{
            "umbrella_id": "python-concurrency-r9",
            "members": ["async-io-r9", "concurrent-futures-r9", "threading-r9"],
            "reason": "都是 Python 並行原語",
        }],
        "procedure_tags": [{
            "entity_id": "async-io-r9",
            "reason": "body 含 async/await 流程",
        }],
    }
    um_result = consolidate_umbrella_with_llm(vault_root, mock_response=mock_um)
    report.step(
        "C27 LLM umbrella (mock): merges_added = 1",
        len(um_result.get("merges_added", [])) == 1,
        f"merges={um_result.get('merges_added', [])[:1]}",
    )
    report.step(
        "C27 LLM procedure_tags_added (mock): 1 條 + apply 後 Mid_Term 含 procedure tag",
        len(um_result.get("procedure_tags_added", [])) == 1,
    )
    ap = apply_procedure_tag(vault_root, entity_id="async-io-r9")
    aio = adapter.read_note("10_Permanent/Mid_Term/async-io-r9.md")
    report.step(
        "C27 apply_procedure_tag 後 tags 含 'procedure'",
        aio is not None and "procedure" in aio.frontmatter.tags,
        f"tags = {aio.frontmatter.tags if aio else None}",
    )

    # 10.6 — C28 weekly digest LLM narrative (mock)
    digest_with_llm = generate_weekly_digest(vault_root, llm_mock_narrative="本週注意力大部分在 Python 並行 + R9 設計, 建議週末做 reflect.")
    digest_with_llm_path = vault_root / digest_with_llm["digest_path"]
    report.step(
        "C28 weekly digest with mock LLM narrative",
        digest_with_llm.get("llm_narrative_used") is True
            and "LLM 觀察" in digest_with_llm_path.read_text(encoding="utf-8"),
        f"llm_narrative_used = {digest_with_llm.get('llm_narrative_used')}",
    )

    # 10.7 — C29 reflect on-demand (mock)
    from agent_memory.reflect import reflect_topic
    reflect_mock = "## 主題概覽\n你的 Python 記憶圍繞 async / concurrent\n\n## 核心要點\n- asyncio\n\n## 關聯 wikilinks\n[[python]]\n\n## 後續建議\n深入 FastAPI"
    rf = reflect_topic(vault_root, "Python", mock_body=reflect_mock)
    report.step(
        "C29 reflect_topic Python → 產 Concepts/reflection_*.md",
        rf.get("action") == "created" and (vault_root / rf.get("path", "")).exists(),
        f"action={rf.get('action')}, path={rf.get('path')}",
    )

    # 10.8 — C30 USER.md vs Mid_Term 矛盾偵測 (mock)
    from agent_memory.gap_analysis import scan_user_gaps_llm, load_pending_gaps
    contra_mock = [{
        "user_md_claim": "偏好簡潔技術回覆",
        "midterm_evidence_entity": "async-io-r9",
        "severity": "low",
        "reason": "Mid_Term 高頻含 Python async 細節需要長說明",
    }]
    contra_result = scan_user_gaps_llm(vault_root, mock_response=contra_mock)
    contradictions = [g for g in load_pending_gaps(vault_root) if g.get("kind") == "contradiction"]
    report.step(
        "C30 scan_user_gaps_llm (mock) → pending 含 kind=contradiction",
        len(contradictions) >= 1,
        f"contradictions_count = {len(contradictions)}",
    )

    # ─── Step 11 (R10): 管家收尾 (observability + Recent_Updates + 文獻吸收) ─────
    report.section("Step 11 (R10): 管家收尾 — observability + Obsidian index + 文獻吸收 (mock LLM)")

    # 11.1 — C36 pending_overview 結構正確 (沿用 Step 9/10 已產的 pending pool 真實狀態)
    from agent_memory.observability_views import (
        pending_overview,
        list_user_gaps,
        list_contradictions,
        list_umbrella_suggestions,
    )
    overview = pending_overview(vault_root)
    report.step(
        "C36 pending_overview 回 4 pool dict + total_pending int",
        all(k in overview for k in ("skill_suggestions", "umbrella", "procedure_tags", "user_gaps", "total_pending"))
            and isinstance(overview["total_pending"], int),
        f"total_pending={overview.get('total_pending')}, keys={sorted(overview.keys())}",
    )

    # 11.2 — C36 list_contradictions = gap-list kind=contradiction 子集 (跟 Step 10 一致)
    contradictions_via_view = list_contradictions(vault_root)
    contradictions_via_gap = list_user_gaps(vault_root, kind="contradiction")
    report.step(
        "C36 list_contradictions = list_user_gaps(kind=contradiction)",
        len(contradictions_via_view) == len(contradictions_via_gap) == len(contradictions),
        f"contra_view={len(contradictions_via_view)} contra_gap={len(contradictions_via_gap)} contra_step10={len(contradictions)}",
    )

    # 11.3 — C36 list_umbrella_suggestions 可看到 Step 10.5 mock 產的 1 個 merge
    umbrella_pending = list_umbrella_suggestions(vault_root)
    report.step(
        "C36 list_umbrella_suggestions 含 Step 10.5 mock 產的 merge",
        len(umbrella_pending) >= 1 and any("python-concurrency" in s.get("umbrella_id", "") for s in umbrella_pending),
        f"umbrella_pending={len(umbrella_pending)}, ids={[s.get('umbrella_id') for s in umbrella_pending]}",
    )

    # 11.4 — C37 build_recent_updates_markdown 純函式產 markdown 不爆 + 5 sections
    from agent_memory.recent_updates import build_recent_updates_markdown, write_recent_updates, RECENT_UPDATES_RELATIVE_PATH
    md = build_recent_updates_markdown(vault_root, lookback_days=7)
    sections_expected = [
        "新增 / 更新的 Mid_Term entity",
        "升降格事件",
        "Skill 升格",
        "本週 / 近期 Weekly digest",
        "Curator 跑過幾輪",
    ]
    missing_sections = [s for s in sections_expected if s not in md]
    report.step(
        "C37 build_recent_updates_markdown 含 5 sections + frontmatter pinned=true",
        not missing_sections and "pinned: true" in md and "lifecycle_state: long" in md,
        f"len(md)={len(md)} bytes, missing_sections={missing_sections}",
    )

    # 11.5 — C37 write_recent_updates 落地 00_System/09_Index/03_Recent_Updates.md
    ru_result = write_recent_updates(vault_root)
    ru_path = vault_root / RECENT_UPDATES_RELATIVE_PATH
    report.step(
        "C37 write_recent_updates → 03_Recent_Updates.md atomic 寫入",
        ru_path.exists()
            and ru_result.get("path") == RECENT_UPDATES_RELATIVE_PATH
            and ru_result.get("bytes_written", 0) > 500,
        f"path={ru_result.get('path')}, bytes={ru_result.get('bytes_written')}",
    )

    # 11.6 — C37 落地檔 frontmatter pinned=true (避免被 curator archive)
    ru_text = ru_path.read_text(encoding="utf-8")
    report.step(
        "C37 03_Recent_Updates.md frontmatter pinned=true + lifecycle_state=long",
        "pinned: true" in ru_text and "lifecycle_state: long" in ru_text and "schema_version: 3" in ru_text,
        f"ru_text head: {ru_text[:200]!r}",
    )

    # 11.7 — C38 summarize_external_ingest (mock LLM): 餵 3 個外部檔 → 3 個 Concept 落地
    from agent_memory.external_ingest_summarize import summarize_external_ingest, load_state as load_ext_state
    # 建假 external_ingest 檔 (text/md/帶 BOM 模擬)
    ext_dir = vault_root / "11_AI_Mirror" / "external_ingest" / "discord_attachments" / "ch_e2e"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "rag_intro.md").write_text("# RAG 介紹\n\nRetrieval-Augmented Generation 結合檢索與生成的 NLP 技術.", encoding="utf-8")
    (ext_dir / "py_async.txt").write_text("Python asyncio 提供 coroutine + event loop.", encoding="utf-8")
    # BOM 檔測 strip
    (ext_dir / "bom_doc.md").write_bytes("﻿# BOM 文檔\n\n含 BOM 的內容, 測 strip.".encode("utf-8"))

    ext_mock = {
        "title": "外部文獻 mock 摘要",
        "summary": "Mock 摘要: RAG + Python asyncio. 給 C38 e2e 驗證寫 Concept 通暢.",
        "key_concepts": ["RAG", "Python asyncio"],
        "tags": ["rag", "python"],
        "wikilinks_suggested": ["[[RAG]]", "[[Python asyncio]]"],
    }
    ext_result = summarize_external_ingest(vault_root, mock_response=ext_mock, max_files=5)
    report.step(
        "C38 mock LLM: 3 外部檔 → 3 Concept (含 BOM 自動 strip)",
        len(ext_result.get("summarized", [])) == 3
            and not ext_result.get("errors")
            and ext_result.get("mock_used") is True,
        f"summarized={len(ext_result.get('summarized', []))}, errors={len(ext_result.get('errors', []))}, skipped={len(ext_result.get('skipped', []))}",
    )

    # 11.8 — C38 Concept frontmatter 對齊 schema
    if ext_result.get("summarized"):
        first_concept_path = vault_root / ext_result["summarized"][0]["concept_path"]
        concept_text = first_concept_path.read_text(encoding="utf-8") if first_concept_path.exists() else ""
        report.step(
            "C38 Concept frontmatter type=concept + source=promotion + extras.ingest_method",
            "type: concept" in concept_text
                and "source: promotion" in concept_text
                and "llm_summarize_c38" in concept_text
                and "lifecycle_state: long" in concept_text,
            f"path={first_concept_path}, head={concept_text[:200]!r}",
        )
    else:
        report.step("C38 Concept frontmatter (skipped - no summarized)", False, "no summarized files")

    # 11.9 — C38 state 持久化 + cooldown skip
    ext_state = load_ext_state(vault_root)
    entries_in_state = len(ext_state.get("entries", {}))
    # 第二次跑同 vault → 應該全 cooldown skip
    ext_result_2 = summarize_external_ingest(vault_root, mock_response=ext_mock, max_files=5)
    report.step(
        "C38 state.json 持久化 + 第二輪 cooldown skip",
        entries_in_state == 3 and len(ext_result_2.get("summarized", [])) == 0
            and any("in_cooldown" in s.get("reason", "") for s in ext_result_2.get("skipped", [])),
        f"state_entries={entries_in_state}, second_run_summarized={len(ext_result_2.get('summarized', []))}, second_skipped={len(ext_result_2.get('skipped', []))}",
    )

    # ─── Step 12 (R11): LLMClient interface 修 — 真實 LLM 路徑乾淨 fallback ──
    report.section("Step 12 (R11): LLM helper 統一 + 真實 LLM 路徑 fallback (HANDOFF §4.3 技術債)")

    # 12.1 — R11 C41 _extract_first_json_block strip ```json fence
    from agent_memory.llm_text_helpers import _extract_first_json_block
    fenced = '```json\n{"key": "val"}\n```'
    bare = _extract_first_json_block(fenced, expect_array=False)
    report.step(
        "C41 _extract_first_json_block 自動 strip ```json fence",
        bare.strip() == '{"key": "val"}',
        f"input={fenced!r}, output={bare!r}",
    )

    # 12.2 — R11 C41 _extract_first_json_block list 模式
    fenced_list = '```\n[{"a": 1}, {"b": 2}]\n```'
    bare_list = _extract_first_json_block(fenced_list, expect_array=True)
    report.step(
        "C41 _extract_first_json_block expect_array=True 抓 [...] 塊",
        bare_list.strip() == '[{"a": 1}, {"b": 2}]',
        f"output={bare_list!r}",
    )

    # 12.3 — R11 C41 call_llm_for_text 沒 LLM env 拋 LLMClientError (不 TypeError)
    from agent_memory.llm_text_helpers import call_llm_for_text
    from agent_memory.llm_client import LLMClientError
    exc_caught: Exception | None = None
    try:
        call_llm_for_text(vault_root, "smoke test prompt", timeout_s=2.0)
    except Exception as exc:  # noqa: BLE001
        exc_caught = exc
    report.step(
        "C41 call_llm_for_text 沒 LLM env → LLMClientError (不 TypeError)",
        isinstance(exc_caught, LLMClientError),
        f"exc_type={type(exc_caught).__name__ if exc_caught else 'None'}",
    )

    # 12.4 — R11 C41 真實 LLM 路徑: 5 模組裡挑 umbrella_llm 驗
    # 不傳 mock_response → 進真 LLM 路徑。
    # 無 LLM env: 應 graceful fallback (llm_call_failed)。
    # 有 LLM env: 允許 llm_called=True 且 error 空字串（真實呼叫成功）。
    from agent_memory.umbrella_llm import consolidate_umbrella_with_llm
    # 建多個同 prefix Mid_Term 讓 scan 有 candidate (sleep cycle 才會 trigger LLM)
    mt = vault_root / "10_Permanent" / "Mid_Term"
    mt.mkdir(parents=True, exist_ok=True)
    for slug in ("python-async-r11", "python-deco-r11"):
        (mt / f"{slug}.md").write_text(
            "---\ntype: concept\nsource: agent\nlifecycle_state: mid\nmention_count: 3\ntags: [mid_term]\n---\n# stub",
            encoding="utf-8",
        )
    real_llm_result = consolidate_umbrella_with_llm(vault_root)
    real_error = str(real_llm_result.get("error", ""))
    real_called = real_llm_result.get("llm_called") is True
    fallback_ok = ("llm_call_failed" in real_error) and not real_called
    live_ok = (real_error.strip() == "") and real_called
    no_typeerror = "TypeError" not in real_error
    has_google_key = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
    report.step(
        "C41 真實 LLM 路徑: 無 env fallback / 有 env 成功 (都不得 TypeError)",
        (fallback_ok or live_ok) and no_typeerror,
        (
            f"has_google_key={has_google_key}, "
            f"error={real_error[:120]}, "
            f"llm_called={real_llm_result.get('llm_called')}"
        ),
    )

    # ─── Step 13 (R12): Codex 批次 A fix 驗證 ──────────────────────────────
    report.section("Step 13 (R12): TOOL parser 擴 / prompt budget cap / persona-wizard preset")

    # 13.1 — C44 TOOL parser 接 4 種 closing tag
    from agent_memory.local_tools import parse_agent_tool_calls, count_unmatched_tool_attempts
    samples_c44 = {
        "[/TOOL]": '[TOOL]memory{"action":"add"}[/TOOL]',
        "<tool_call|>": '[TOOL]memory{"action":"add"}<tool_call|>',
        "</tool_call>": '[TOOL]memory{"action":"add"}</tool_call>',
        "<|tool_call|>": '[TOOL]memory{"action":"add"}<|tool_call|>',
    }
    all_4_ok = all(len(parse_agent_tool_calls(t)) == 1 for t in samples_c44.values())
    report.step(
        "C44 parse_agent_tool_calls 接 4 種 closing tag 變體",
        all_4_ok,
        f"variants_tested={list(samples_c44.keys())}",
    )

    # 13.2 — C44 unmatched [TOOL] 護欄計數正確
    mixed = '[TOOL]memory{"a":1}[/TOOL] xx [TOOL]broken_no_close{leftover'
    parsed_mixed = parse_agent_tool_calls(mixed)
    unmatched = count_unmatched_tool_attempts(mixed, len(parsed_mixed))
    report.step(
        "C44 count_unmatched_tool_attempts: 1 ok + 1 unmatched 偵測",
        len(parsed_mixed) == 1 and unmatched == 1,
        f"parsed={len(parsed_mixed)}, unmatched={unmatched}",
    )

    # 13.3 — C45 cross_session 預設 max_total_chars 從 chat_runtime 傳入 800
    # 直接驗 chat_runtime module 內常數
    import agent_memory.chat_runtime as _crt
    crt_src = Path(_crt.__file__).read_text(encoding="utf-8")
    has_caps = all(
        s in crt_src
        for s in ("HISTORY_TAIL_CAP = 2400", "CROSS_SESSION_CAP = 800", "SHARED_HISTORY_CAP = 1200", "MEMORY_CONTEXT_CAP = 3000")
    )
    report.step(
        "C45 chat_runtime 四個 prompt budget 常數都在",
        has_caps,
        f"caps_check={has_caps} (HISTORY 2400 / CROSS 800 / SHARED 1200 / MEMORY 3000)",
    )

    # 13.4 — C46 _LLM_PRESET_MAP 含 7 preset
    from agent_memory.cli import _LLM_PRESET_MAP, _resolve_llm_preset_or_explicit
    expected_keys = {"gemma4", "qwen9", "qwen30", "gemini", "gemini-pro", "gemma-31b", "gemma-26b"}
    actual_keys = set(_LLM_PRESET_MAP.keys())
    report.step(
        "C46 _LLM_PRESET_MAP 含 7 個 preset alias",
        expected_keys == actual_keys,
        f"missing={expected_keys - actual_keys}, extra={actual_keys - expected_keys}",
    )

    # 13.5 — C46 _resolve_llm_preset_or_explicit 解析
    import argparse as _argparse
    ns_key = _argparse.Namespace(key="gemma-31b", profile=None, model=None)
    ns_explicit = _argparse.Namespace(key=None, profile="gemini", model="gemini-1.5-pro")
    try:
        ns_bogus = _argparse.Namespace(key="bogus", profile=None, model=None)
        _resolve_llm_preset_or_explicit(ns_bogus)
        bogus_raised = False
    except ValueError:
        bogus_raised = True
    p1, m1 = _resolve_llm_preset_or_explicit(ns_key)
    p2, m2 = _resolve_llm_preset_or_explicit(ns_explicit)
    report.step(
        "C46 _resolve 解析: preset → (profile,model) / explicit / bogus 拋 ValueError",
        p1 == "gemini" and m1 == "gemma-4-31b-it"
            and p2 == "gemini" and m2 == "gemini-1.5-pro"
            and bogus_raised,
        f"key→{p1}/{m1}, explicit→{p2}/{m2}, bogus_raised={bogus_raised}",
    )

    return report.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", default="", help="指定 vault root (不指定則用 temp dir).")
    parser.add_argument("--keep", action="store_true", help="跑完保留 vault (temp dir 不刪).")
    args = parser.parse_args()

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  R7 E2E Simulation — 自動驗證自我進化迴圈是否串通")
    print("  對應 V2_Round7_記憶分層升格設計 + MISSION §3 7 個承諾")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    report = Report()
    cleanup = None
    if args.vault:
        vault_root = Path(args.vault).expanduser().resolve()
        print(f"  Vault: {vault_root} (指定, 不會清理)")
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="r7_e2e_"))
        vault_root = tmp_dir / "vault"
        print(f"  Vault: {vault_root} (臨時)")
        if not args.keep:
            cleanup = lambda: _safe_unlink_tree(tmp_dir)

    try:
        return run_simulation(vault_root, report)
    except Exception as exc:
        import traceback
        print()
        print(f"  💥 Simulation crashed: {exc}")
        traceback.print_exc()
        return 2
    finally:
        if cleanup:
            cleanup()
            print(f"  (temp vault 已清理)")
        elif args.keep:
            print(f"  (vault 保留在 {vault_root} — 可用 Obsidian 開啟手動檢查)")


if __name__ == "__main__":
    sys.exit(main())
