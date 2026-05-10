# agent-memory-core

`agent-memory-core` 是 `Agent_Memory` 的核心執行層，負責第二大腦、角色治理、記憶流程、以及 Discord/Web/Line 通訊橋接。

## 角色型別（入口）
1. `tooling`：可寫程式 + 有記憶。
2. `chat`：純聊天 + 有記憶（限制工具能力）。
3. `emotive`：開發中，入口不可選。

## 重要原則（V1）
- 第二大腦（Vault）必須在核心目錄外。
- 模型資料夾必須在核心目錄外。
- 橋接服務預設埠為 `16000`。

## 安裝入口（像 openclaw/hermes 的引導）
Windows 直接雙擊：
```bat
START_SETUP.bat
```

或命令列執行：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\first-run-wizard.ps1
```

這個引導會做：
- 檢查/安裝 `git`（可選，自動詢問）。
- 檢查/安裝 `python`（可選，自動詢問）。
- 預設跳過 `pip/setuptools/wheel` 升級（需要時可加 `-UpgradePipPackages`）。
- 預設跳過核心可編輯安裝（需要時可加 `-InstallEditable`，或後續手動安裝）。
- 可選安裝 `llama-cpp-python`（你可先跳過）。
- 建立第二大腦 + 初始化管家角色（呼叫 `bootstrap-v1.ps1`）。

後續若要安裝核心可編輯套件：
```powershell
python -m pip install -e .
```

## 一個命令起手（手動建立環境）
```powershell
cd "<PROJECT_ROOT>/agent-memory-core"
.\scripts\bootstrap-v1.ps1 -SetDefaultVault -Json
```

預設會建立（都在核心外部）：
- 第二大腦：`../SecondBrains/default_second_brain`
- 模型目錄：`../0_Models`

## 模型下載（你目前使用的兩款）
若要使用本地 GGUF 推理（`llama_cpp_python` provider），先安裝可選依賴：
```powershell
pip install -e .[llama-cpp]
```

先安裝 huggingface CLI：
```powershell
pip install -U "huggingface_hub[cli]"
```

下載 Gemma4 E4B Q8：
```powershell
huggingface-cli download ggml-org/gemma-4-E4B-it-GGUF gemma-4-E4B-it-Q8_0.gguf --local-dir "../0_Models/gemma-4-E4B-it-GGUF"
```

下載 Qwen3.5 9B Q8：
```powershell
huggingface-cli download seerware/Qwen3.5-9B-GGUF Qwen3.5-9B-Q8_0.gguf --local-dir "../0_Models/Qwen3.5-9B-GGUF"
```

## 入口與 Relay
```powershell
Copy-Item .\scripts\entry-stack.sample.json .\scripts\entry-stack.local.json
Copy-Item .\scripts\discord-relay-stack.sample.json .\scripts\discord-relay-stack.local.json
```

啟動 bridge：
```powershell
.\scripts\run-bridge.ps1 -VaultRoot "<VAULT_ROOT>" -BindHost 127.0.0.1 -Port 16000
```

啟停 relay：
```powershell
.\scripts\manage-discord-relay-stack.ps1 -Action start  -ConfigFile .\scripts\discord-relay-stack.local.json
.\scripts\manage-discord-relay-stack.ps1 -Action status -ConfigFile .\scripts\discord-relay-stack.local.json
.\scripts\manage-discord-relay-stack.ps1 -Action stop   -ConfigFile .\scripts\discord-relay-stack.local.json
.\scripts\manage-discord-relay-stack.ps1 -Action stop-stray
```

## 管家工具實測（release 憑證之一）
```powershell
.\scripts\run-tooling-smoke.ps1 -VaultRoot "<VAULT_ROOT>" -Json
```

對話內工具格式：
```text
/tool {action:write_file,target:workspace,path:artifacts/tooling_smoke/demo.txt,content:hello}
```

## 發版前 Gate（release 憑證）
```powershell
.\scripts\run-phase5-promotion-gate.ps1 -PythonExe python
.\scripts\run-tooling-smoke.ps1 -VaultRoot "<VAULT_ROOT>" -Json
```

## 安全原則
- 不要把 token / API key 寫進 repo。
- 僅使用 `*.local.json` 或環境變數存放機敏資訊。
- 複製專案前先重發新 token。
