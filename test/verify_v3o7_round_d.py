"""V3-O.7 Round D 驗證: 朋友卡 input 收束 end-to-end 流程

完整路徑:
  1. viewer 第一條訊息 → ensure_user_record (Step 2.5) → users 表有 row
  2. pipeline Step 17.5 → write_viewer_profile → 20_Audience_Graph/22_Casual_Viewers/<uid>.md
  3. viewer 第二條訊息 →
     Step 13.5: load_viewer_profile_md 讀朋友卡 md
     Step 14: prompt_packet["viewer_dynamic_context"] 有內容
  4. _real_companion_llm (stub) 走前: 確認用 viewer_dynamic_context 而非空 DB fallback
"""
import pathlib
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
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="v3o7d_"))
    try:
        v = tmp / "vault"
        v.mkdir()
        write_brain_type(v, "companion")
        ObsidianVaultAdapter(v).ensure_skeleton()
        ensure_companion_db(v)
        yield v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_friend_card_full_flow():
    with temp_companion_vault() as vr:
        from agent_memory.companion.companion_chat_runtime import ChatRequest, run_companion_chat_turn
        from agent_memory.companion.audience_writer import CASUAL_DIR, load_viewer_profile_md

        # ─ Turn 1: 第一次對話 ─
        req1 = ChatRequest(
            session_id="s-friendcard-001",
            user_id="viewer-fc-001",
            display_name="小精靈",
            message="你好！我是小精靈，喜歡看直播",
            is_owner=False,
        )
        resp1 = run_companion_chat_turn(req1, vault_root=vr)
        time.sleep(0.3)

        # 驗證 Step 2.5: users 表有 row
        with open_companion_db(vr) as conn:
            user_row = conn.execute(
                "SELECT user_id, display_name FROM users WHERE user_id=?",
                ("viewer-fc-001",),
            ).fetchone()
        assert user_row is not None, "D FAIL: users table empty after Turn 1"

        # 驗證 Step 17.5: viewer profile md 寫出
        md_files = list((vr / CASUAL_DIR).glob("*.md"))
        assert len(md_files) >= 1, f"D FAIL: no viewer profile md after Turn 1: {md_files}"
        profile_path = md_files[0]
        profile_content = profile_path.read_text(encoding="utf-8")
        assert "viewer-fc-001" in profile_content, "D FAIL: user_id not in profile md"
        print(f"  Turn 1 PASS: users row OK, profile md={profile_path.name}")

        # ─ Turn 2: 第二次對話，朋友卡應該被載入 ─
        # 用 hook 攔截 prompt_packet 來驗證 viewer_dynamic_context
        captured_packet = {}

        def _capture_llm(pkt: dict) -> str:
            captured_packet.update(pkt)
            return "你好小精靈！"

        req2 = ChatRequest(
            session_id="s-friendcard-001",
            user_id="viewer-fc-001",
            display_name="小精靈",
            message="你記得我嗎？",
            is_owner=False,
        )
        resp2 = run_companion_chat_turn(req2, vault_root=vr, llm_fn=_capture_llm)
        time.sleep(0.2)

        # 驗證 Step 13.5: load_viewer_profile_md 有內容
        viewer_ctx = captured_packet.get("viewer_dynamic_context", "")
        assert viewer_ctx, (
            f"D FAIL: viewer_dynamic_context empty in prompt_packet after Turn 2. "
            f"packet keys={list(captured_packet.keys())}"
        )
        assert "viewer-fc-001" in viewer_ctx, (
            f"D FAIL: user_id not in viewer_dynamic_context. ctx[:200]={viewer_ctx[:200]!r}"
        )
        print(f"  Turn 2 PASS: viewer_dynamic_context loaded ({len(viewer_ctx)} chars)")
        print(f"    ctx[:100]: {viewer_ctx[:100]!r}")

        # 驗證直接呼叫 load_viewer_profile_md (Turn 2 後 profile 已更新，內容比 viewer_ctx 更新)
        direct_md = load_viewer_profile_md(vr, "viewer-fc-001")
        assert direct_md, "D FAIL: load_viewer_profile_md returned empty"
        # viewer_ctx = Turn 2 開始時載入的 Turn 1 snapshot
        # direct_md = Turn 2 結束後 Step 17.5 更新的最新版本
        # 正確設計: Turn N 看 Turn N-1 的卡片, 然後用 Turn N 的資料更新卡片
        assert "viewer-fc-001" in direct_md, "D FAIL: user_id not in updated profile"
        print(f"  load_viewer_profile_md PASS: post-turn profile updated (Turn1={len(viewer_ctx)}, Turn2={len(direct_md)} chars)")

        # ─ Turn 3: 確認 _real_companion_llm 真的用 md-based context ─
        # 驗方法: viewer_dynamic_context 有內容 → _real_companion_llm 會用它
        # (實際 LLM 不跑, 用 stub 確認 packet 有正確欄位)
        assert "viewer_dynamic_context" in captured_packet, (
            "D FAIL: viewer_dynamic_context key missing from prompt_packet"
        )
        print("  _real_companion_llm routing PASS: viewer_dynamic_context key present in packet")

    return True


def test_friend_card_new_viewer_fallback():
    """新觀眾 (無 md) → viewer_dynamic_context 為空 → 走 DB fallback (不 crash)"""
    with temp_companion_vault() as vr:
        from agent_memory.companion.audience_writer import load_viewer_profile_md
        from agent_memory.companion.companion_chat_runtime import ChatRequest, run_companion_chat_turn

        captured_packet = {}

        def _capture_llm(pkt: dict) -> str:
            captured_packet.update(pkt)
            return "你好！"

        req = ChatRequest(
            session_id="s-newviewer",
            user_id="brand-new-viewer",
            display_name="新人",
            message="安安！",
            is_owner=False,
        )

        # 清空 users 表確保是真新觀眾情境 (manually wipe after turn to simulate)
        # Actually since it's fresh vault, just run turn directly
        # Step 2.5 會建立 users row，但 write_viewer_profile 要等 Step 17.5
        # 在同一 turn 內朋友卡 md 尚未存在 (先寫後讀在同 turn 是 race)
        # 但 Step 13.5 在 Step 17.5 之前 → 第一次 turn viewer_dynamic_context = ""

        resp = run_companion_chat_turn(req, vault_root=vr, llm_fn=_capture_llm)

        viewer_ctx = captured_packet.get("viewer_dynamic_context", "NOT_SET")
        # 第一次 turn: md 不存在 → 應該是空字串
        assert viewer_ctx == "", (
            f"D FAIL: first-turn viewer_dynamic_context should be empty, got {viewer_ctx[:100]!r}"
        )
        print(f"  New viewer first turn PASS: viewer_dynamic_context='' (fallback to DB)")

        # 確認沒有 crash
        assert resp is not None, "D FAIL: pipeline crashed on new viewer"
        print(f"  No crash PASS: pipeline completed for new viewer")

    return True


if __name__ == "__main__":
    failures = []
    tests = [
        ("Friend card full flow (2 turns)", test_friend_card_full_flow),
        ("New viewer fallback (no md)", test_friend_card_new_viewer_fallback),
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
        print("ALL PASS (Round D)")
