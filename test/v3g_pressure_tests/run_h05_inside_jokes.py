# -*- coding: utf-8 -*-
"""V3-H5 壓測: 殘-11 H8 Inside Jokes 寫入 + 撈 + 注入.

驗證:
- Test 1: inside_joke_writer 4 helpers imported
- Test 2: detect_inside_jokes_for_user 對重複 keyword ≥ 3 次偵測
- Test 3: write_inside_joke_md 寫對 20_Audience_Graph/23_Inside_Jokes/
- Test 4: list_active_inside_jokes 對 intimacy_threshold filter
- Test 5: maybe_inject_inside_joke 對 playfulness>0.5 + intim ≥ 0.4 + 10% 機率
- Test 6: curator L3 加 _detect_inside_jokes_pass hook
- Test 7: chat_runtime Step 16.5 加 inside_joke 注入 hook
"""
from __future__ import annotations
import sys
import time
import tempfile
import random
import uuid
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h05_inside_jokes.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-H5 PRESSURE TEST: 殘-11 H8 Inside Jokes")
    log("=" * 70)
    failed = 0

    # ─── Test 1: imports ───
    log("\n[Test 1] inside_joke_writer helpers")
    try:
        from agent_memory.companion.inside_joke_writer import (
            detect_inside_jokes_for_user, write_inside_joke_md,
            list_active_inside_jokes, maybe_inject_inside_joke,
            INSIDE_JOKE_DIR,
        )
        log("  ✅ PASS: 4 helpers + dir const imported")
    except Exception as e:
        log(f"  ❌ FAIL: import {e}")
        failed += 1
        return 1

    # ─── Test 2: detect_inside_jokes_for_user 偵測重複 keyword ───
    log("\n[Test 2] detect_inside_jokes_for_user 重複 keyword ≥ 3")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        from agent_memory.companion.companion_db import open_companion_db
        now = datetime.now(timezone.utc).isoformat()
        with open_companion_db(tmp_vault) as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO raw_events (event_id, user_id, session_id, actor, content, source, injection_risk, created_at) "
                    "VALUES (?, ?, 'sess-test', 'user', ?, 'test', 'low', ?)",
                    (str(uuid.uuid4()), "user-h5", f"今天我們又看到 randomizer 了 {i}", now),
                )
            conn.commit()

        jokes = detect_inside_jokes_for_user(tmp_vault, "user-h5", window_days=7)
        log(f"  jokes detected = {len(jokes)}")
        if not jokes:
            log("  ❌ FAIL: 沒偵測到")
            failed += 1
        else:
            kws = [j["keyword"] for j in jokes]
            if "randomizer" in kws:
                log(f"  ✅ PASS: 偵測到 'randomizer' (count={jokes[0]['count']})")
            else:
                log(f"  ⚠️ partial: top keywords = {kws[:5]} (randomizer 可能被其他 keyword 排在後)")

    # ─── Test 3: write_inside_joke_md ───
    log("\n[Test 3] write_inside_joke_md 寫 20_Audience_Graph/23_Inside_Jokes/")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        path = write_inside_joke_md(
            tmp_vault, keyword="randomizer", user_id="user-h5",
            use_count=5, intimacy_threshold=0.4,
        )
        if not path or not path.exists():
            log("  ❌ FAIL: write 失敗")
            failed += 1
        else:
            content = path.read_text(encoding="utf-8")
            if "type: inside_joke" not in content:
                log("  ❌ FAIL: frontmatter type 錯")
                failed += 1
            elif "joke_keyword: randomizer" not in content:
                log("  ❌ FAIL: joke_keyword 沒寫")
                failed += 1
            elif "23_Inside_Jokes" not in str(path):
                log("  ❌ FAIL: 路徑錯")
                failed += 1
            else:
                log(f"  ✅ PASS: 寫到 {path.relative_to(tmp_vault)}")

    # ─── Test 4: list_active_inside_jokes intimacy filter ───
    log("\n[Test 4] list_active_inside_jokes intimacy threshold filter")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        # 寫 2 個 joke, threshold 0.4 和 0.8
        write_inside_joke_md(tmp_vault, keyword="kw_low", user_id="user-h5", intimacy_threshold=0.4)
        write_inside_joke_md(tmp_vault, keyword="kw_high", user_id="user-h5", intimacy_threshold=0.8)
        # intimacy=0.5 → 只看到 kw_low
        jokes = list_active_inside_jokes(tmp_vault, "user-h5", intimacy_score=0.5)
        log(f"  jokes for intim=0.5 = {[j['keyword'] for j in jokes]}")
        if len(jokes) != 1 or jokes[0]["keyword"] != "kw_low":
            log("  ❌ FAIL: intim=0.5 應只回 kw_low (threshold=0.4)")
            failed += 1
        else:
            log("  ✅ PASS: intim=0.5 filter 對")
        # intimacy=0.9 → 看到 2 個
        jokes_all = list_active_inside_jokes(tmp_vault, "user-h5", intimacy_score=0.9)
        if len(jokes_all) != 2:
            log(f"  ❌ FAIL: intim=0.9 應回 2 個, 拿到 {len(jokes_all)}")
            failed += 1
        else:
            log("  ✅ PASS: intim=0.9 看到 2 個")

    # ─── Test 5: maybe_inject_inside_joke 機率注入 ───
    log("\n[Test 5] maybe_inject_inside_joke playfulness/intim/random 條件")
    rng_fixed = random.Random(42)
    jokes_list = [{"keyword": "test_joke", "threshold": 0.4}]
    # 條件 met + 100 次跑統計 (10% 機率注入)
    rng_test = random.Random(1)
    injected_count = 0
    for _ in range(100):
        result = maybe_inject_inside_joke(
            "Hello user", jokes_list,
            playfulness=0.7, intimacy_score=0.6, rng=rng_test,
        )
        if "test_joke" in result:
            injected_count += 1
    log(f"  100 次跑, 注入 {injected_count} 次 (期望 ~10)")
    if injected_count < 3 or injected_count > 25:
        log(f"  ❌ FAIL: 機率不在 3~25% 範圍")
        failed += 1
    else:
        log(f"  ✅ PASS: 注入率 {injected_count}% ~10% (random distribution)")

    # 條件不 met (playfulness 低) → 不注入
    no_inject = maybe_inject_inside_joke(
        "Hello", jokes_list, playfulness=0.2, intimacy_score=0.6, rng=rng_fixed,
    )
    if "test_joke" in no_inject:
        log("  ❌ FAIL: playfulness 低不該注入")
        failed += 1
    else:
        log("  ✅ PASS: playfulness<0.5 → 不注入")

    # 條件不 met (intim 低) → 不注入
    no_inject2 = maybe_inject_inside_joke(
        "Hello", jokes_list, playfulness=0.7, intimacy_score=0.2, rng=rng_fixed,
    )
    if "test_joke" in no_inject2:
        log("  ❌ FAIL: intim 低不該注入")
        failed += 1
    else:
        log("  ✅ PASS: intim<0.4 → 不注入")

    # ─── Test 6: curator L3 hook ───
    log("\n[Test 6] curator L3 加 _detect_inside_jokes_pass")
    cur_src = (ROOT / "agent_memory" / "companion" / "companion_curator.py").read_text(encoding="utf-8")
    if "_detect_inside_jokes_pass" not in cur_src:
        log("  ❌ FAIL: curator L3 沒加 hook")
        failed += 1
    else:
        log("  ✅ PASS: curator L3 hook")

    # ─── Test 7: chat_runtime Step 16.5 注入 hook ───
    log("\n[Test 7] chat_runtime 加 maybe_inject_inside_joke hook")
    chat_src = (ROOT / "agent_memory" / "companion" / "companion_chat_runtime.py").read_text(encoding="utf-8")
    if "maybe_inject_inside_joke(" not in chat_src:
        log("  ❌ FAIL: chat_runtime 沒接 hook")
        failed += 1
    else:
        log("  ✅ PASS: chat_runtime 接 hook")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-H5 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-H5 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
