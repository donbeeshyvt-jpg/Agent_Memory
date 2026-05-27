# -*- coding: utf-8 -*-
"""V3-J1 + V3-J2 壓測: trait_evolution chat hook + obsidian_watcher inbox 觸發.

驗證:
- Test 1: chat_runtime Step 17.4 加 trait_evolution hook
- Test 2: add_trait_evidence + audit_candidate 跑 7 turn 後寫 73_Candidates
- Test 3: daemon Check-IngestInbox helper 存在
- Test 4: daemon 對 inbox 有檔強制觸發 L4
"""
from __future__ import annotations
import os
import sys
import time
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "j01_trait_watcher.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-J1 + V3-J2 PRESSURE TEST: trait_evolution hook + watcher inbox")
    log("=" * 70)
    failed = 0

    # ─── Test 1: chat_runtime Step 17.4 加 trait_evolution hook ───
    log("\n[Test 1] chat_runtime Step 17.4 加 trait_evolution hook")
    src = (ROOT / "agent_memory" / "companion" / "companion_chat_runtime.py").read_text(encoding="utf-8")
    if "Step 17.4" not in src or "V3-J1 trait_evolution evidence" not in src:
        log("  ❌ FAIL: Step 17.4 沒加")
        failed += 1
    else:
        log("  ✅ PASS: Step 17.4 hook 存在")
    if "add_trait_evidence" not in src or "audit_candidate" not in src:
        log("  ❌ FAIL: add_trait_evidence / audit_candidate call 沒加")
        failed += 1
    else:
        log("  ✅ PASS: add_trait_evidence + audit_candidate 接上")

    # ─── Test 2: 跑 7 turn 累積 evidence → audit_candidate 寫 73_Candidates ───
    log("\n[Test 2] 7 次 add_trait_evidence → audit_candidate 寫 73_Candidates")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(tmp_vault) as conn:
            pass  # init schema
        from agent_memory.companion.trait_evolution import add_trait_evidence
        from agent_memory.companion.drift_guard import audit_candidate

        # 跑 7 turn, observation_value 漂到 0.85 ~ 1.0 (確保 drift ≥ 0.5)
        # current=0 default, proposed 平均 ~0.92 → drift = 0.92 × 0.7 ≈ 0.64 ≥ 0.5
        for i in range(7):
            result = add_trait_evidence(
                tmp_vault, "user-j1",
                "baseline_balance",
                observation_value=0.85 + i * 0.02,  # 0.85 ~ 0.97 平均 0.91
                event_id=f"evt-{i}",
            )
        log(f"  add_trait_evidence 跑 7 次, last result = {result}")

        # audit_candidate
        audit_result = audit_candidate(tmp_vault, "user-j1", "baseline_balance")
        log(f"  audit drift={audit_result.drift_score:.3f}, passed={audit_result.passed}")
        log(f"  candidate_path={audit_result.candidate_path}")

        if not audit_result.passed:
            log(f"  ❌ FAIL: 7 evidence + drift 應該 pass, 但 {audit_result.reason}")
            failed += 1
        else:
            # 確認 markdown 寫了
            candidates_dir = tmp_vault / "70_Persona_Versions" / "73_Candidates"
            if not candidates_dir.exists():
                log("  ❌ FAIL: 73_Candidates 目錄不存在")
                failed += 1
            else:
                mds = list(candidates_dir.glob("*.md"))
                if not mds:
                    log("  ❌ FAIL: 73_Candidates 沒 .md")
                    failed += 1
                else:
                    log(f"  ✅ PASS: 寫了 {len(mds)} 個 candidate.md")
                    # 驗 frontmatter (V3-H2 統一 schema superset)
                    content = mds[0].read_text(encoding="utf-8")
                    if "type: persona_version_candidate" not in content:
                        log("  ❌ FAIL: frontmatter type 錯")
                        failed += 1
                    elif "awaiting_human_confirm: true" not in content:
                        log("  ❌ FAIL: awaiting_human_confirm 沒寫")
                        failed += 1
                    else:
                        log("  ✅ PASS: schema superset (V3-H2) 對齊")

    # ─── Test 3: daemon Check-IngestInbox helper ───
    log("\n[Test 3] daemon Check-IngestInbox helper 存在")
    daemon_src = (ROOT / "scripts" / "companion-curator-daemon.ps1").read_text(encoding="utf-8")
    if "function Check-IngestInbox" not in daemon_src:
        log("  ❌ FAIL: Check-IngestInbox 沒加")
        failed += 1
    else:
        log("  ✅ PASS: Check-IngestInbox 存在")
    if "list_ingest_inbox" not in daemon_src:
        log("  ❌ FAIL: 沒 call list_ingest_inbox")
        failed += 1
    else:
        log("  ✅ PASS: call list_ingest_inbox")

    # ─── Test 4: daemon 對 inbox 有檔強制觸發 L4 ───
    log("\n[Test 4] daemon 對 inbox 有檔 force L4 (skip 7d gate)")
    if '$forceL4 = ($inboxPending -gt 0)' not in daemon_src:
        log("  ❌ FAIL: forceL4 邏輯沒加")
        failed += 1
    else:
        log("  ✅ PASS: forceL4 邏輯接上")
    if "[FORCE] L4 觸發" not in daemon_src:
        log("  ❌ FAIL: force log 沒加")
        failed += 1
    else:
        log("  ✅ PASS: force log marker")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-J1 + V3-J2 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-J 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
