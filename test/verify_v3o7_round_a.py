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


# ─── RC2: _make_viewer_slug CJK 不碰撞 ───
def test_rc2_slug_no_collision():
    # 直接測試 slug 邏輯
    def _make_viewer_slug(name: str) -> str:
        ascii_part = re.sub(r"[^A-Za-z0-9._-]", "", name)[:12]
        hash_suffix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
        return f"{ascii_part}-{hash_suffix}" if ascii_part else f"v-{hash_suffix}"

    slug_a = _make_viewer_slug("小米奇")
    slug_b = _make_viewer_slug("小精靈")
    slug_alice = _make_viewer_slug("Alice")
    slug_a2 = _make_viewer_slug("小米奇")  # same as slug_a

    assert slug_a != slug_b, f"RC2 FAIL: 小米奇 == 小精靈 ({slug_a})"
    assert slug_a == slug_a2, f"RC2 FAIL: same name gives different slugs"
    assert len(slug_a) <= 25, f"RC2: slug too long: {slug_a!r}"
    # ASCII part should be preserved for ASCII names
    assert slug_alice.startswith("Alice-"), f"RC2 FAIL: Alice slug missing ASCII part: {slug_alice!r}"
    # Pure CJK → starts with "v-"
    assert slug_a.startswith("v-"), f"RC2 FAIL: pure CJK should start with v-: {slug_a!r}"

    # relay 模組真的用了 _make_viewer_slug
    relay_path = pathlib.Path(__file__).parent.parent / "scripts" / "discord_bridge_relay.py"
    src = relay_path.read_text(encoding="utf-8")
    assert "_make_viewer_slug" in src, "RC2 FAIL: _make_viewer_slug not in relay"
    assert "ai-viewer-{_make_viewer_slug" in src, "RC2 FAIL: relay not using _make_viewer_slug"
    # hashlib import in relay
    assert "import hashlib" in src, "RC2 FAIL: hashlib not imported in relay"

    print(f"  RC2 PASS: 小米奇={slug_a!r} 小精靈={slug_b!r} Alice={slug_alice!r}")
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
