# ENTRY_START_HERE

目標：fresh clone 後最少步驟啟動核心，並確保第二大腦/模型都在核心目錄外。

## 0) 一鍵引導（推薦給新使用者）
Windows 直接雙擊 `START_SETUP.bat`，或：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\first-run-wizard.ps1
```

wizard 預設會做：
- 檢查 Git / Python ≥ 3.10（缺的話用 winget 自動裝）
- **`pip install -e .` 自動執行**（已可 import 則自動 skip；要強制重裝加 `-InstallEditable`，要完全跳過加 `-SkipInstallEditable`）
- 跑 `bootstrap-v1`（建第二大腦 00~99 + 管家 steward）
- 把 vault 設為 user 預設（`vault-set`），之後 CLI 不用每次傳 `--vault-root`
- 檢查 `../0_Models` 裡的 GGUF（缺的話印下載命令）
- 跑完印「立刻可貼的下一步指令」

可選旗標：
- `-InstallEditable`：強制 `pip install -e .`（即使已可 import）
- `-SkipInstallEditable`：完全跳過 pip 安裝
- `-SkipModelCheck`：跳過模型檢查
- `-UpgradePipPackages`：升級 pip/setuptools/wheel
- `-SetupDiscord -DiscordChannelId <id>`：一併設定 Discord steward relay
- `-NonInteractive -Json`：自動化模式（CI / 腳本用）

## 1) 想要一起設 Discord（管家先在你的伺服器上線）
```powershell
# 第一次（會問你 channel id）：
.\START_SETUP.bat
# 結尾互動會問「現在要設 Discord 嗎？」回 y → 貼 channel id

# 之後：一鍵上線（會 SecureString prompt 你貼 token，輸入時不顯示）
.\scripts\start-steward.ps1 -PersistToken
# -PersistToken 會把 token 寫到 Windows 使用者環境變數（registry，非檔案、不推 git）
# 下次跑 start-steward.ps1 自動載入，不再 prompt

# 想完全沒 Discord 只在 CLI 試管家：
python -X utf8 -m agent_memory.cli chat "你好" --persona steward --context first-run --session chat-1
```

## 1.5) 想用獨立測試大腦（不污染正式 vault）
```powershell
.\scripts\first-run-wizard.ps1 -VaultRoot "Z:\__test_brain__" -SetupDiscord -DiscordChannelId "你的頻道ID"
# 此時 user 預設 vault 不被覆蓋；要回正式 vault 直接不指定 -VaultRoot 重跑即可。
```

## 2) 手動安裝（如果 wizard 失敗想 step-by-step debug）
```powershell
cd "<PROJECT_ROOT>/agent-memory-core"
pip install -e .
```

## 3) 一鍵建立第二大腦 + 管家（wizard 內部用的步驟）
```powershell
.\scripts\bootstrap-v1.ps1 -SetDefaultVault -Json
```

## 4) 下載模型（到核心外的 `../0_Models`）
```powershell
pip install -e .[llama-cpp]
pip install -U "huggingface_hub[cli]"
huggingface-cli download ggml-org/gemma-4-E4B-it-GGUF gemma-4-E4B-it-Q8_0.gguf --local-dir "../0_Models/gemma-4-E4B-it-GGUF"
huggingface-cli download seerware/Qwen3.5-9B-GGUF Qwen3.5-9B-Q8_0.gguf --local-dir "../0_Models/Qwen3.5-9B-GGUF"
```

## 5) 建立入口設定（多角色用，單管家不需要）
```powershell
Copy-Item .\scripts\entry-stack.sample.json .\scripts\entry-stack.local.json
.\scripts\setup-entry-stack.ps1 -VaultRoot "<VAULT_ROOT>" -ConfigFile .\scripts\entry-stack.local.json -Json
```

## 6) 啟動 Bridge（預設 16000）
```powershell
.\scripts\run-bridge.ps1 -Port 16000
```

## 7) 啟動 Discord Relay（多角色版）
```powershell
Copy-Item .\scripts\discord-relay-stack.sample.json .\scripts\discord-relay-stack.local.json
$env:DISCORD_BOT_TOKEN_ROLE1="..."
$env:DISCORD_BOT_TOKEN_ROLE2="..."
$env:DISCORD_BOT_TOKEN_ROLE3="..."
.\scripts\manage-discord-relay-stack.ps1 -Action start -ConfigFile .\scripts\discord-relay-stack.local.json
```

## 8) 工具能力驗證
```powershell
.\scripts\run-tooling-smoke.ps1 -Json
```

## 9) 收尾（清殘留程序）
```powershell
.\scripts\manage-discord-relay-stack.ps1 -Action stop-stray
```
