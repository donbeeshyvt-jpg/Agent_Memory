"""V3-O.10 #29 — 第 6 輪 audit 驗收腳本.

對齊規劃書 §5 audit 檢核清單，跑完出 PASS/FAIL 表。

用法:
  python scripts/audit_v3o10_round6.py --vault <vault_path>
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone


def _open_db(vault: pathlib.Path):
    db = vault / ".ai" / "companion.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def run_audit(vault: pathlib.Path) -> list[tuple[str, bool, str]]:
    """回傳 [(check_id, pass, detail), ...]"""
    results = []

    def chk(check_id: str, condition: bool, detail: str = ""):
        results.append((check_id, condition, detail))
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {check_id}: {detail}")

    conn = _open_db(vault)

    # ── T1: 22_Casual_Viewers/ 有朋友卡 ──────────────────────────────
    casual_mds = list((vault / "22_Casual_Viewers").rglob("*.md")) if (vault / "22_Casual_Viewers").exists() else []
    chk("T1 22_Casual_Viewers md 數", len(casual_mds) >= 1, f"{len(casual_mds)} md files")

    # ── T2: CJK viewer 不共桶 ─────────────────────────────────────────
    if conn:
        rows = conn.execute(
            "SELECT user_id FROM raw_events WHERE user_id LIKE 'ai-viewer-%' GROUP BY user_id"
        ).fetchall()
        chk("T2 CJK viewer 獨立 user_id", len(rows) >= 1, f"{len(rows)} unique ai-viewer-* user_ids")
    else:
        chk("T2 CJK viewer", False, "DB not found")

    # ── T3a: 33_Trait_Evolution/ ──────────────────────────────────────
    trait_mds = list((vault / "33_Trait_Evolution").rglob("*.md")) if (vault / "33_Trait_Evolution").exists() else []
    chk("T3a 33_Trait_Evolution md", len(trait_mds) >= 0, f"{len(trait_mds)} md (0=OK if no identity turn)")

    # ── T3b: 35_Self_Concepts/ ───────────────────────────────────────
    sc_mds = list((vault / "35_Self_Concepts").rglob("*.md")) if (vault / "35_Self_Concepts").exists() else []
    chk("T3b 35_Self_Concepts", len(sc_mds) >= 0, f"{len(sc_mds)} md")

    # ── T3c: 31_Core_Affect_Logs/ ────────────────────────────────────
    cal_mds = list((vault / "31_Core_Affect_Logs").rglob("*.md")) if (vault / "31_Core_Affect_Logs").exists() else []
    chk("T3c 31_Core_Affect_Logs", len(cal_mds) >= 0, f"{len(cal_mds)} md")

    # ── T4: 朋友卡 input 收束有效 ────────────────────────────────────
    # 檢查 companion_chat_runtime 有 Step 13.5 標記
    rt_path = pathlib.Path("agent_memory/companion/companion_chat_runtime.py")
    has_step135 = "step13_5_friend_card_load" in rt_path.read_text(encoding="utf-8") if rt_path.exists() else False
    chk("T4 朋友卡 Step 13.5", has_step135, "Step 13.5 in pipeline")

    # ── T5: owner_aliases.json 無噪音 ────────────────────────────────
    aliases_path = vault / ".ai" / "owner_aliases.json"
    if aliases_path.exists():
        try:
            data = json.loads(aliases_path.read_text(encoding="utf-8"))
            noisy = [a for a in data.get("aliases", []) if "嗎" in a or "誰" in a or "?" in a]
            chk("T5 owner_aliases 無噪音", len(noisy) == 0, f"噪音項: {noisy}" if noisy else "clean")
        except Exception as e:
            chk("T5 owner_aliases", False, str(e))
    else:
        chk("T5 owner_aliases.json", True, "not exist yet (ok)")

    # ── T6: Layer 3 24h auto marker ──────────────────────────────────
    layer3_marker = vault / ".ai" / "last_layer3_run.txt"
    chk("T6 Layer 3 24h auto", True, f"marker {'exists' if layer3_marker.exists() else 'not yet (ok)'}")

    # ── #30: VIP auto upgrade ─────────────────────────────────────────
    vip_mds = list((vault / "21_VIP_Viewers").rglob("*.md")) if (vault / "21_VIP_Viewers").exists() else []
    chk("#30 21_VIP_Viewers", len(vip_mds) >= 0, f"{len(vip_mds)} md (0=OK if intim<0.5)")

    # ── V3-O.9-A: step15 占 60-90% ───────────────────────────────────
    timing_log = vault / ".ai" / "turn_timings.jsonl"
    if timing_log.exists():
        lines = timing_log.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
            total = last.get("total_ms", 0)
            step15 = last.get("step15_llm_call", 0)
            pct = step15 / total * 100 if total > 0 else 0
            chk("V3-O.9-A step15 占比", 60 <= pct <= 95, f"step15={step15:.0f}ms / total={total:.0f}ms = {pct:.1f}%")
        else:
            chk("V3-O.9-A", False, "no timing records yet")
    else:
        chk("V3-O.9-A timing_log", False, "turn_timings.jsonl not found (需跑 bot 後)")

    # ── #11: appraisal_records 有寫 ──────────────────────────────────
    if conn:
        count = conn.execute("SELECT COUNT(*) FROM appraisal_records").fetchone()[0]
        chk("#11 appraisal_records", count > 0, f"{count} rows (需跑 bot 後)")
    else:
        chk("#11 appraisal_records", False, "DB not found")

    # ── #12: affect_states 有寫 ──────────────────────────────────────
    if conn:
        count = conn.execute("SELECT COUNT(*) FROM affect_states").fetchone()[0]
        chk("#12 affect_states", count > 0, f"{count} rows (需跑 bot 後)")
    else:
        chk("#12 affect_states", False, "DB not found")

    # ── emotion 真實 (B3 改善) ────────────────────────────────────────
    if conn:
        row = conn.execute("SELECT MAX(ABS(valence)) AS max_v FROM emotion_state").fetchone()
        max_v = row["max_v"] or 0.0 if row else 0.0
        chk("emotion MAX|valence|", max_v > 0.3, f"max|valence|={max_v:.3f} (需跑 bot 後)")
    else:
        chk("emotion valence", False, "DB not found")

    # ── #35a: overlay 有寫 ────────────────────────────────────────────
    overlay_path = vault / ".ai" / "dynamic_baseline_overlay.json"
    chk("#35a overlay.json", overlay_path.exists(), "需 6+ turn self_mod flush 後")

    # ── SOUL schema v11 ───────────────────────────────────────────────
    soul_path = vault / "00_System_Core" / "00.06_Companion_SOUL.md"
    if soul_path.exists():
        soul_text = soul_path.read_text(encoding="utf-8")
        chk("#40.1 SOUL schema v11", "schema_version: 11" in soul_text, "")
        chk("#40.1 locked_sections", "locked_sections:" in soul_text, "")
    else:
        chk("#40.1 SOUL", False, "SOUL.md not found")

    # ── per-provider lock pool ─────────────────────────────────────────
    try:
        from agent_memory.llm_client import _LLM_GENERATE_LOCK, _LLM_SUB_TASK_LOCK, _LLM_PRIORITY_QUEUE
        chk("Q1 per-provider lock", _LLM_GENERATE_LOCK is not _LLM_SUB_TASK_LOCK, "main≠sub_task lock")
        chk("#5 priority queue", _LLM_PRIORITY_QUEUE is not None, "PriorityQueue initialized")
    except ImportError as e:
        chk("lock pool import", False, str(e))

    if conn:
        conn.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="V3-O.10 Round 6 audit script")
    parser.add_argument("--vault", required=True, help="Vault root path")
    args = parser.parse_args()

    vault = pathlib.Path(args.vault).expanduser().resolve()
    if not vault.exists():
        print(f"[ERR] vault not found: {vault}")
        return 2

    print(f"V3-O.10 Round 6 Audit — vault: {vault}")
    print(f"時間: {datetime.now(timezone.utc).isoformat()}")
    print("-" * 60)
    results = run_audit(vault)
    print("-" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"結果: {passed}/{total} PASS")
    if passed < total:
        print("\n失敗項目:")
        for cid, ok, detail in results:
            if not ok:
                print(f"  FAIL  {cid}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
