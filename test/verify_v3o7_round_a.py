"""V3-O.7 Round A 驗證測試: RC1 + RC2 + D1/D2"""
import hashlib
import pathlib
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from agent_memory.vault.obsidian import ObsidianVaultAdapter, write_brain_type
from agent_memory.companion.companion_db import ensure_companion_db, open_companion_db


@contextmanager
def temp_companion_vault():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="v3o7_"))
    try:
        v = tmp / "vault"
        v.mkdir()
        write_brain_type(v, "companion")
        ObsidianVaultAdapter(v).ensure_skeleton()
        ensure_companion_db(v)
        yield v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── RC1: ensure_user_record wired → users table populated → viewer profile md ───
def test_rc1_viewer_profile_written():
    with temp_companion_vault() as vr:
        from agent_memory.companion.companion_chat_runtime import ChatRequest, run_companion_chat_turn
        from agent_memory.companion.audience_writer import CASUAL_DIR

        req = ChatRequest(
            session_id="s-rc1",
            user_id="viewer-rc1-001",
            display_name="小花",
            message="哈囉",
            is_owner=False,
        )
        run_companion_chat_turn(req, vault_root=vr)
        time.sleep(0.2)

        # users 表有 row
        with open_companion_db(vr) as conn:
            row = conn.execute(
                "SELECT user_id, display_name FROM users WHERE user_id=?",
                ("viewer-rc1-001",),
            ).fetchone()

        assert row is not None, "RC1 FAIL: users table empty after pipeline"
        dn = row["display_name"]
        assert dn == "小花", f"RC1 FAIL: display_name wrong: {dn!r}"

        # viewer profile md 寫出
        md_files = list((vr / CASUAL_DIR).glob("*.md"))
        assert len(md_files) >= 1, f"RC1 FAIL: no md in 22_Casual_Viewers, got {md_files}"

        # md 內容包含 user_id
        content = md_files[0].read_text(encoding="utf-8")
        assert "viewer-rc1-001" in content, "RC1 FAIL: user_id not in md content"

        print(f"  RC1 PASS: users row=({row['user_id']}, {dn!r}), md={md_files[0].name}")
    return True


# ─── RC2 (V3-O.13.5 翻轉): AI viewer pool 模擬路徑已淨化, 驗證 relay 不再有 _make_viewer_slug ───
# 原 V3-O.7 RC2 測試 CJK slug 不碰撞 — 屬「single-bot 模擬多 viewer」hack 的支援 code.
# V3-O.13.5 user 拍板「測試/正式都不用」全淨化, 朋友卡只收真實 Discord 用戶真實互動.
# 本 test 翻轉成 regression assert: 確保 _make_viewer_slug / ai-viewer- prefix / hashlib import
# 在 relay 內已徹底移除, 將來若有誤加回去會被本 test 擋下.
def test_rc2_slug_no_collision():
    relay_path = pathlib.Path(__file__).parent.parent / "scripts" / "discord_bridge_relay.py"
    src = relay_path.read_text(encoding="utf-8")
    # 淨化 regression: 這 4 個 substring 都應該已不在 relay
    forbidden = [
        ("_make_viewer_slug", "RC2 (V3-O.13.5): _make_viewer_slug 應已淨化, 不該在 relay"),
        ("ai-viewer-", "RC2 (V3-O.13.5): ai-viewer-<slug> prefix 應已淨化, 不該在 relay"),
        ("allow_bot_author_ids = set", "RC2 (V3-O.13.5): allow_bot_author_ids state 應已淨化"),
        ("self.split_by_display_name", "RC2 (V3-O.13.5): split_by_display_name flag 應已淨化"),
    ]
    for needle, msg in forbidden:
        assert needle not in src, msg
    print("  RC2 PASS (V3-O.13.5 淨化 regression): AI viewer pool 模擬路徑徹底移除")
    return True


# ─── D1/D2: curator Layer3 呼叫 consolidate_preferences → 60_ md ───
def test_d1d2_preference_consolidation():
    with temp_companion_vault() as vr:
        # 插入 episodic preference (evidence>=3) 觸發升格
        with open_companion_db(vr) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO preference_memories "
                "(preference_id, user_id, preference_type, claim, strength, confidence, "
                "evidence_count, status, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                ("pref-0001", "owner-001", "food", "喜歡拉麵", 0.8, 0.6, 3, "episodic"),
            )
            conn.commit()

        from agent_memory.companion.companion_curator import run_layer3_24h_medium
        result = run_layer3_24h_medium(vr)

        assert any("preferences_consolidated" in a for a in result.actions_performed), (
            f"D1/D2 FAIL: preferences_consolidated not in actions: {result.actions_performed}"
        )

        # 60_ 資料夾有 md
        from agent_memory.companion.markdown_writers import OWNER_PREF_DIR, VIEWER_PREF_DIR
        owner_files = list((vr / OWNER_PREF_DIR).glob("*.md"))
        viewer_files = list((vr / VIEWER_PREF_DIR).glob("*.md"))
        assert len(owner_files) + len(viewer_files) >= 1, (
            f"D1/D2 FAIL: no md in 60_. owner={owner_files} viewer={viewer_files}"
        )

        print(f"  D1/D2 PASS: actions={result.actions_performed}")
        print(f"    pref md: owner={len(owner_files)} viewer={len(viewer_files)}")
    return True


if __name__ == "__main__":
    failures = []
    tests = [
        ("RC1 viewer profile written", test_rc1_viewer_profile_written),
        ("RC2 slug no collision", test_rc2_slug_no_collision),
        ("D1/D2 preference consolidation", test_d1d2_preference_consolidation),
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as e:
            import traceback
            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
            failures.append(name)

    print()
    if failures:
        print(f"FAILED: {failures}")
        sys.exit(1)
    else:
        print("ALL PASS (Round A)")
