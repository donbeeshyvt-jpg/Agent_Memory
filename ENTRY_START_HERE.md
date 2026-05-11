# ENTRY_START_HERE

從 0 開始的 fresh user 入口指南。雙擊 `START_SETUP.bat` 進入互動主選單即可。

---

## 主選單

雙擊 `START_SETUP.bat` 看到：

```
╔══════════════════════════════════════════════════════════════════╗
║ AGENT MEMORY CORE                                       v0.1.0   ║
║ 本機 LLM × 多角色記憶 × Discord 串接                                ║
╚══════════════════════════════════════════════════════════════════╝

  目前環境狀態：
    [✓] Python                : v3.10+
    [✓] agent_memory CLI      : 可用
    [✓] 第二大腦 vault         : default_second_brain
    [✓] LLM 預設              : [本機推理] llama_cpp_local / gemma-4-E4B
    [○] Discord token         : 未設

  請選擇：

    [1] 快速設定               自動建大腦 + 配本機模型 + 跑 chat 驗證
    [2] 自訂設定               逐步互動：選 LLM、要不要 Discord、要不要下載
    [3] 上線管家到 Discord     啟 bridge + relay，貼 token 即上線
    [4] 切換 LLM 模型          本機 ↔ Gemini / OpenAI / OpenRouter / Claude
    [5] 下載本地模型           Gemma / Qwen3 8B/14B/30B / Llama / Phi
    [6] 第一次測聊管家         直接在這視窗對話一次（不用 Discord）
    [7] 跑工具能力 smoke
    [8] 重新掃描狀態
    [Q] 離開
```

選號碼 → 動作完成自動回主選單 → **Q 離開**。

---

## 第一次設定建議流程

| 順序 | 動作 | 預期 |
|---|---|---|
| 1 | 雙擊 `START_SETUP.bat` | 看主選單 |
| 2 | 按 `[1]` 快速設定 | 自動裝 pip、建大腦、配模型、提示是否下載 |
| 3 | 按 `[5]` 下載本地模型（如果沒有） | 選 1-7 號模型，看進度條跑完 |
| 4 | 按 `[6]` 試聊管家 | 看回應驗證一切順 |
| 5 | 按 `[3]` 上線到 Discord | 第一次會 prompt 你貼 Discord Bot Token |
| 6 | 到綁好的 channel `@steward` | 看管家回覆 |

---

## 模型選擇（基礎三套）

預設只提供 **2 個本地模型 + Google API**，三選一夠用。要更多進階選項見最下方「進階」段落。

### A. 本地推理（推薦給有 GPU/8GB+ RAM 的）

按 `[5] 下載本地模型`：

| 號 | 模型 | 大小 | 適合 |
|---|---|---|---|
| 1 | **gemma-4 E4B Instruct Q8** | ~4 GB | 快速啟動，4-6GB RAM 也能跑，預設首選 |
| 2 | **Qwen3.5-9B Q8** | ~10 GB | 中文流暢，主要對話角色用 |

下載完按 `[4] 切換 LLM 模型 → [1] 本機 llama-cpp-python` 選對應路徑即可。

### B. 雲端 API（推薦給沒 GPU / 想試最新模型的）

按 `[4] 切換 LLM 模型 → [2] Google Gemini / Gemma API`：

| API key env | 推薦 model | 備註 |
|---|---|---|
| `GOOGLE_API_KEY` | `gemini-2.5-flash` | 最快，免費層大，預設首選 |
| `GOOGLE_API_KEY` | `gemini-2.5-pro` | 推理強，較慢 |
| `GOOGLE_API_KEY` | `gemma-4-31b-it` | Gemma 系列大模型 |
| `GOOGLE_API_KEY` | `gemma-4-26b-a4b-it` | Gemma sparse MoE |

加 `-PersistKey` 旗標把 API key 寫入 Windows 使用者環境變數（registry，不寫檔，不推 git）。

---

## Discord 上線

按 `[3] 上線管家到 Discord`：
- 第一次會 SecureString prompt 你貼 Discord Bot Token（輸入時不顯示）
- 啟動 bridge :16000 在背景 + relay 在前景
- 到綁好的 channel `@steward 你好` 開始對話
- 結束按 `Ctrl+C`

加 `-PersistToken` 把 token 寫入使用者環境變數（同上，不寫檔）。

要先用 wizard 綁 channel：
```powershell
.\scripts\first-run-wizard.ps1 -SetupDiscord -DiscordChannelId "你的頻道ID"
```

---

## 角色 / 模型搭配

每個 persona 可以單獨指定用哪個 model：
```powershell
# 管家 steward 用本地 gemma-4（輕量、執行工具用）
python -X utf8 -m agent_memory.cli llm-set-persona --persona steward --profile llama_cpp_local --model "../../0_Models/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q8_0.gguf"

# 主要對話角色用 Qwen3.5-9B（中文流暢）
python -X utf8 -m agent_memory.cli llm-set-persona --persona writer-curator --profile llama_cpp_local --model "../../0_Models/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q8_0.gguf"

# 全域 default 設成 Gemini（雲端 fallback）
python -X utf8 -m agent_memory.cli llm-set-default --profile gemini --model gemini-2.5-flash
```

## 進階：其他 cloud provider（非預設）

`switch-llm.ps1` 選單還有以下 provider，預設不主推但可選：
- `[3] OpenAI 商用 API` — `OPENAI_API_KEY` / `gpt-4.1-mini`
- `[4] OpenRouter` — `OPENROUTER_API_KEY` / `anthropic/claude-sonnet-4.6` (一個 key 用各家)
- `[5] Anthropic Claude 直連` — `ANTHROPIC_API_KEY` / `claude-sonnet-4-6`
- `[6] 本機 Ollama` — 需先 `ollama serve`

要直接用 CLI 不開選單：
```powershell
$env:OPENAI_API_KEY = "sk-..."
python -X utf8 -m agent_memory.cli llm-set-default --profile openai --model gpt-4.1-mini
```

---

## 進階：直接跑各個 sub-script（跳過選單）

| 動作 | 命令 |
|---|---|
| 快速設定 | `.\scripts\first-run-wizard.ps1 -NonInteractive` |
| 自訂設定 | `.\scripts\first-run-wizard.ps1` |
| 用獨立測試大腦不污染主 vault | `.\scripts\first-run-wizard.ps1 -VaultRoot "Z:\__test_brain__"` |
| 設好 Discord 一併下去 | `.\scripts\first-run-wizard.ps1 -SetupDiscord -DiscordChannelId "..."` |
| 下載模型 | `.\scripts\download-model.ps1` 或 `-ModelKey qwen3-8b` |
| 上線管家 | `.\scripts\start-steward.ps1 -PersistToken` |
| 切 LLM | `.\scripts\switch-llm.ps1 -PersistKey` |
| 工具能力 smoke | `.\scripts\run-tooling-smoke.ps1` |
| 直接跑舊版 wizard（不走選單） | `START_SETUP.bat --legacy` |

---

## 安全 / git 邊界

`*.local.json`、`artifacts/`、`runtime_brains/` 都被 `.gitignore` 蓋住。
第二大腦 (`SecondBrains/`) 與模型 (`0_Models/`) 都在 `agent-memory-core` repo **外部**。
API key / Discord token **永不寫檔案** — 只走 process 環境變數或 Windows 使用者 registry（`-PersistKey` / `-PersistToken`）。
