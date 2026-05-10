# RELEASE_NOTES_v1

## 版本
- 版本：`v0.1.0`（核心第一版候選）
- 日期：`2026-05-10`

## 已完成
- 角色入口型別完成：`tooling` / `chat`（`emotive` 保留開發中、入口不可選）。
- Persona 治理策略已落地：`chat` 角色會限制工具能力。
- 對話內本地工具第一版完成：`/tool` 支援 `list_dir/read_file/write_file/append_file/mkdir`。
- 工具能力與角色權限已串接到 transport 主流程（Discord/CLI 共用）。
- 增加殘留程序清理：`manage-discord-relay-stack.ps1 -Action stop-stray`。
- 本地 bridge/relay 預設埠統一為 `16000`。
- 一鍵啟動流程：`scripts/bootstrap-v1.ps1`（外部第二大腦 + 外部模型目錄）。
- Vault 預設路徑統一為核心外 `../SecondBrains/default_second_brain`（CLI config + 主要 scripts）。

## Gate 憑證
- Phase5 promotion gate：
  - `artifacts/phase5_promotion_runs/phase5-promotion-20260510-160239/summary.json`
- Tooling smoke（steward 可寫檔、chat 被拒絕）：
  - `artifacts/release_v1/tooling-smoke-20260510-160249.json`
- stop-stray 檢查：
  - `scripts/manage-discord-relay-stack.ps1 -Action stop-stray -Json`（`ok=true`、`items=[]`）

## 未完成 / 有意保留
- `emotive` 人格型別仍為保留能力，尚未開放入口建立。
- 模型下載目前採「文件命令 + 使用者環境下載」，未做強制自動下載流程。
- 多通道壓測與角色互監自動化仍屬下一階段。

## 風險
- `/tool` 屬第一版，支援的是檔案系統基礎動作，不含進程控制與高風險系統操作。
- 若使用者把第二大腦/模型目錄設在核心內，會破壞 repo 邊界與發版可攜性。
- Discord/Bridge 長時運行仍仰賴本機權限與環境穩定性。

## 回滾
1. 關閉所有測試程序：
   - `scripts/manage-discord-relay-stack.ps1 -Action stop-stray`
2. 若需回到純對話模式：
   - `persona-update --persona <id> --role-type chat`
3. 若需暫時停用工具型角色：
   - `persona-disable --persona <id>`
4. 清理本次測試寫入：
   - 刪除 `artifacts/tooling_smoke/` 內測試檔。

## 發版建議
- 僅以 `agent-memory-core` 作為 Git 邊界與首次推送內容。
- 第二大腦資料夾與模型資料夾保持在核心外部。
- 推送前再次執行：
  - `scripts/run-tooling-smoke.ps1 -VaultRoot "<VAULT_ROOT>" -Json`
  - `scripts/run-phase5-promotion-gate.ps1 -PythonExe python`
  - `scripts/manage-discord-relay-stack.ps1 -Action stop-stray`

