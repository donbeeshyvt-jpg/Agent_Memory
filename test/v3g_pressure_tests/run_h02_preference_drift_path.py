# -*- coding: utf-8 -*-
"""V3-H2 壓測: 殘-03 preference + 殘-04 drift_guard 雙寫路徑釐清.

驗證:
- Test 1: drift_guard.py 不再直接 atomic_write 到 73_Candidates/
- Test 2: drift_guard call write_drift_candidate_md (canonical 路徑)
- Test 3: drift_candidate schema 含 user_id + current_value + awaiting_human_confirm (backward compat)
- Test 4: preference_consolidator 升 semantic 時 call write_preference_md
- Test 5: owner pref → 61_Owner_Preferences / viewer → 62_Viewer_Preferences 分流
"""
from __future__ import annotations
import sys
import time
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h02_preference_drift.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-H2 PRESSURE TEST: 殘-03 preference + 殘-04 drift_guard 路徑")
    log("=" * 70)
    failed = 0

    # ─── Test 1: drift_guard 不再直接 atomic_write 73_Candidates/ ───
    log("\n[Test 1] drift_guard.py 不再直接 atomic_write 73_Candidates/")
    src = (ROOT / "agent_memory" / "companion" / "drift_guard.py").read_text(encoding="utf-8")
    # 找 atomic_write(candidate_path 直接寫的 pattern (V3-H2 已廢)
    if "atomic_write(candidate_path, content)" in src:
        log("  ❌ FAIL: 還有直接 atomic_write(candidate_path, ...) 沒清掉")
        failed += 1
    else:
        log("  ✅ PASS: 廢除直接 atomic_write")

    # ─── Test 2: drift_guard 改 call markdown_writers ───
    log("\n[Test 2] drift_guard 走 markdown_writers canonical")
    if "from agent_memory.companion.markdown_writers import write_drift_candidate_md" not in src:
        log("  ❌ FAIL: drift_guard 沒 import write_drift_candidate_md")
        failed += 1
    else:
        log("  ✅ PASS: import write_drift_candidate_md")
    if "candidate_path = write_drift_candidate_md(" not in src:
        log("  ❌ FAIL: drift_guard 沒 call write_drift_candidate_md")
        failed += 1
    else:
        log("  ✅ PASS: call write_drift_candidate_md")

    # ─── Test 3: schema superset ───
    log("\n[Test 3] write_drift_candidate_md schema superset (user_id + current_value + awaiting_human_confirm)")
    from agent_memory.companion.markdown_writers import write_drift_candidate_md
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        path = write_drift_candidate_md(
            tmp_vault,
            trait_name="baseline_balance",
            proposed_value=0.5,
            evidence_count=8,
            drift_score=0.65,
            current_value=0.3,
            user_id="user-1",
            candidate_id=str(uuid.uuid4()),
        )
        if not path or not path.exists():
            log("  ❌ FAIL: write 失敗")
            failed += 1
        else:
            content = path.read_text(encoding="utf-8")
            checks = [
                ("user_id: user-1", "user_id field"),
                ("current_value: 0.3000", "current_value field"),
                ("awaiting_human_confirm: true", "awaiting_human_confirm (backward compat)"),
                ("awaiting_active: true", "awaiting_active (V3-G6 新)"),
            ]
            for kw, name in checks:
                if kw not in content:
                    log(f"  ❌ FAIL: {name} 沒寫")
                    failed += 1
                else:
                    log(f"  ✅ {name}")

    # ─── Test 4: preference_consolidator 升 semantic 接 write_preference_md ───
    log("\n[Test 4] preference_consolidator 升 semantic 呼叫 write_preference_md")
    pc_src = (ROOT / "agent_memory" / "companion" / "preference_consolidator.py").read_text(encoding="utf-8")
    if "from agent_memory.companion.markdown_writers import write_preference_md" not in pc_src:
        log("  ❌ FAIL: preference_consolidator 沒 import write_preference_md")
        failed += 1
    else:
        log("  ✅ PASS: preference_consolidator import")
    if "write_preference_md(" not in pc_src:
        log("  ❌ FAIL: preference_consolidator 沒 call writer")
        failed += 1
    else:
        log("  ✅ PASS: preference_consolidator call writer")

    # ─── Test 5: preference owner/viewer 分流 (sanity 既有 V3-G6 test 已 cover) ───
    log("\n[Test 5] write_preference_md owner→61 / viewer→62 (regression)")
    from agent_memory.companion.markdown_writers import write_preference_md
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        p_owner = write_preference_md(
            tmp_vault, topic="coffee", claim="主人愛黑咖啡",
            user_id="owner-1", is_owner=True, strength=0.8, confidence=0.7, evidence_count=5,
        )
        p_viewer = write_preference_md(
            tmp_vault, topic="game", claim="觀眾愛 mod",
            user_id="viewer-1", is_owner=False, strength=0.6, confidence=0.5, evidence_count=3,
        )
        if not p_owner or "61_Owner_Preferences" not in str(p_owner):
            log("  ❌ FAIL: owner→61 分流錯")
            failed += 1
        else:
            log("  ✅ PASS: owner→61_Owner_Preferences")
        if not p_viewer or "62_Viewer_Preferences" not in str(p_viewer):
            log("  ❌ FAIL: viewer→62 分流錯")
            failed += 1
        else:
            log("  ✅ PASS: viewer→62_Viewer_Preferences")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-H2 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-H2 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
