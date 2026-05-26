"""V3 companion vault setup helper.

對齊 V3 §3.2 first-run-wizard companion 流程 (Step 1-6).
之後接 Discord 前必須跑一次此 script 設好 vault + owner_state.

執行:
    python scripts/setup_companion_vault.py --vault Z:/.../SecondBrains/companion_test \
                                              --owner-user-id <discord_user_id> \
                                              --owner-label "我的中之人" \
                                              [--owner-directive-weight 0.85]

對齊 V3 §3.2 wizard 引導:
- Step 1: brain_type = companion
- Step 3: Owner Identity (user_id / 標籤 / directive 接受權重)
- Step 6: bootstrap vault skeleton + 29 SQLite 表
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# cp950 / UTF-8 safety (對齊 R21.1 C114 修補)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agent_memory.vault.obsidian import ObsidianVaultAdapter, write_brain_type, read_brain_type
from agent_memory.companion.companion_db import ensure_companion_db, open_companion_db


def setup_companion_vault(
    vault: Path,
    owner_user_id: str,
    owner_label: str = "我的中之人",
    owner_directive_weight: float = 0.85,
    soul_path: str = "00_System_Core/00.06_Companion_SOUL.md",
) -> None:
    vault = Path(vault).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)

    # Step 1: brain_type=companion (永久綁定)
    try:
        existing = read_brain_type(vault)
        if existing != "companion":
            raise RuntimeError(
                f"vault 已綁定 brain_type={existing!r}, 不能改成 companion. "
                f"請另開新 vault."
            )
        print(f"[step 1] brain_type 已是 companion (skip)")
    except FileNotFoundError:
        write_brain_type(vault, "companion")
        print(f"[step 1] brain_type=companion 寫入 .ai/brain_type.json")
    except RuntimeError:
        raise

    # Step 6 (前): bootstrap vault skeleton (10 區資料夾 + frontmatter 模板)
    adapter = ObsidianVaultAdapter(vault)
    adapter.ensure_skeleton()
    print(f"[step 6a] vault skeleton bootstrap (10 區資料夾)")

    # Step 6 (後): companion.db 29 表 schema migration
    ensure_companion_db(vault)
    print(f"[step 6b] companion.db 29 SQLite 表 ensure")

    # Step 3: Owner Identity 寫入 owner_state
    with open_companion_db(vault) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO owner_state
               (owner_user_id, soul_path, directive_acceptance_weight,
                relationship_label, total_directive_count, directive_accepted_count,
                last_drift_check_at)
               VALUES (?, ?, ?, ?, 0, 0, ?)""",
            (owner_user_id, soul_path, owner_directive_weight, owner_label,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    print(f"[step 3] owner_state 寫入: user_id={owner_user_id} "
          f"label={owner_label} directive_weight={owner_directive_weight}")

    # 建 Companion SOUL.md placeholder (使用者要去填角色設定)
    soul_full = vault / soul_path
    if not soul_full.exists():
        soul_full.parent.mkdir(parents=True, exist_ok=True)
        soul_full.write_text(
            "---\n"
            "type: companion_soul\n"
            "schema_version: 10\n"
            f"created_at: {datetime.now().isoformat()}\n"
            "---\n\n"
            "# Companion SOUL — 角色靈魂設定\n\n"
            "## 角色設定 (使用者請填)\n\n"
            "- 名字: \n"
            "- 個性 baseline: \n"
            "- 喜歡的事: \n"
            "- 不喜歡的事: \n"
            "- 口頭禪: \n\n"
            "## 紅線\n\n"
            "- 過度擬人化 / 主張意識 → OG1 自動攔\n"
            "- Owner 不蓋 safety → H9 decision engine\n"
            "- 防裝熟 → interaction_count < 5 強制 balance_axis ≤ 0\n",
            encoding="utf-8",
        )
        print(f"[step 4] Companion SOUL.md placeholder: {soul_path}")

    print()
    print("✅ V3 companion vault setup 完成")
    print(f"   vault path: {vault}")
    print(f"   下一步: 跑 transport_bridge_server 連這個 vault, 或直接 CLI smoke test:")
    print(f"     python -m agent_memory.transport_bridge_server --vault {vault} --port 16001")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V3 companion vault setup")
    parser.add_argument("--vault", required=True, help="vault 路徑 (e.g. SecondBrains/companion_test)")
    parser.add_argument("--owner-user-id", required=True,
                        help="Owner Discord user id (e.g. 123456789012345678)")
    parser.add_argument("--owner-label", default="我的中之人",
                        help="Owner 標籤 (e.g. 我的爸爸 / 我的中之人)")
    parser.add_argument("--owner-directive-weight", type=float, default=0.85,
                        help="Owner directive 接受權重 (0.5-0.95, default 0.85)")
    args = parser.parse_args(argv)

    setup_companion_vault(
        vault=Path(args.vault),
        owner_user_id=args.owner_user_id,
        owner_label=args.owner_label,
        owner_directive_weight=args.owner_directive_weight,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
