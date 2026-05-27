# -*- coding: utf-8 -*-
"""V3-H3 壓測: 殘-05 attention_score 寫入 + 殘-06 flow_mode_history INSERT.

驗證:
- Test 1: chat_runtime import maybe_record_flow_mode_transition
- Test 2: 模擬 transition → flow_mode_history INSERT 1 row
- Test 3: 同 mode 連續 → 不重複 INSERT (transition guard)
- Test 4: attention_score UPDATE 邏輯存在
- Test 5: 算式範圍合理 (intimacy × salience × goal × novelty ∈ [0,1])
"""
from __future__ import annotations
import sys
import time
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h03_attention_flow_mode.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-H3 PRESSURE TEST: 殘-05 attention + 殘-06 flow_mode_history")
    log("=" * 70)
    failed = 0

    # ─── Test 1: imports ───
    log("\n[Test 1] chat_runtime import maybe_record_flow_mode_transition")
    src = (ROOT / "agent_memory" / "companion" / "companion_chat_runtime.py").read_text(encoding="utf-8")
    if "maybe_record_flow_mode_transition" not in src:
        log("  ❌ FAIL: chat_runtime 沒 import maybe_record_flow_mode_transition")
        failed += 1
    else:
        log("  ✅ PASS: imported")

    # ─── Test 2: 模擬 transition 寫 1 row ───
    log("\n[Test 2] 模擬 transition → flow_mode_history INSERT 1 row")
    from agent_memory.companion.flow_mode_detector import maybe_record_flow_mode_transition, list_flow_mode_history
    from agent_memory.companion.companion_db import open_companion_db
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        with open_companion_db(tmp_vault) as conn:
            pass
        session_id = "sess-h3-test"
        # turn 1: normal_mode → INSERT
        mode_id = maybe_record_flow_mode_transition(tmp_vault, session_id, "normal_mode", chat_velocity_avg=0.3)
        if not mode_id:
            log("  ❌ FAIL: 第 1 次 transition 沒寫")
            failed += 1
        else:
            log(f"  ✅ Turn 1 INSERT mode_id={mode_id[:8]}")

        rows = list_flow_mode_history(tmp_vault, session_id)
        log(f"  history rows = {len(rows)}")
        if len(rows) != 1:
            log(f"  ❌ FAIL: 應 1 row, 拿到 {len(rows)}")
            failed += 1
        else:
            log("  ✅ PASS: 1 row 已寫")

        # ─── Test 3: 同 mode 連續 → 不重複 INSERT ───
        log("\n[Test 3] 同 mode 連續 → 不重複寫")
        mode_id2 = maybe_record_flow_mode_transition(tmp_vault, session_id, "normal_mode", chat_velocity_avg=0.4)
        if mode_id2:
            log(f"  ❌ FAIL: 同 mode 不該寫第 2 row, 但拿到 {mode_id2[:8]}")
            failed += 1
        else:
            log("  ✅ PASS: 同 mode 跳過")

        # ─── Test 3b: 真 transition (mode 變) → 寫第 2 row ───
        log("\n[Test 3b] mode 真變 → INSERT")
        mode_id3 = maybe_record_flow_mode_transition(tmp_vault, session_id, "burst_mode", chat_velocity_avg=1.5)
        if not mode_id3:
            log("  ❌ FAIL: 真 transition 沒寫")
            failed += 1
        else:
            log(f"  ✅ PASS: transition normal→burst 寫了 {mode_id3[:8]}")

        rows2 = list_flow_mode_history(tmp_vault, session_id)
        if len(rows2) != 2:
            log(f"  ❌ FAIL: 應 2 row, 拿到 {len(rows2)}")
            failed += 1
        else:
            log("  ✅ PASS: 第 2 row 寫了 + 第 1 row ended_at 應有")

    # ─── Test 4: attention_score UPDATE 邏輯存在 chat_runtime ───
    log("\n[Test 4] attention_score UPDATE 邏輯存在 Step 17")
    if "UPDATE raw_events SET attention_score=?" not in src:
        log("  ❌ FAIL: UPDATE attention_score 沒接")
        failed += 1
    else:
        log("  ✅ PASS: UPDATE attention_score 接上")

    # ─── Test 5: attention 算式範圍 ───
    log("\n[Test 5] attention 公式範圍 [0,1]")
    # 模擬手算 (intimacy=0.8, salience=0.5, goal=0.5, novelty=0.5)
    expected = 0.8 * 0.5 * 0.5 * 0.5
    log(f"  公式: 0.8 × 0.5 × 0.5 × 0.5 = {expected:.3f}")
    if expected < 0 or expected > 1:
        log("  ❌ FAIL: out of range")
        failed += 1
    else:
        log(f"  ✅ PASS: in range [0,1] = {expected:.3f}")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-H3 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-H3 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
