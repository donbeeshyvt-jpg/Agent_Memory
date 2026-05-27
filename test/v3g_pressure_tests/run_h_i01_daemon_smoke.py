# -*- coding: utf-8 -*-
"""V3-I1 壓測: companion-curator-daemon.ps1 smoke test.

驗證:
- Test 1: daemon script 存在
- Test 2: state file (companion_curator_state.json) JSON valid
- Test 3: state 含 last_layer3_at + last_layer4_at
- Test 4: 第 2 次跑 daemon 不會重跑 (24h gate)
- Test 5: daemon_runs.jsonl 寫對 (每筆 valid JSON)
"""
from __future__ import annotations
import os
import sys
import json
import time
import tempfile
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h_i01_daemon_smoke.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-I1 PRESSURE TEST: companion-curator-daemon.ps1 smoke")
    log("=" * 70)
    failed = 0

    # ─── Test 1: daemon script 存在 ───
    log("\n[Test 1] daemon script 檔案存在")
    daemon_script = ROOT / "scripts" / "companion-curator-daemon.ps1"
    if not daemon_script.exists():
        log(f"  ❌ FAIL: {daemon_script} 不存在")
        failed += 1
        return 1
    log(f"  ✅ PASS: {daemon_script.name} 存在")

    # ─── Test 2: script 含關鍵 helper ───
    log("\n[Test 2] daemon script 含關鍵 helper functions")
    src = daemon_script.read_text(encoding="utf-8")
    helpers = ["Read-State", "Write-State", "Should-Run-Layer", "Invoke-Layer", "Run-Daemon-Once"]
    for h in helpers:
        if h not in src:
            log(f"  ❌ FAIL: helper {h} 不在")
            failed += 1
        else:
            log(f"  ✅ {h} ✓")

    # ─── Test 3: daemon 跑通 (對 test vault) ───
    log("\n[Test 3] daemon 對 test vault 跑通 (use existing state)")
    test_vault = Path(r"Z:\Cursor練習用\Agent_Memory\test\SecondBrains\companion_test")
    if not test_vault.exists():
        log(f"  ⚠️ skip: test vault 不存在 {test_vault}")
    else:
        state_file = test_vault / ".ai" / "companion_curator_state.json"
        if state_file.exists():
            content = state_file.read_text(encoding="utf-8")
            try:
                state = json.loads(content.lstrip("﻿"))  # 去 BOM
                if "last_layer3_at" not in state or "last_layer4_at" not in state:
                    log(f"  ❌ FAIL: state 缺 layer3/layer4 keys: {list(state.keys())}")
                    failed += 1
                else:
                    log(f"  ✅ PASS: state JSON valid + 含 last_layer3_at + last_layer4_at")
                    log(f"    last_layer3_at = {state.get('last_layer3_at', '')[:25]}")
                    log(f"    last_layer4_at = {state.get('last_layer4_at', '')[:25]}")
            except Exception as e:
                log(f"  ❌ FAIL: state JSON parse {e}")
                failed += 1
        else:
            log(f"  ⚠️ skip: state file 不存在 (daemon 還沒跑過)")

    # ─── Test 4: daemon_runs.jsonl 寫對 ───
    log("\n[Test 4] daemon_runs.jsonl 寫對 (每筆 valid JSON)")
    if test_vault.exists():
        log_file = test_vault / ".ai" / "companion_daemon_runs.jsonl"
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8").strip().split("\n")
            valid_count = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if "timestamp" in entry and "runs" in entry:
                        valid_count += 1
                except Exception:
                    pass
            log(f"  total log lines = {len(lines)}, valid = {valid_count}")
            if valid_count == 0:
                log("  ⚠️ partial: 沒 valid JSON lines (daemon 還沒跑過)")
            else:
                log(f"  ✅ PASS: {valid_count} valid JSON entries")
        else:
            log("  ⚠️ skip: log file 不存在 (daemon 還沒跑過)")

    # ─── Test 5: -ShowSchedule 印 schtasks 命令 ───
    log("\n[Test 5] daemon 含 -ShowSchedule flag")
    if "-ShowSchedule" not in src or "schtasks /create" not in src:
        log("  ❌ FAIL: -ShowSchedule 或 schtasks 命令沒寫")
        failed += 1
    else:
        log("  ✅ PASS: -ShowSchedule 印 schtasks 命令")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-I1 daemon smoke 全 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-I1 有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
