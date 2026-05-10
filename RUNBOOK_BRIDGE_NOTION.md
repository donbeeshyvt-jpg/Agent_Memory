# RUNBOOK_BRIDGE_NOTION

用於維運 `Bridge + Discord Relay + Notion Queue`。

## 啟動 Bridge（16000）
```powershell
cd "<PROJECT_ROOT>/agent-memory-core"
.\scripts\run-bridge.ps1 -VaultRoot "<VAULT_ROOT>" -BindHost 127.0.0.1 -Port 16000
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:16000/health"
```

## 設定 channel -> persona
```powershell
Copy-Item .\scripts\discord-entry.sample.json .\scripts\discord-entry.local.json
.\scripts\setup-discord-entry.ps1 -VaultRoot "<VAULT_ROOT>" -ConfigFile .\scripts\discord-entry.local.json -Json
```

## 啟動多 Relay
```powershell
Copy-Item .\scripts\discord-relay-stack.sample.json .\scripts\discord-relay-stack.local.json
$env:DISCORD_BOT_TOKEN_ROLE1="..."
$env:DISCORD_BOT_TOKEN_ROLE2="..."
$env:DISCORD_BOT_TOKEN_ROLE3="..."
.\scripts\manage-discord-relay-stack.ps1 -Action start -ConfigFile .\scripts\discord-relay-stack.local.json
.\scripts\manage-discord-relay-stack.ps1 -Action status -ConfigFile .\scripts\discord-relay-stack.local.json
```

## 對話內工具（tooling 角色）
```text
/tool {action:write_file,target:workspace,path:artifacts/tooling_smoke/demo.txt,content:hello}
```

`chat` 角色會被拒絕（`tools_disabled_for_persona`）。

## Notion Queue
```powershell
python -X utf8 -m agent_memory.cli --vault-root "<VAULT_ROOT>" notion-queue --title "週報草稿" --body "## 本週重點" --tag weekly --priority normal
python -X utf8 -m agent_memory.cli --vault-root "<VAULT_ROOT>" notion-queue-list --status pending --limit 20
```

## 收尾
```powershell
.\scripts\manage-discord-relay-stack.ps1 -Action stop-stray
```
