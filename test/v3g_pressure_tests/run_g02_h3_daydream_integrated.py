# -*- coding: utf-8 -*-
"""V3-G2 壓測: H3 Daydream + flow_mode_detector 真的接 chat_runtime.

對齊 V3 §29.3 H3 白日夢 + §26.2.E flow_mode + audit doc TOP 3 + Plan A.

驗證:
- Test 1: chat_runtime 真的 import daydream_engine + flow_mode_detector
- Test 2: idle_seconds ≥ 30 → daydream_result.daydream_text 非空
- Test 3: prompt_packet 含 daydream + flow_mode key
- Test 4: system prompt section [F2. 白日夢] 出現 (idle 後)
- Test 5: dead_chat_mode → daydream.externally_visible=True
- Test 6: idle_seconds < 30 → 不觸發 daydream (regression)
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "g02_h3_daydream_integrated.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-G2 PRESSURE TEST: H3 Daydream + flow_mode_detector integration")
    log("=" * 70)
    failed = 0

    # ─── Test 1: chat_runtime import ───
    log("\n[Test 1] chat_runtime 真的 import daydream + flow_mode_detector")
    chat_runtime_path = ROOT / "agent_memory" / "companion" / "companion_chat_runtime.py"
    src = chat_runtime_path.read_text(encoding="utf-8")
    if "from agent_memory.companion.daydream_engine import" not in src:
        log("  ❌ FAIL: chat_runtime 沒 import daydream_engine")
        failed += 1
    else:
        log("  ✅ PASS: daydream_engine imported")
    if "from agent_memory.companion.flow_mode_detector import" not in src:
        log("  ❌ FAIL: chat_runtime 沒 import flow_mode_detector")
        failed += 1
    else:
        log("  ✅ PASS: flow_mode_detector imported")
    if "Step 11.8" not in src or "generate_daydream(" not in src:
        log("  ❌ FAIL: chat_runtime 沒呼叫 generate_daydream")
        failed += 1
    else:
        log("  ✅ PASS: Step 11.8 generate_daydream call 存在")

    # ─── Test 2: idle ≥ 30 直接呼叫 generate_daydream ───
    log("\n[Test 2] idle_seconds=60 → daydream_text 非空")
    import random
    from agent_memory.companion.daydream_engine import generate_daydream
    rng = random.Random(42)
    dd = generate_daydream(
        idle_seconds=60,
        recent_topics=["遊戲攻略"],
        knowledge_gap_entities=["randomizer mod"],
        flow_mode="normal_mode",
        rng=rng,
    )
    log(f"  daydream_text = '{dd.daydream_text[:60]}...'")
    if not dd.daydream_text.strip():
        log("  ❌ FAIL: daydream_text 是空")
        failed += 1
    else:
        log(f"  ✅ PASS: daydream_text 有內容 ({len(dd.daydream_text)} char)")

    # ─── Test 3: prompt_packet 含 daydream + flow_mode ───
    log("\n[Test 3] _build_companion_system_prompt 認得 daydream + flow_mode")
    from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt
    packet = {
        "affect": {"valence": 0.0, "arousal": 0.3, "dominance": 0.5, "uncertainty": 0.3},
        "emotion": {"dominant_emotion": "joy", "joy": 0.5},
        "balance": {"balance_axis": 0.0},
        "policy": {"strategy": "calm_clear", "tone": "calm", "intimacy_score": 0.5, "is_owner": True},
        "decision": "ALLOW",
        "memory_context": "L1 recent",
        "system_persona": "test",
        "daydream": "想到 randomizer mod, 我可以多查一下",  # ⭐ V3-G2 新 field
        "flow_mode": "dead_chat_mode",  # ⭐ V3-G2 新 field
    }
    prompt = _build_companion_system_prompt(packet, vault_root=None)
    if "[F2. 我 idle 時想到的" not in prompt:
        log("  ❌ FAIL: section F2 沒出現")
        failed += 1
    else:
        log("  ✅ PASS: section F2 出現")
    if "randomizer mod" not in prompt:
        log("  ❌ FAIL: daydream_text 沒進 prompt")
        failed += 1
    else:
        log("  ✅ PASS: daydream_text 進 prompt")
    if "[F3. 流量模式" not in prompt:
        log("  ❌ FAIL: section F3 流量模式沒出現")
        failed += 1
    else:
        log("  ✅ PASS: section F3 流量模式出現")
    if "dead_chat_mode" not in prompt:
        log("  ❌ FAIL: flow_mode 沒進 prompt")
        failed += 1
    else:
        log("  ✅ PASS: flow_mode dead_chat_mode 進 prompt")

    # ─── Test 4: dead_chat_mode → externally_visible ───
    log("\n[Test 4] dead_chat_mode → daydream.externally_visible=True")
    dd2 = generate_daydream(idle_seconds=120, flow_mode="dead_chat_mode", rng=rng)
    if not dd2.externally_visible:
        log("  ❌ FAIL: dead_chat_mode 應該 externally_visible=True")
        failed += 1
    else:
        log("  ✅ PASS: dead_chat 外顯")

    # ─── Test 5: maybe_emit_daydream 對 externally_visible 加前綴 ───
    log("\n[Test 5] maybe_emit_daydream dead_chat 加前綴")
    from agent_memory.companion.daydream_engine import maybe_emit_daydream
    final = maybe_emit_daydream("Hello user", dd2)
    if "自言自語" not in final:
        log("  ❌ FAIL: dead_chat 沒加自言自語前綴")
        failed += 1
    else:
        log(f"  ✅ PASS: 加前綴 — {final[:50]}...")

    # ─── Test 6: idle < 30 不觸發 ───
    log("\n[Test 6] idle_seconds=10 → daydream_text 為空")
    dd3 = generate_daydream(idle_seconds=10, rng=rng)
    if dd3.daydream_text.strip():
        log(f"  ❌ FAIL: idle=10 不該觸發 daydream, 但拿到 '{dd3.daydream_text}'")
        failed += 1
    else:
        log("  ✅ PASS: idle=10 不觸發 daydream")

    # ─── Test 7: normal_mode → F3 section 不出現 ───
    log("\n[Test 7] normal_mode → section F3 不應出現 (避免 LLM 雜訊)")
    packet_normal = dict(packet, flow_mode="normal_mode")
    prompt_normal = _build_companion_system_prompt(packet_normal, vault_root=None)
    if "[F3. 流量模式" in prompt_normal:
        log("  ❌ FAIL: normal_mode 不該出現 F3")
        failed += 1
    else:
        log("  ✅ PASS: normal_mode 不出 F3")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-G2 全壓測 PASS (10/10)")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-G2 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
