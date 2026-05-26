# -*- coding: utf-8 -*-
"""V3-G4 壓測: 40_Knowledge_Base 重設計 (廢 41-44 → 日常 + 外部 兩入口).

對齊 user 2026-05-27 拍板「不要分 41/42/43/44, 改成 日常(對話累積) + 外部(中之人/hermes 抓)」
對齊 MISSION §3.6 文獻吸收致用 + V3 §13 Memory Router L4.

驗證:
- Test 1: knowledge_base.py 模組存在 + 4 個 helpers
- Test 2: write_daily_knowledge 寫對 41_Daily/<topic>.md (frontmatter + 內容)
- Test 3: write_external_knowledge 寫對 42_External/<topic>.md
- Test 4: list_ingest_inbox 列 _ingest_inbox/ 內 .md/.txt
- Test 5: retrieve_knowledge fallback substring search 撈到
- Test 6: chat_runtime 加 Step 11.85 retrieve hook + prompt_packet knowledge_hits
- Test 7: _build_companion_system_prompt 對 knowledge_hits 加 section F4
- Test 8: vault skeleton (obsidian.py) 新 dir 對齊
- Test 9: folder_labels 對應新 dir
"""
from __future__ import annotations
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "g04_knowledge_base_redesign.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-G4 PRESSURE TEST: 40_Knowledge_Base 重設計 (日常+外部 兩入口)")
    log("=" * 70)
    failed = 0

    # ─── Test 1: imports ───
    log("\n[Test 1] knowledge_base.py 4 個 helpers")
    try:
        from agent_memory.companion.knowledge_base import (
            write_daily_knowledge, write_external_knowledge,
            list_ingest_inbox, retrieve_knowledge,
            DAILY_DIR, EXTERNAL_DIR, INGEST_INBOX_DIR,
        )
        log("  ✅ PASS: 4 helpers + 3 dir consts imported")
        log(f"  DAILY_DIR = {DAILY_DIR}")
        log(f"  EXTERNAL_DIR = {EXTERNAL_DIR}")
        log(f"  INGEST_INBOX_DIR = {INGEST_INBOX_DIR}")
    except Exception as e:
        log(f"  ❌ FAIL: import 錯 {e}")
        failed += 1
        return 1

    # ─── 用 tmp vault 做 isolated test ───
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)

        # ─── Test 2: write_daily_knowledge ───
        log("\n[Test 2] write_daily_knowledge 寫 41_Daily/")
        path = write_daily_knowledge(
            tmp_vault,
            topic="randomizer mod 解釋",
            claim="randomizer mod 是隨機化關鍵物品的 mod, viewer-A 玩過",
            source_event_ids=["evt-1", "evt-2"],
            confidence=0.7,
            tags=["遊戲", "mod"],
        )
        if not path:
            log("  ❌ FAIL: write_daily_knowledge 回 None")
            failed += 1
        elif not path.exists():
            log(f"  ❌ FAIL: 檔不存在 {path}")
            failed += 1
        else:
            content = path.read_text(encoding="utf-8")
            log(f"  寫入: {path.relative_to(tmp_vault)}")
            log(f"  size: {len(content)} char")
            checks = [
                ("type: daily_knowledge", "frontmatter type"),
                ("knowledge_source: daily_conversation", "knowledge_source"),
                ("lifecycle_state: mid", "lifecycle_state mid"),
                ("randomizer mod 是隨機化", "claim 內容"),
                ("evt-1", "source_event_ids"),
            ]
            for kw, name in checks:
                if kw not in content:
                    log(f"  ❌ FAIL: {name} 沒寫對")
                    failed += 1
                else:
                    log(f"  ✅ {name}")

        # ─── Test 3: write_external_knowledge ───
        log("\n[Test 3] write_external_knowledge 寫 42_External/")
        path2 = write_external_knowledge(
            tmp_vault,
            topic="VTuber 設定: 史萊姆 角色背景",
            content="這是中之人投餵的角色設定文件, 包含角色生平 + 喜好 + 紅線.",
            summary="史萊姆角色, 水做的, 喜歡撒嬌",
            source_path=Path("user_drag/setting.md"),
            tags=["lore", "viewer-friendly"],
        )
        if not path2 or not path2.exists():
            log("  ❌ FAIL: write_external_knowledge")
            failed += 1
        else:
            content2 = path2.read_text(encoding="utf-8")
            log(f"  寫入: {path2.relative_to(tmp_vault)}")
            if "type: external_knowledge" not in content2:
                log("  ❌ FAIL: external_knowledge type")
                failed += 1
            elif "lifecycle_state: long" not in content2:
                log("  ❌ FAIL: lifecycle_state long")
                failed += 1
            elif "## 摘要" not in content2:
                log("  ❌ FAIL: 摘要 section")
                failed += 1
            else:
                log("  ✅ PASS: external_knowledge frontmatter + 摘要對")

        # ─── Test 4: list_ingest_inbox ───
        log("\n[Test 4] list_ingest_inbox 偵測 _ingest_inbox/")
        inbox = tmp_vault / INGEST_INBOX_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        # 寫 3 個檔
        (inbox / "research_001.md").write_text("# 論文摘要", encoding="utf-8")
        (inbox / "game_strategy.md").write_text("# 攻略", encoding="utf-8")
        (inbox / "notes.txt").write_text("筆記", encoding="utf-8")
        files = list_ingest_inbox(tmp_vault)
        log(f"  inbox 內 {len(files)} 檔: {[f.name for f in files]}")
        if len(files) < 3:
            log(f"  ❌ FAIL: 應 3 檔, 拿到 {len(files)}")
            failed += 1
        else:
            log("  ✅ PASS: 偵測 3 檔")

        # ─── Test 5: retrieve_knowledge fallback substring ───
        log("\n[Test 5] retrieve_knowledge fallback substring search")
        hits = retrieve_knowledge(tmp_vault, "randomizer", top_k=3)
        log(f"  query='randomizer' → hits={len(hits)}")
        if not hits:
            log("  ❌ FAIL: 沒撈到 (寫 daily 應該撈得到)")
            failed += 1
        elif "randomizer" not in hits[0].get("path", "").lower() and "randomizer" not in hits[0].get("summary", "").lower():
            log(f"  ❌ FAIL: hit content 沒 randomizer: {hits[0]}")
            failed += 1
        else:
            log(f"  ✅ PASS: hit 0 = {hits[0].get('path', '')[-40:]} ({hits[0].get('source', '')})")

    # ─── Test 6: chat_runtime Step 11.85 + prompt_packet ───
    log("\n[Test 6] chat_runtime 加 Step 11.85 retrieve hook")
    src = (ROOT / "agent_memory" / "companion" / "companion_chat_runtime.py").read_text(encoding="utf-8")
    if "Step 11.85" not in src:
        log("  ❌ FAIL: Step 11.85 沒加進 chat_runtime")
        failed += 1
    else:
        log("  ✅ PASS: Step 11.85 存在")
    if "retrieve_knowledge(vault_root, request.message" not in src:
        log("  ❌ FAIL: retrieve_knowledge call 沒接")
        failed += 1
    else:
        log("  ✅ PASS: retrieve_knowledge call 存在")
    if '"knowledge_hits"' not in src:
        log("  ❌ FAIL: prompt_packet knowledge_hits 沒加")
        failed += 1
    else:
        log("  ✅ PASS: prompt_packet knowledge_hits 存在")

    # ─── Test 7: _build_companion_system_prompt section F4 ───
    log("\n[Test 7] _build_companion_system_prompt section F4")
    from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt
    packet = {
        "affect": {"valence": 0.0, "arousal": 0.3, "dominance": 0.5, "uncertainty": 0.3},
        "emotion": {"dominant_emotion": "joy", "joy": 0.5},
        "balance": {"balance_axis": 0.0},
        "policy": {"strategy": "calm", "tone": "calm", "intimacy_score": 0.5, "is_owner": True},
        "decision": "ALLOW",
        "memory_context": "L1 recent",
        "system_persona": "test",
        "knowledge_hits": [
            {"path": "40_Knowledge_Base/41_Daily_Knowledge/randomizer.md", "summary": "隨機化關鍵物品 mod", "score": 0.8, "source": "daily"},
            {"path": "40_Knowledge_Base/42_External_Knowledge/lore.md", "summary": "史萊姆角色背景", "score": 0.7, "source": "external"},
        ],
    }
    prompt = _build_companion_system_prompt(packet, vault_root=None)
    if "[F4. 知識庫" not in prompt:
        log("  ❌ FAIL: section F4 沒出現")
        failed += 1
    else:
        log("  ✅ PASS: section F4 出現")
    if "日常累積" not in prompt:
        log("  ❌ FAIL: source label '日常累積' 沒出現")
        failed += 1
    else:
        log("  ✅ PASS: source label 對")
    if "外部文獻" not in prompt:
        log("  ❌ FAIL: source label '外部文獻' 沒出現")
        failed += 1
    else:
        log("  ✅ PASS: source label '外部文獻' 對")

    # ─── Test 8: vault skeleton (obsidian.py) ───
    log("\n[Test 8] obsidian.py vault skeleton 對齊新結構")
    obsidian_src = (ROOT / "agent_memory" / "vault" / "obsidian.py").read_text(encoding="utf-8")
    if "41_Daily_Knowledge" not in obsidian_src:
        log("  ❌ FAIL: 41_Daily_Knowledge 沒加進 obsidian.py")
        failed += 1
    else:
        log("  ✅ PASS: 41_Daily_Knowledge ✓")
    if "42_External_Knowledge" not in obsidian_src:
        log("  ❌ FAIL: 42_External_Knowledge 沒加")
        failed += 1
    else:
        log("  ✅ PASS: 42_External_Knowledge ✓")
    if "_ingest_inbox" not in obsidian_src:
        log("  ❌ FAIL: _ingest_inbox 沒加")
        failed += 1
    else:
        log("  ✅ PASS: _ingest_inbox ✓")

    # ─── Test 9: folder_labels 對齊 ───
    log("\n[Test 9] folder_labels.py 對齊新結構")
    labels_src = (ROOT / "agent_memory" / "folder_labels.py").read_text(encoding="utf-8")
    if "41_Daily_Knowledge" not in labels_src:
        log("  ❌ FAIL: folder_labels 41_Daily_Knowledge 沒加")
        failed += 1
    else:
        log("  ✅ PASS: folder_labels 對齊")

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-G4 全壓測 PASS (~20 個 check)")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-G4 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
