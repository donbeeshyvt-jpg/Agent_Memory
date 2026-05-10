# ENTRY_START_HERE

目標：只用最少步驟啟動核心，並確保第二大腦/模型都在核心目錄外。

## 0) 一鍵引導（建議）
Windows 直接雙擊 `START_SETUP.bat`，或：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\first-run-wizard.ps1
```

> 這個引導會幫你做安裝檢查、`pip install -e .`、以及 `bootstrap-v1`。
> 預設會跳過 `pip/setuptools/wheel` 升級；若要強制升級可加 `-UpgradePipPackages`。

## 1) 手動安裝
```powershell
cd "<PROJECT_ROOT>/agent-memory-core"
pip install -e .
```

## 2) 一鍵建立第二大腦 + 管家
```powershell
.\scripts\bootstrap-v1.ps1 -SetDefaultVault -Json
```

## 3) 下載模型（到核心外的 `../0_Models`）
```powershell
pip install -e .[llama-cpp]
pip install -U "huggingface_hub[cli]"
huggingface-cli download ggml-org/gemma-4-E4B-it-GGUF gemma-4-E4B-it-Q8_0.gguf --local-dir "../0_Models/gemma-4-E4B-it-GGUF"
huggingface-cli download seerware/Qwen3.5-9B-GGUF Qwen3.5-9B-Q8_0.gguf --local-dir "../0_Models/Qwen3.5-9B-GGUF"
```

## 4) 建立入口設定
```powershell
Copy-Item .\scripts\entry-stack.sample.json .\scripts\entry-stack.local.json
.\scripts\setup-entry-stack.ps1 -VaultRoot "<VAULT_ROOT>" -ConfigFile .\scripts\entry-stack.local.json -Json
```

## 5) 啟動 Bridge（預設 16000）
```powershell
.\scripts\run-bridge.ps1 -VaultRoot "<VAULT_ROOT>" -Port 16000
```

## 6) 啟動 Discord Relay
```powershell
Copy-Item .\scripts\discord-relay-stack.sample.json .\scripts\discord-relay-stack.local.json
$env:DISCORD_BOT_TOKEN_ROLE1="..."
$env:DISCORD_BOT_TOKEN_ROLE2="..."
$env:DISCORD_BOT_TOKEN_ROLE3="..."
.\scripts\manage-discord-relay-stack.ps1 -Action start -ConfigFile .\scripts\discord-relay-stack.local.json
```

## 7) 工具能力驗證
```powershell
.\scripts\run-tooling-smoke.ps1 -VaultRoot "<VAULT_ROOT>" -Json
```

## 8) 收尾（清殘留程序）
```powershell
.\scripts\manage-discord-relay-stack.ps1 -Action stop-stray
```
