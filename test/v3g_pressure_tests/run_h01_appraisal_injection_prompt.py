# -*- coding: utf-8 -*-
"""V3-H1 壓測: 殘-01 appraisal + 殘-02 injection 進 prompt.

對齊 V3-H 殘留修補規劃 doc P0 HIGH.

驗證:
- Test 1: _humanize_affect 加 appraisal 參數
- Test 2: appraisal goal_congruence<-0.3 → 「事情卡住了」
- Test 3: appraisal certainty<0.3 → 「不太確定」
- Test 4: appraisal identity_relevance>0.7 → 「跟我這角色有關」
- Test 5: prompt_packet 新增 appraisal field
- Test 6: chat_runtime _load_recent_injection_hint imported
- Test 7: injection_hint 非空 → section D'' 出現
- Test 8: 沒 injection 紀錄 → section D'' 不出現
"""
from __future__ import annotations
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h01_appraisal_injection_prompt.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-H1 PRESSURE TEST: 殘-01 appraisal + 殘-02 injection 進 prompt")
    log("=" * 70)
    failed = 0

    # ─── Test 1: _humanize_affect 接受 appraisal 參數 ───
    log("\n[Test 1] _humanize_affect 加 appraisal 參數")
    from agent_memory.companion.companion_chat_runtime import _humanize_affect
    affect = {"valence": -0.4, "arousal": 0.5, "dominance": 0.4, "uncertainty": 0.5}
    emotion = {"sadness": 0.6, "dominant_emotion": "sadness", "joy": 0.2}
    balance = {"balance_axis": -0.1, "playfulness": 0.3}
    policy = {"intimacy_score": 0.8, "is_owner": True}
    appraisal = {
        "goal_congruence": -0.5, "certainty": 0.2, "control": 0.3,
        "norm_fit": 1.0, "identity_relevance": 0.3, "novelty": 0.5, "relationship_impact": 0.0,
    }
    try:
        result = _humanize_affect(affect, emotion, balance, policy, appraisal=appraisal)
        log(f"  result preview: {result[:200]}")
        log("  ✅ PASS: 接受 appraisal kwarg")
    except TypeError as e:
        log(f"  ❌ FAIL: 沒接受 appraisal kwarg {e}")
        failed += 1
        return 1

    # ─── Test 2-4: appraisal hint 文字 ───
    log("\n[Test 2-4] appraisal hint 翻譯文字")
    if "事情卡住了" not in result:
        log(f"  ❌ FAIL: goal_congruence=-0.5 應觸發「事情卡住了」")
        failed += 1
    else:
        log("  ✅ Test 2 PASS: 「事情卡住了」")
    if "不太確定" not in result:
        log(f"  ❌ FAIL: certainty=0.2 應觸發「不太確定」")
        failed += 1
    else:
        log("  ✅ Test 3 PASS: 「不太確定」")
    if "沒法掌控" not in result:
        log(f"  ❌ FAIL: control=0.3 應觸發「沒法掌控」")
        failed += 1
    else:
        log("  ✅ Test 4 PASS: 「沒法掌控」")

    # ─── Test 4b: identity_relevance > 0.7 ───
    log("\n[Test 4b] identity_relevance>0.7 → 「跟我這角色有關」")
    apr_id = dict(appraisal, identity_relevance=0.8)
    result_id = _humanize_affect(affect, emotion, balance, policy, appraisal=apr_id)
    if "這個角色" not in result_id and "自我有關" not in result_id:
        log("  ❌ FAIL: identity_relevance hint 沒觸發")
        failed += 1
    else:
        log("  ✅ PASS: identity hint 觸發")

    # ─── Test 5: prompt_packet appraisal field ───
    log("\n[Test 5] _build_companion_system_prompt 從 packet 拿 appraisal")
    from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt
    packet = {
        "affect": affect, "emotion": emotion, "balance": balance, "policy": policy,
        "decision": "ALLOW", "memory_context": "", "system_persona": "test",
        "appraisal": appraisal,
    }
    prompt = _build_companion_system_prompt(packet, vault_root=None)
    if "事情卡住了" not in prompt:
        log("  ❌ FAIL: appraisal 沒進 prompt section E")
        failed += 1
    else:
        log("  ✅ PASS: appraisal 進 prompt")

    # ─── Test 6: injection hint helper ───
    log("\n[Test 6] _load_recent_injection_hint helper imported")
    from agent_memory.companion.companion_chat_runtime import _load_recent_injection_hint
    log("  ✅ PASS: _load_recent_injection_hint imported")

    # ─── Test 7: 有 injection 紀錄 → section D'' ───
    log("\n[Test 7] injection_hint 非空 → section D'' 出現")
    packet_with_hint = dict(packet, injection_hint="⚠️ 警覺: 過去 24h 嘗試 3 次注入攻擊")
    prompt_d = _build_companion_system_prompt(packet_with_hint, vault_root=None)
    if "[D''. 警覺提示" not in prompt_d:
        log("  ❌ FAIL: section D'' 沒出現")
        failed += 1
    else:
        log("  ✅ PASS: section D'' 出現")
    if "嘗試 3 次注入攻擊" not in prompt_d:
        log("  ❌ FAIL: hint 內容沒進 prompt")
        failed += 1
    else:
        log("  ✅ PASS: hint 內容對")

    # ─── Test 8: 空 injection_hint → section D'' 不出現 ───
    log("\n[Test 8] 空 injection_hint → section D'' 不出現")
    packet_empty = dict(packet, injection_hint="")
    prompt_e = _build_companion_system_prompt(packet_empty, vault_root=None)
    if "[D''. 警覺提示" in prompt_e:
        log("  ❌ FAIL: 空 hint 不該出 section D''")
        failed += 1
    else:
        log("  ✅ PASS: 空 hint → section D'' 不出 (避免雜訊)")

    # ─── Test 9: _load_recent_injection_hint 對空 db return 空 ───
    log("\n[Test 9] _load_recent_injection_hint 對空 db return 空")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        from agent_memory.companion.companion_db import open_companion_db
        with open_companion_db(tmp_vault) as conn:
            pass
        h = _load_recent_injection_hint(tmp_vault, "user-test")
        if h:
            log(f"  ❌ FAIL: 空 db 應回 '', 拿到 {h!r}")
            failed += 1
        else:
            log("  ✅ PASS: 空 db 回空")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-H1 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-H1 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
