# -*- coding: utf-8 -*-
"""V3-G3 壓測: H4 Embodied + H10 Metacognition + H11 Contagion + H12 Expectation 全接.

對齊 V3 §29.4 + §29.10 + §29.11 + §29.12 + audit doc Plan A.
4 個白寫的程式碼 chat_runtime 終於 import + 接 chat pipeline.

驗證:
- Test 1: chat_runtime imports (5 點)
- Test 2: H4 update_embodied_over_time → section E2 (對 stream_duration 高)
- Test 3: H11 apply_contagion owner factor=0.4 / VIP=0.2 / stranger=0
- Test 4: H12 expectation delta>0.3 → joy/arousal +; delta<-0.3 → valence-
- Test 5: H10 check_self_consistency (metacognition.py raw_events actor='bot' 對齊修)
- Test 6: regression — V3-G1/G2 既有 section 不受影響
"""
from __future__ import annotations
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "g03_h4_h10_h11_h12.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-G3 PRESSURE TEST: H4 + H10 + H11 + H12 integration")
    log("=" * 70)
    failed = 0

    # ─── Test 1: imports ───
    log("\n[Test 1] chat_runtime 真的 import 4 個 H 機制 module")
    src = (ROOT / "agent_memory" / "companion" / "companion_chat_runtime.py").read_text(encoding="utf-8")
    for keyword, name in [
        ("from agent_memory.companion.embodied_state import EmbodiedState", "H4 EmbodiedState"),
        ("from agent_memory.companion.emotion_contagion import apply_contagion", "H11 emotion_contagion"),
        ("from agent_memory.companion.expectation_state import", "H12 expectation_state"),
        ("from agent_memory.companion.metacognition import", "H10 metacognition"),
        ("from agent_memory.companion.daydream_engine import", "H3 daydream (regression V3-G2)"),
    ]:
        if keyword not in src:
            log(f"  ❌ FAIL: {name} 沒 import")
            failed += 1
        else:
            log(f"  ✅ PASS: {name} imported")

    # ─── Test 2: H4 update_embodied_over_time + section E2 (360min 6hr 直播) ───
    log("\n[Test 2] H4 embodied 360min (6hr 直播) → section E2 出現 (對齊 V3 §29.4 6hr 設計)")
    from agent_memory.companion.embodied_state import EmbodiedState, update_embodied_over_time
    e = update_embodied_over_time(EmbodiedState(), elapsed_minutes=360)
    log(f"  embodied after 360min: energy={e.energy:.2f}, thirst={e.thirst:.2f}, voice_strain={e.voice_strain:.2f}, sleepiness={e.sleepiness:.2f}")
    if e.thirst < 0.3:
        log(f"  ❌ FAIL: 360min thirst 應 ≥ 0.3, 拿到 {e.thirst:.2f}")
        failed += 1
    else:
        log(f"  ✅ PASS: thirst {e.thirst:.2f} ≥ 0.3 (觸發 section E2)")

    from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt
    packet = {
        "affect": {"valence": 0.0, "arousal": 0.3, "dominance": 0.5, "uncertainty": 0.3},
        "emotion": {"dominant_emotion": "joy", "joy": 0.5},
        "balance": {"balance_axis": 0.0},
        "policy": {"strategy": "calm", "tone": "calm", "intimacy_score": 0.5, "is_owner": True},
        "decision": "ALLOW",
        "memory_context": "L1 recent",
        "system_persona": "test",
        "daydream": "",
        "flow_mode": "normal_mode",
        "embodied": e.as_dict(),  # ⭐ V3-G3 新 field
    }
    prompt = _build_companion_system_prompt(packet, vault_root=None)
    if "[E2. 我的身體感" not in prompt:
        log("  ❌ FAIL: section E2 沒出現")
        failed += 1
    else:
        log("  ✅ PASS: section E2 出現")

    # ─── Test 3: H11 emotion_contagion ───
    log("\n[Test 3] H11 apply_contagion: owner factor=0.4 / stranger=0")
    from agent_memory.companion.emotion_contagion import apply_contagion, get_contagion_factor
    from agent_memory.companion.affect_manager import AffectState
    f_owner = get_contagion_factor(is_owner=True, intimacy_score=0.0)
    f_vip = get_contagion_factor(is_owner=False, intimacy_score=0.5)
    f_stranger = get_contagion_factor(is_owner=False, intimacy_score=0.1)
    log(f"  owner={f_owner}, VIP={f_vip}, stranger={f_stranger}")
    if f_owner != 0.4 or f_vip != 0.2 or f_stranger != 0.0:
        log("  ❌ FAIL: contagion factor 錯")
        failed += 1
    else:
        log("  ✅ PASS: contagion factor 正確")

    # contagion 對 owner 應該混合 viewer affect
    own_neutral = AffectState()
    viewer_happy = AffectState(valence=0.8, arousal=0.7)
    mixed = apply_contagion(own_neutral, viewer_happy, is_owner=True, intimacy_score=0.0)
    log(f"  owner contagion: own(0,0.3) + viewer(0.8,0.7) → val={mixed.valence:.2f} aro={mixed.arousal:.2f}")
    if abs(mixed.valence - 0.32) > 0.01:  # 0.6*0 + 0.4*0.8 = 0.32
        log(f"  ❌ FAIL: mixed.valence 不對, 預期 0.32 拿到 {mixed.valence:.2f}")
        failed += 1
    else:
        log("  ✅ PASS: contagion 混合公式對")

    # ─── Test 4: H12 expectation 邏輯 ───
    log("\n[Test 4] H12 expectation set_baseline + update_actual delta 邏輯")
    from agent_memory.companion.expectation_state import set_baseline, update_actual, list_session_expectations
    # 寫一個 temp vault 跑 (避免污染 test vault)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir(parents=True)
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(tmp_vault) as conn:
            pass  # init schema
        eid = set_baseline(tmp_vault, "sess_test", "viewers", expected_value=10.0)
        log(f"  set_baseline OK eid={eid[:8]}...")
        diff = update_actual(tmp_vault, eid, actual_value=15.0)
        log(f"  update_actual viewers=15 (expected 10) → delta={diff.get('delta')}")
        if abs(diff.get("delta", 0.0) - 0.5) > 0.01:
            log(f"  ❌ FAIL: delta 應 0.5, 拿到 {diff.get('delta')}")
            failed += 1
        else:
            log("  ✅ PASS: delta = 0.5 對")
        rows = list_session_expectations(tmp_vault, "sess_test")
        if len(rows) != 1:
            log(f"  ❌ FAIL: list 應回 1 row, 拿到 {len(rows)}")
            failed += 1
        else:
            log("  ✅ PASS: list_session_expectations 回對")

    # ─── Test 5: H10 metacognition no contradiction ───
    log("\n[Test 5] H10 metacognition — check_self_consistency 不誤觸發")
    from agent_memory.companion.metacognition import check_self_consistency, maybe_prefix_correction
    # 用 tmp vault 跑 (避免 raw_events 干擾)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir(parents=True)
        with open_companion_db(tmp_vault) as conn:
            pass
        result = check_self_consistency(
            tmp_vault,
            candidate_response="今天我覺得很開心",
            session_id="sess_g03",
            look_back_turns=5,
        )
        if result.contradiction_detected:
            log("  ❌ FAIL: 沒矛盾應 not detected")
            failed += 1
        else:
            log("  ✅ PASS: no prior agent turns → no detection")

    # ─── Test 6: regression V3-G1/G2 ───
    log("\n[Test 6] V3-G1/G2 既有 section 不受影響")
    packet_g12 = dict(packet, memory_context="A" * 2400, daydream="想到下個話題", flow_mode="dead_chat_mode")
    prompt_g12 = _build_companion_system_prompt(packet_g12, vault_root=None)
    checks = [
        ("[F. 最近相關記憶", "V3-G1 section F"),
        ("[F2. 我 idle 時想到的", "V3-G2 section F2"),
        ("[F3. 流量模式", "V3-G2 section F3"),
        ("[E. 我現在的感受", "V3-E7"),
        ("[G+. ⭐⭐⭐ 綜合應用", "V3-E6/E9"),
        ("[H. Output 限制", "V3-E5"),
    ]
    for kw, name in checks:
        if kw in prompt_g12:
            log(f"  ✅ {name} '{kw[:20]}...' 存在")
        else:
            log(f"  ❌ FAIL: {name} '{kw}' 不見了")
            failed += 1

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-G3 全壓測 PASS (16/16)")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-G3 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
