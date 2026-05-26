# -*- coding: utf-8 -*-
"""V3-G1 壓測: memory_ctx[:600]→[:2400] 真的撈完整 4-layer memory.

對齊 audit Plan D + V3 §13 Memory Router 3000 char budget 設計.

驗證:
- Test 1: memory_context 2400 char → section F 含 ≥ 2000 char (V3-G1 生效)
- Test 2: memory_context 短 → section F 仍正常
- Test 3: regression — V3-E6/E7/E9 既有 section 不受影響
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "g01_memory_ctx_budget.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-G1 PRESSURE TEST: memory_ctx[:600]→[:2400] (audit Plan D)")
    log("=" * 70)

    from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt

    failed = 0

    # ─── Test 1: memory_ctx 長 (2400 char) → section F 應 ≥ 2000 char ───
    log("\n[Test 1] memory_ctx=2400 char → section F 應 ≥ 2000")
    long_mem = "L1_recent " + ("X" * 600) + " L2_episodic " + ("Y" * 1000) + " L3_owner " + ("Z" * 600) + " L4_dynamic " + ("W" * 130)
    log(f"  input memory_context len = {len(long_mem)} char")
    packet = {
        "affect": {"valence": 0.0, "arousal": 0.3, "dominance": 0.5, "uncertainty": 0.3},
        "emotion": {"joy": 0.5, "dominant_emotion": "joy"},
        "balance": {"balance_axis": 0.0},
        "policy": {"strategy": "calm_clear", "tone": "calm", "intimacy_score": 0.5, "is_owner": True},
        "decision": "ALLOW",
        "memory_context": long_mem,
        "system_persona": "test",
    }
    prompt = _build_companion_system_prompt(packet, vault_root=None)
    sec_f_start = prompt.find("[F. 最近相關記憶")
    if sec_f_start < 0:
        log("  ❌ FAIL: section F 不存在於 prompt")
        failed += 1
    else:
        # 抓到下一個 [X.] section 為止
        sec_f_end = prompt.find("\n\n[", sec_f_start + 1)
        if sec_f_end < 0:
            sec_f_end = len(prompt)
        sec_f = prompt[sec_f_start:sec_f_end]
        log(f"  section F len = {len(sec_f)} char")
        if len(sec_f) < 2000:
            log(f"  ❌ FAIL: section F < 2000 (V3-G1 沒生效)")
            failed += 1
        else:
            log(f"  ✅ PASS: section F ≥ 2000 char ({len(sec_f)})")

    # ─── Test 2: memory_ctx 短 → section F 仍正常包含 ───
    log("\n[Test 2] memory_ctx 短 → section F 仍正常 (regression)")
    short_mem = "L1: 你說過你今天累; L2: 上週也提過工作累"
    packet2 = dict(packet, memory_context=short_mem)
    prompt2 = _build_companion_system_prompt(packet2, vault_root=None)
    sec_f2_start = prompt2.find("[F. 最近相關記憶")
    if sec_f2_start < 0:
        log("  ❌ FAIL: section F 不存在 (regression)")
        failed += 1
    elif short_mem not in prompt2:
        log("  ❌ FAIL: short_mem 內容沒進 prompt")
        failed += 1
    else:
        log(f"  ✅ PASS: short memory 也正常 ({len(short_mem)} char 都進去)")

    # ─── Test 3: empty memory_ctx → section F 不出現 (V3-E5 既有行為) ───
    log("\n[Test 3] memory_ctx=空 → section F 不出現 (規範 regression)")
    packet3 = dict(packet, memory_context="")
    prompt3 = _build_companion_system_prompt(packet3, vault_root=None)
    if "[F. 最近相關記憶" in prompt3:
        log("  ❌ FAIL: 空 memory_ctx 還是出現 section F")
        failed += 1
    else:
        log("  ✅ PASS: 空 memory_ctx → section F 不出現")

    # ─── Test 4: V3-E6/E7/E9 既有 section 仍正常 ───
    log("\n[Test 4] V3-E6/E7/E9 既有 section 不受影響")
    checks = {
        "[E. 我現在的感受": "V3-E7",
        "[G+. ⭐⭐⭐ 綜合應用": "V3-E6/E9",
        "[H. Output 限制": "V3-E5",
        "[D. 紅線": "V3-D6",
    }
    for kw, ver in checks.items():
        if kw in prompt:
            log(f"  ✅ {ver} section '{kw[:25]}...' 存在")
        else:
            log(f"  ❌ FAIL: {ver} section '{kw}' 不見了")
            failed += 1

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-G1 全壓測 PASS (4/4)")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-G1 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
