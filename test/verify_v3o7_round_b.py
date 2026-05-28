"""V3-O.7 Round B 驗證: write_trait_evolution_md + write_memory_audit_md"""
import pathlib
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from agent_memory.vault.obsidian import ObsidianVaultAdapter, write_brain_type
from agent_memory.companion.companion_db import ensure_companion_db


@contextmanager
def temp_companion_vault():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="v3o7b_"))
    try:
        v = tmp / "vault"
        v.mkdir()
        write_brain_type(v, "companion")
        ObsidianVaultAdapter(v).ensure_skeleton()
        ensure_companion_db(v)
        yield v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Round B-1: write_trait_evolution_md ───
def test_write_trait_evolution_md():
    with temp_companion_vault() as vr:
        from agent_memory.companion.markdown_writers import write_trait_evolution_md, TRAIT_EVOLUTION_DIR

        # 第一次寫（建 frontmatter + 第一條 entry）
        path1 = write_trait_evolution_md(
            vr,
            trait_name="baseline_balance",
            old_value=0.30,
            new_value=0.35,
            delta=0.05,
            evidence_count=5,
            trigger="drift_guard_confirm",
            user_id="viewer-001",
            evolution_id="evo-aaa",
        )
        assert path1 is not None, "Round B FAIL: write_trait_evolution_md returned None"
        assert path1.exists(), f"Round B FAIL: trait md not found at {path1}"

        content1 = path1.read_text(encoding="utf-8")
        assert "type: trait_evolution" in content1, "Round B FAIL: frontmatter missing"
        assert "baseline_balance" in content1
        assert "0.3500" in content1
        assert "+0.0500" in content1
        assert "evo-aaa" in content1

        # 路徑應在 30_Emotional_State/33_Trait_Evolution/
        # Windows: str(path) uses backslash, so normalise for comparison
        assert "33_Trait_Evolution" in str(path1), f"Round B FAIL: wrong dir: {path1}"

        # 第二次寫（append mode）
        path2 = write_trait_evolution_md(
            vr,
            trait_name="baseline_balance",
            old_value=0.35,
            new_value=0.40,
            delta=0.05,
            evidence_count=8,
            evolution_id="evo-bbb",
        )
        assert path2 == path1, "Round B FAIL: second write went to different path"

        content2 = path2.read_text(encoding="utf-8")
        assert "evo-aaa" in content2, "Round B FAIL: first entry missing after append"
        assert "evo-bbb" in content2, "Round B FAIL: second entry not appended"
        # frontmatter only once
        assert content2.count("type: trait_evolution") == 1, "Round B FAIL: frontmatter duplicated"

        print(f"  Round B-1 PASS: trait_evolution md at {path1.relative_to(vr)}")
        print(f"    first entry + append both present, single frontmatter")
    return True


# ─── Round B-2: write_memory_audit_md ───
def test_write_memory_audit_md():
    with temp_companion_vault() as vr:
        from agent_memory.companion.markdown_writers import write_memory_audit_md, MEMORY_AUDIT_DIR

        aid = uuid.uuid4().hex[:12]
        path = write_memory_audit_md(
            vr,
            audit_id=aid,
            audit_type="episodic_to_semantic_promote",
            user_id="viewer-001",
            session_id="s-audit-001",
            summary="偏好記憶從 episodic 升格到 semantic",
            details={"preference_id": "pref-0001", "old_status": "episodic", "new_status": "semantic"},
        )
        assert path is not None, "Round B FAIL: write_memory_audit_md returned None"
        assert path.exists(), f"Round B FAIL: memory audit md not found at {path}"

        content = path.read_text(encoding="utf-8")
        assert "type: memory_audit" in content
        assert "episodic_to_semantic_promote" in content
        assert "viewer-001" in content
        assert "偏好記憶從 episodic 升格到 semantic" in content
        assert "pref-0001" in content
        assert "82_Memory_Audit" in str(path)

        print(f"  Round B-2 PASS: memory_audit md at {path.relative_to(vr)}")
    return True


if __name__ == "__main__":
    failures = []
    tests = [
        ("Round B-1 write_trait_evolution_md", test_write_trait_evolution_md),
        ("Round B-2 write_memory_audit_md", test_write_memory_audit_md),
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
        print("ALL PASS (Round B)")
