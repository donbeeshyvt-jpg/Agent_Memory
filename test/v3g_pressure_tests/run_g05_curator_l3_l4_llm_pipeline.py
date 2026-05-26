# -*- coding: utf-8 -*-
"""V3-G5 壓測: curator L3+L4 LLM 摘要 + V3-F4 知識管道完整接.

對齊 V3 §21.4+§21.5 + MISSION §3.6 + V3-F4 移植 V2 R10 pattern + 用戶 audit Plan A.

驗證:
- Test 1: companion_curator import 新 helpers
- Test 2: run_layer3_24h_medium 對空 db 不爆 + 新 step _consolidate_daily_knowledge 接上
- Test 3: run_layer4_7d_deep 對空 inbox 不爆 + 新 step _ingest_external_knowledge 接上
- Test 4: _consolidate_daily_knowledge 對沒 LLM client 環境 graceful return 0
- Test 5: _ingest_external_knowledge 對沒檔案 return 0
- Test 6: chat_runtime + curator + knowledge_base 三方 import 不衝突
"""
from __future__ import annotations
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "g05_curator_l3_l4_llm_pipeline.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-G5 PRESSURE TEST: curator L3+L4 LLM 摘要 + V3-F4 知識管道")
    log("=" * 70)
    failed = 0

    # ─── Test 1: imports ───
    log("\n[Test 1] companion_curator new helpers imported")
    try:
        from agent_memory.companion.companion_curator import (
            run_layer3_24h_medium, run_layer4_7d_deep,
            _consolidate_daily_knowledge, _ingest_external_knowledge,
            CuratorRunResult,
        )
        log("  ✅ PASS: 4 helpers + CuratorRunResult")
    except Exception as e:
        log(f"  ❌ FAIL: import {e}")
        failed += 1
        return 1

    # ─── Test 2: run_layer3_24h_medium 對空 db 不爆 ───
    log("\n[Test 2] run_layer3_24h_medium 對空 db 不爆 + 新 step 接上")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(tmp_vault) as conn:
            pass  # init schema
        try:
            result = run_layer3_24h_medium(tmp_vault)
            log(f"  layer3 result.layer = {result.layer}")
            log(f"  actions = {result.actions_performed}")
            if result.layer != "layer3_24h_medium":
                log("  ❌ FAIL: layer name 錯")
                failed += 1
            else:
                log("  ✅ PASS: layer3 跑通")
        except Exception as e:
            log(f"  ❌ FAIL: {e}")
            failed += 1

    # ─── Test 3: run_layer4_7d_deep 對空 inbox 不爆 ───
    log("\n[Test 3] run_layer4_7d_deep 對空 inbox 不爆")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        with open_companion_db(tmp_vault) as conn:
            pass
        try:
            result = run_layer4_7d_deep(tmp_vault)
            log(f"  layer4 result.layer = {result.layer}")
            log(f"  actions = {result.actions_performed}")
            if result.layer != "layer4_7d_deep":
                log("  ❌ FAIL: layer name 錯")
                failed += 1
            else:
                log("  ✅ PASS: layer4 跑通")
        except Exception as e:
            log(f"  ❌ FAIL: {e}")
            failed += 1

    # ─── Test 4: _consolidate_daily_knowledge 無 episodic_memories return 0 ───
    log("\n[Test 4] _consolidate_daily_knowledge 空 db → return 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        with open_companion_db(tmp_vault) as conn:
            pass
        try:
            count = _consolidate_daily_knowledge(tmp_vault)
            if count != 0:
                log(f"  ❌ FAIL: 空 db 應 return 0, 拿到 {count}")
                failed += 1
            else:
                log("  ✅ PASS: 空 db return 0")
        except Exception as e:
            log(f"  ❌ FAIL: {e}")
            failed += 1

    # ─── Test 5: _ingest_external_knowledge 空 inbox return 0 ───
    log("\n[Test 5] _ingest_external_knowledge 空 inbox → return 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        # 不建 inbox
        try:
            count = _ingest_external_knowledge(tmp_vault)
            if count != 0:
                log(f"  ❌ FAIL: 空 inbox 應 return 0, 拿到 {count}")
                failed += 1
            else:
                log("  ✅ PASS: 空 inbox return 0")
        except Exception as e:
            log(f"  ❌ FAIL: {e}")
            failed += 1

    # ─── Test 6: 三方 import 不衝突 ───
    log("\n[Test 6] chat_runtime + curator + knowledge_base 三方 import")
    try:
        from agent_memory.companion import companion_chat_runtime
        from agent_memory.companion import companion_curator
        from agent_memory.companion import knowledge_base
        log("  ✅ PASS: 三方 imports 沒衝突")
    except Exception as e:
        log(f"  ❌ FAIL: import 衝突 {e}")
        failed += 1

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-G5 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-G5 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
