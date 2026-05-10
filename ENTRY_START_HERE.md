# ENTRY_START_HERE

目標：fresh clone 後最少步驟啟動核心，並確保第二大腦/模型都在核心目錄外。

## 0) 一鍵啟動（推薦）
Windows 直接雙擊 `START_SETUP.bat` → 進入**美化選單**：

```
╔════════════════════════════════════════════════════════════════╗
║                                                                ║
║ AGENT MEMORY CORE                                     v0.1.0  ║
║ 本機 LLM × 多角色記憶 × Discord 串接                              ║
║                                                                ║
╚════════════════════════════════════════════════════════════════╝

  目前環境狀態：
    [✓] Python                : v3.10.6
    [✓] agent_memory CLI      : 可用
    [✓] 第二大腦 vault         : default_second_brain
    [✓] LLM 預設              : llama_cpp_local / gemma-4-E4B-Q8_0
    [○] Discord token         : 未設

  請選擇：
    [1] 快速設定               自動建大腦 + 配本機 gemma-4 + 跑 chat 驗證
    [2] 自訂設定               逐步互動：選 LLM、要不要 Discord、要不要下載
    [3] 上線管家到 Discord     啟 bridge + relay，貼 token 即上線
    [4] 切換 LLM 模型          本機 ↔ Gemini / OpenAI / OpenRouter / Claude
    [5] CLI 試聊管家           直接在這視窗對話
    [6] 跑工具能力 smoke       驗證 /tool 寫檔
    [7] 重新掃描狀態
    [Q] 離開
```

選號碼即可，每個動作完成回主選單。**Q 離開**。

要直接跑舊版 wizard：`START_SETUP.bat --legacy`

## 0.5) 各選項對應的 sub-script（給 power user）
| 選項 | 對應命令 |
|---|---|
| [1] 快速設定 | `.\scripts\first-run-wizard.ps1 -NonInteractive` |
| [2] 自訂設定 | `.\scripts\first-run-wizard.ps1` |
| [3] 上線 DC | `.\scripts\start-steward.ps1 -PersistToken` |
| [4] 切 LLM | `.\scripts\switch-llm.ps1 -PersistKey` |
| [5] CLI chat | `python -m agent_memory.cli chat ...` |
| [6] tooling smoke | `.\scripts\run-tooling-smoke.ps1` |

## 0.6) wizard 細節（[1] / [2] 跑的東西）
wizard 預設會做：
- 檢查 Git / Python ≥ 3.10（缺的話用 winget 自動裝）
- **`pip install -e .` 自動執行**（已可 import 則自動 skip；要強制重裝加 `-InstallEditable`，要完全跳過加 `-SkipInstallEditable`）
- 跑 `bootstrap-v1`（建第二大腦 00~99 + 管家 steward）
- 把 vault 設為 user 預設（`vault-set`），之後 CLI 不用每次傳 `--vault-root`
- 檢查 `../0_Models` 裡的 GGUF（缺的話互動詢問下載 ~10GB）
- 把 gemma-4 配進 `llm_router.yaml`（自動偵測 Ollama CUDA path）
- 跑一次 chat smoke 驗證

可選旗標：
- `-InstallEditable`：強制 `pip install -e .`（即使已可 import）
- `-SkipInstallEditable`：完全跳過 pip 安裝
- `-SkipModelCheck`：跳過模型檢查
- `-SkipModelDownload`：跳過模型下載提示
- `-UpgradePipPackages`：升級 pip/setuptools/wheel
- `-SetupDiscord -DiscordChannelId <id>`：一併設定 Discord steward relay
- `-VaultRoot <path>`：用獨立測試大腦（不污染正式 vault）
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

## 1.6) 切換到線上 API 模型（Google Gemini / OpenAI / OpenRouter / Claude）
```powershell
.\scripts\switch-llm.ps1 -PersistKey
# 互動選 provider [1-6]，再選 model：
#   [1] 本機 llama-cpp-python (GGUF)
#   [2] Google Gemini API     (推薦 gemini-2.5-flash, 免費層夠用)
#   [3] OpenAI                (gpt-4.1-mini)
#   [4] OpenRouter            (anthropic/claude-sonnet-4.6 或各家)
#   [5] Anthropic Claude      (claude-sonnet-4-6)
#   [6] 本機 Ollama
# 沒設過 API key 會 SecureString 安全 prompt（輸入不顯示）
# -PersistKey 把 key 寫入 Windows 使用者環境變數（registry，不寫檔案、不推 git）
# 切完自動跑一次 chat smoke 驗證
```

兩條切換命令任挑一條（同樣的事）：
```powershell
# A. 用 switch-llm.ps1（互動、自動 prompt key）
.\scripts\switch-llm.ps1

# B. 直接 CLI 命令（你已經設好 env 變數時用這個比較快）
$env:GOOGLE_API_KEY = "你的Gemini key"
python -X utf8 -m agent_memory.cli llm-set-default --profile gemini --model gemini-2.5-flash
```

可用 provider id 與最常見 model：
| Provider | API key env | 推薦 model |
|---|---|---|
| `llama_cpp_local` | (無) | `../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf` |
| `gemini` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| `openai` | `OPENAI_API_KEY` | `gpt-4.1-mini` / `gpt-4.1` |
| `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4.6` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| `ollama_local` | (無) | `qwen2.5:14b` |

要單獨給某個角色用線上、其他角色仍本機：
```powershell
python -X utf8 -m agent_memory.cli llm-set-persona --persona steward --profile gemini --model gemini-2.5-flash
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
