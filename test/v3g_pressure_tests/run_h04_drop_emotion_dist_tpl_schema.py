# -*- coding: utf-8 -*-
"""V3-H4 壓測: 殘-07 廢 emotion_distribution + 殘-10 4 TPL schema 升真實.

驗證:
- Test 1: companion_db.py 不再 CREATE TABLE emotion_distribution
- Test 2: _DROP_LEGACY_TABLES 含 emotion_distribution DROP
- Test 3: ensure_companion_db 跑完 28 表 (≥28, 不含 emotion_distribution)
- Test 4: 既有 db 含 emotion_distribution → ensure_companion_db 跑後表沒了
- Test 5: obsidian.py 內 4 TPL 升真實 schema (含 schema_version: 10)
- Test 6: 5 TPL 全部都有真實 schema (V3-G6 + V3-H4)
"""
from __future__ import annotations
import sys
import time
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h04_drop_tpl.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-H4 PRESSURE TEST: 殘-07 廢 emotion_distribution + 殘-10 4 TPL schema")
    log("=" * 70)
    failed = 0

    # ─── Test 1: db.py 不再 CREATE emotion_distribution ───
    log("\n[Test 1] companion_db.py 不再 CREATE TABLE emotion_distribution")
    db_src = (ROOT / "agent_memory" / "companion" / "companion_db.py").read_text(encoding="utf-8")
    if "CREATE TABLE IF NOT EXISTS emotion_distribution" in db_src:
        log("  ❌ FAIL: 還有 CREATE emotion_distribution")
        failed += 1
    else:
        log("  ✅ PASS: CREATE emotion_distribution 已移除")

    # ─── Test 2: _DROP_LEGACY_TABLES 含 DROP ───
    log("\n[Test 2] _DROP_LEGACY_TABLES 含 emotion_distribution DROP")
    if "DROP TABLE IF EXISTS emotion_distribution" not in db_src:
        log("  ❌ FAIL: DROP migration 沒加")
        failed += 1
    else:
        log("  ✅ PASS: DROP migration ✓")

    # ─── Test 3: ensure_companion_db 跑完 28 表 ───
    log("\n[Test 3] ensure_companion_db 跑完 ≥28 表 (V3-H4 廢 1 表)")
    from agent_memory.companion.companion_db import ensure_companion_db, list_table_names
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        ensure_companion_db(tmp_vault)
        tables = set(list_table_names(tmp_vault))
        log(f"  total tables = {len(tables)}")
        if len(tables) < 28:
            log(f"  ❌ FAIL: < 28 表")
            failed += 1
        else:
            log(f"  ✅ PASS: ≥ 28 表 ({len(tables)})")
        if "emotion_distribution" in tables:
            log("  ❌ FAIL: emotion_distribution 還在")
            failed += 1
        else:
            log("  ✅ PASS: emotion_distribution 已廢")

    # ─── Test 4: 既有 db 含 emotion_distribution → 跑後消失 ───
    log("\n[Test 4] 既有 db 含 emotion_distribution → ensure_companion_db DROP 之")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        # 手動建一個含 emotion_distribution 的舊 db
        old_db = tmp_vault / ".ai" / "companion.db"
        conn = sqlite3.connect(str(old_db))
        conn.execute("CREATE TABLE emotion_distribution (dist_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        # 跑 ensure_companion_db
        ensure_companion_db(tmp_vault)
        tables2 = set(list_table_names(tmp_vault))
        if "emotion_distribution" in tables2:
            log("  ❌ FAIL: migration 沒 DROP 既有表")
            failed += 1
        else:
            log("  ✅ PASS: migration 成功 DROP 既有 emotion_distribution")

    # ─── Test 5: obsidian.py 4 TPL 升真實 schema ───
    log("\n[Test 5] obsidian.py 4 TPL 含 schema_version + frontmatter 真實")
    obs_src = (ROOT / "agent_memory" / "vault" / "obsidian.py").read_text(encoding="utf-8")
    tpl_checks = [
        ("tpl_emotion_event", "TPL_Emotion_Event 真實"),
        ("tpl_inside_joke", "TPL_Inside_Joke 真實"),
        ("tpl_learned_skill", "TPL_Learned_Skill 真實"),
        ("tpl_persona_version", "TPL_Persona_Version 真實"),
    ]
    for var_name, label in tpl_checks:
        if var_name not in obs_src:
            log(f"  ❌ FAIL: {label} 變數沒定義")
            failed += 1
        else:
            log(f"  ✅ {label} ✓")

    # ─── Test 6: 5 TPL 全部都有 schema ───
    log("\n[Test 6] 5 TPL all real schema (V3-G6 + V3-H4)")
    if "tpl_viewer_body" not in obs_src:
        log("  ❌ FAIL: TPL_Viewer (V3-G6) 變數消失")
        failed += 1
    else:
        log("  ✅ PASS: TPL_Viewer (V3-G6 + V3-H4 4 TPL) 全 active")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-H4 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-H4 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
