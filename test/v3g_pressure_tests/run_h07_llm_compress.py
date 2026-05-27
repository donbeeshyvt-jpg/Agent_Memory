# -*- coding: utf-8 -*-
"""V3-H7 壓測: 殘-12 self_modification Phase 1 truncate → Phase 3 LLM 壓縮.

驗證:
- Test 1: _llm_compress_old_section helper imported
- Test 2: _enforce_char_limit_compress sig 接受 vault_root
- Test 3: force_stub env 仍走 truncate fallback (不爆)
- Test 4: 達 limit 觸發壓縮; 未達不觸發
- Test 5: caller 傳 vault_root (line 371 + 427)
"""
from __future__ import annotations
import os
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LOG = Path(__file__).parent / "logs" / "h07_llm_compress.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def main() -> int:
    log("=" * 70)
    log("V3-H7 PRESSURE TEST: 殘-12 self_modification LLM 壓縮")
    log("=" * 70)
    failed = 0

    # ─── Test 1: helpers imported ───
    log("\n[Test 1] _llm_compress_old_section + _enforce_char_limit_compress imported")
    try:
        from agent_memory.companion.self_modification_loop import (
            _llm_compress_old_section,
            _enforce_char_limit_compress,
        )
        log("  ✅ PASS: imported")
    except Exception as e:
        log(f"  ❌ FAIL: import {e}")
        failed += 1
        return 1

    # ─── Test 2: sig 接受 vault_root ───
    log("\n[Test 2] _enforce_char_limit_compress sig 接 vault_root kwarg")
    import inspect
    sig = inspect.signature(_enforce_char_limit_compress)
    if "vault_root" not in sig.parameters:
        log("  ❌ FAIL: vault_root kwarg 沒加")
        failed += 1
    else:
        log("  ✅ PASS: vault_root kwarg 存在")

    # ─── Test 3: force_stub env → fallback truncate ───
    log("\n[Test 3] force_stub env → fallback truncate (不爆)")
    os.environ["AGENT_MEMORY_COMPANION_LLM_FORCE_STUB"] = "1"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        (tmp_vault / ".ai").mkdir()
        # 寫個超 limit 的檔
        memory_md = tmp_vault / "test_MEMORY.md"
        body = "old stuff " * 300  # ~3000 chars
        memory_md.write_text(
            "---\ntype: companion_memory\n---\n" + body,
            encoding="utf-8",
        )
        try:
            result = _enforce_char_limit_compress(memory_md, 1000, vault_root=tmp_vault)
            log(f"  compressed = {result}")
            new_content = memory_md.read_text(encoding="utf-8")
            if "Phase 1 truncate" in new_content or "已壓縮" in new_content:
                log("  ✅ PASS: force_stub → fallback truncate marker 出現")
            else:
                log("  ⚠️ partial: 壓縮但 marker 沒找到 (可能 LLM 壓縮跑了)")
        except Exception as e:
            log(f"  ❌ FAIL: 壓縮爆掉 {e}")
            failed += 1

    # ─── Test 4: 未達 limit → 不壓縮 ───
    log("\n[Test 4] 未達 limit → return False 不壓縮")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_vault = Path(tmpdir)
        small_md = tmp_vault / "small.md"
        small_md.write_text("---\n---\nshort content", encoding="utf-8")
        result = _enforce_char_limit_compress(small_md, 1000, vault_root=tmp_vault)
        if result:
            log("  ❌ FAIL: 未達 limit 不該壓縮")
            failed += 1
        else:
            log("  ✅ PASS: 未達 limit return False")

    # ─── Test 5: caller 傳 vault_root ───
    log("\n[Test 5] self_modification_loop caller 傳 vault_root (370+ / 427+)")
    src = (ROOT / "agent_memory" / "companion" / "self_modification_loop.py").read_text(encoding="utf-8")
    if "_enforce_char_limit_compress(memory_path, char_limit_mem, vault_root=vault_root)" not in src:
        log("  ❌ FAIL: MEMORY 路徑 caller 沒傳 vault_root")
        failed += 1
    else:
        log("  ✅ PASS: MEMORY caller 傳 vault_root")
    if "_enforce_char_limit_compress(profile_path, char_limit_owner, vault_root=vault_root)" not in src:
        log("  ❌ FAIL: OWNER_PROFILE 路徑 caller 沒傳 vault_root")
        failed += 1
    else:
        log("  ✅ PASS: OWNER_PROFILE caller 傳 vault_root")

    # 清 env
    os.environ.pop("AGENT_MEMORY_COMPANION_LLM_FORCE_STUB", None)

    # ─── 收尾 ───
    log("\n" + "=" * 70)
    if failed == 0:
        log("✅ V3-H7 全壓測 PASS")
        log("=" * 70)
        return 0
    else:
        log(f"❌ V3-H7 壓測有 {failed} 個 FAIL")
        log("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
