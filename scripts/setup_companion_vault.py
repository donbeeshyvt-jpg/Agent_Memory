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
from agent_memory.llm_routing import save_llm_router_config


_VAULT_ROOT_README_TPL = """# V3 夥伴大腦 — 你的 Vault 索引

> 這是 V3 companion vault, 跟你對話、有情緒、會成長的孩子住在這裡.
> 你可以在 Obsidian 開這個資料夾, 隨時看 / 改 / 紀錄.

## 📂 重要資料夾速查

| 資料夾 | 用途 | 你能改? |
|---|---|---|
| `00_System_Core/` | 夥伴的靈魂 / 設定 / 紅線 / 自學筆記 / 主人 profile | ✅ **你的主場** — 看 [00_System_Core/_INDEX_README.md](00_System_Core/_INDEX_README.md) |
| `10_Working_Memory/` | 短期對話紀錄 | 讀為主 |
| `20_Audience_Graph/` | 觀眾關係圖譜 (VIP/Casual/Inside Jokes) | 讀為主 |
| `30_Emotional_State/` | 情緒事件 / Trait Evolution / Mood Diary | 讀為主 |
| `40_Knowledge_Base/` | 知識庫 (Lore / Game / Topics) | ✅ 可投餵知識 |
| `50_Skills_Tools/` | 技能 / hermes 學的 / tool audit | 讀為主 |
| `60_Preference_Memory/` | 偏好記憶 | 讀為主 |
| `70_Persona_Versions/73_Candidates/` | 人格漂移候選 (drift_guard) | ⚠ 你 active 才生效 |
| `80_Audit_Trace/` | 決策稽核 | 讀為主 |
| `90_Daily_Journal/` | 每日心情 | 讀為主 |
| `99_Archive/auto_archived/` | 自動降級封存的記憶 (90/180d) | 讀為主 |
| `99_Templates/` | 模板 | 讀為主 |
| `.ai/companion.db` | 29 個 SQLite 業務表 (情緒/天平/親密度/...) | ❌ 純機讀 |

## 🚀 First-time setup 必做 3 件

1. **填 `00_System_Core/00.06_Companion_SOUL.md`** — 寫夥伴的角色設定 (name / 個性 / 紅線 / 口頭禪). 這是「永久角色錨」.
2. **可選: 填 `00.01_Persona.md` / `00.04_Safety_Rules.md` / `00.05_Brand_Voice.md`** — 更細節人設.
3. **跟夥伴 chat** — 它會自己累積 `00.07_Companion_MEMORY.md` 跟 `00.08_Owner_Profile.md`.

對齊 V3-E5 (user 2026-05-27 Q1+Q2 拍板): 每 chat turn 夥伴會**動態讀** SOUL/Persona/Safety/Brand_Voice/MEMORY/Owner_Profile 進 LLM system prompt. 你 obsidian 改完它**立刻看見**.
"""

_SYSTEM_CORE_INDEX_TPL = """# 00_System_Core 索引 — 你的記憶修改地圖

> 這 8 個檔每個都對應夥伴大腦不同層. 哪些夥伴自動改 / 哪些你手動改, 一目了然.

## 完整索引

| 檔名 | 用途 | 夥伴自動改 | 你能手動改 | 注意 |
|---|---|---|---|---|
| `00.01_Persona.md` | 核心人設 + 價值觀 | ❌ Drift Guard 保護 | ✅ 隨時 | V3-E5 後夥伴讀進 prompt |
| `00.02_SystemPrompt.md` | 系統指令 baseline | ❌ | ✅ 你可創 | 預設不存在 (optional) |
| `00.03_Governor_Rules.md` | 防漂移約束 | ❌ | ✅ 你可創 | 預設不存在 (optional) |
| `00.04_Safety_Rules.md` | 紅線 (Hard Rules) | ❌ 保護 | ✅ 隨時 | V3-E5 後夥伴讀進 prompt |
| `00.05_Brand_Voice.md` | VTuber 口頭禪 / 招牌動作 | ❌ | ✅ 你可創 | 預設不存在 (optional) |
| `00.06_Companion_SOUL.md` ⭐ | 靈魂 (永久角色錨) | ❌ Drift Guard | ✅ **隨時, V3-E5 重點** | **你寫角色設定的地方** |
| `00.07_Companion_MEMORY.md` 🤖 | 夥伴自學筆記 | ✅ **自動每 6 turn flush LLM 整理** | ✅ 隨時 (但會被覆蓋累積) | dynamic — 「我學到了 X」 |
| `00.08_Owner_Profile.md` 🤖 | 主人 profile | ✅ **自動每 6 turn flush LLM 整理** | ✅ 隨時 | dynamic — 「主人偏好 / 風格」 |
| `personalities/` | 多 personality 模式 (daily / stream / intimate) | ❌ | ✅ 你可加 | hot-reload |

## 圖例
- ❌ = 夥伴**不會**自動改 (你的編輯不會被覆蓋)
- ✅ = 夥伴**會**自動寫 (你的編輯可能被累積/覆蓋)
- ⭐ = 你**第一次**就該填 (永久角色錨)
- 🤖 = 純動態 LLM 整理, 你看到的就是夥伴的學習軌跡

## 開始填寫的順序

1. ⭐ **`00.06_Companion_SOUL.md`** (必填) — 填角色 name + character_archetype + catchphrases
2. (可選) `00.01_Persona.md` — 更細節人設 + 價值觀
3. (可選) `00.04_Safety_Rules.md` — 加你自己的紅線
4. (可選) `00.05_Brand_Voice.md` — 寫角色口頭禪

填完跟夥伴 chat. **夥伴每 chat turn 動態讀這些檔**, 你改完它立刻看見.

對齊 V3-E5 (user 2026-05-27 拍板).
"""


def _write_vault_index_files(vault: Path) -> None:
    """V3-E5 (user 2026-05-27 Q2): 寫 README 索引文件給 user 看哪些檔可改."""
    root_readme = vault / "README.md"
    sysc_index = vault / "00_System_Core" / "_INDEX_README.md"
    if not root_readme.exists():
        root_readme.write_text(_VAULT_ROOT_README_TPL, encoding="utf-8")
    if not sysc_index.exists():
        sysc_index.parent.mkdir(parents=True, exist_ok=True)
        sysc_index.write_text(_SYSTEM_CORE_INDEX_TPL, encoding="utf-8")


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
    # 對齊真實 setup 流程 2026-05-26: read_brain_type 對沒 file 的 vault 回 'steward' default,
    # 不是 raise — 所以要直接看 file exists.
    bt_path = vault / ".ai" / "brain_type.json"
    if not bt_path.exists():
        bt_path.parent.mkdir(parents=True, exist_ok=True)
        write_brain_type(vault, "companion")
        print(f"[step 1] brain_type=companion 寫入 .ai/brain_type.json (新 vault)")
    else:
        existing = read_brain_type(vault)
        if existing != "companion":
            raise RuntimeError(
                f"vault 已綁定 brain_type={existing!r}, 不能改成 companion. "
                f"請另開新 vault 路徑."
            )
        print(f"[step 1] brain_type 已是 companion (skip)")

    # Step 6 (前): bootstrap vault skeleton (10 區資料夾 + frontmatter 模板)
    adapter = ObsidianVaultAdapter(vault)
    adapter.ensure_skeleton()
    print(f"[step 6a] vault skeleton bootstrap (10 區資料夾)")

    # Step 6 (後): companion.db 29 表 schema migration
    ensure_companion_db(vault)
    print(f"[step 6b] companion.db 29 SQLite 表 ensure")

    # V3-D6: 寫 llm_router.yaml — 預設 openrouter 為 global_default, gemini fallback
    companion_router = {
        "schema_version": 1,
        "description": "V3 companion: OpenRouter 為主, Gemini fallback. Phase 1 stub 透過 env AGENT_MEMORY_COMPANION_LLM_FORCE_STUB=1 切回.",
        "resolution_order": [
            "request_override", "auxiliary_override", "persona_override",
            "global_default", "fallback_chain",
        ],
        "global_default": {
            "profile": "openrouter",
            "model": "google/gemma-2-9b-it:free",
        },
        "fallback_chain": [
            {"profile": "openrouter", "model": "meta-llama/llama-3.1-8b-instruct:free"},
            {"profile": "gemini", "model": "gemma-4-31b-it"},
        ],
        "persona_overrides": {
            "companion": {
                "profile": "openrouter",
                "model": "google/gemma-2-9b-it:free",
            },
        },
        "auxiliary_default": {"profile": "", "model": ""},
        "auxiliary_overrides": {},
        "providers": {
            "openrouter": {
                "kind": "openai_compatible",
                "zh_label": "OpenRouter 聚合 API",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "requires_api_key": True,
                "max_tokens": 500,  # V3-E6 (user 2026-05-27 拍板): 300→500 鬆綁, 仍受 _enforce_output_limits 1-6 句 ≤18 字 post-process 截
            },
            "gemini": {
                "kind": "openai_compatible",
                "zh_label": "Google Gemini / Gemma API",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key_env": "GOOGLE_API_KEY",
                "requires_api_key": True,
                "max_tokens": 500,
            },
        },
    }
    save_llm_router_config(vault, companion_router)
    print(f"[step 5] llm_router.yaml 寫入: openrouter(google/gemma-2-9b-it:free) → 對齊 V3-D6 接真實 LLM")

    # ⭐ V3-E5 (user 2026-05-27 Q2): 寫 README 索引文件 (vault root + 00_System_Core/)
    _write_vault_index_files(vault)
    print(f"[step 5b] V3-E5 索引文件: README.md + 00_System_Core/_INDEX_README.md (告訴 user 哪些檔可改)")

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

    # ⭐ V3-O.1 (user 2026-05-28 拍板): companion_config.yaml.owner.discord_user_id 跟 owner_state 對齊
    # bootstrap 已生 baseline yaml (discord_user_id=""), 此處用 --owner-user-id 填回去
    # 對齊 V3-L owner 判定鏈: companion_config.yaml 優先, fallback DB owner_state
    config_yaml = vault / "00_System_Core" / "companion_config.yaml"
    if config_yaml.exists():
        import re
        text = config_yaml.read_text(encoding="utf-8")
        new_text = re.sub(
            r'(\n\s*discord_user_id:\s*)""([ \t]*#[^\n]*)?',
            lambda m: f'{m.group(1)}"{owner_user_id}"{m.group(2) or ""}',
            text,
            count=1,
        )
        new_text = re.sub(
            r'(\n\s*label:\s*)"中之人"',
            f'\\g<1>"{owner_label}"',
            new_text,
            count=1,
        )
        new_text = re.sub(
            r'(\n\s*directive_acceptance_weight:\s*)[0-9.]+',
            f'\\g<1>{owner_directive_weight}',
            new_text,
            count=1,
        )
        if new_text != text:
            config_yaml.write_text(new_text, encoding="utf-8")
            print(f"[step 3b] companion_config.yaml.owner 對齊 (discord_user_id={owner_user_id})")

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
