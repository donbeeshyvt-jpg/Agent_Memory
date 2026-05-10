# DEPLOY_TRANSPORT_BRIDGE

本文件說明如何部署 `memory-cli serve-transport-bridge`，讓 Discord/LINE 透過 webhook 進入核心對話流程。

## 1) 啟動 Bridge（本地）

```powershell
cd "<PROJECT_ROOT>/agent-memory-core"
memory-cli --vault-root "<VAULT_ROOT>" serve-transport-bridge --host 127.0.0.1 --port 16000
```

健康檢查：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:16000/health"
```

也可用腳本：

```powershell
.\scripts\run-bridge.ps1 -VaultRoot "<VAULT_ROOT>" -BindHost 127.0.0.1 -Port 16000
```

## 2) 反向代理（Nginx 範例）

```nginx
server {
  listen 443 ssl;
  server_name your-bridge.example.com;

  client_max_body_size 256k;

  location /webhook/discord {
    proxy_pass http://127.0.0.1:16000/webhook/discord;
    proxy_connect_timeout 3s;
    proxy_read_timeout 30s;
    proxy_send_timeout 30s;
  }

  location /webhook/line {
    proxy_pass http://127.0.0.1:16000/webhook/line;
    proxy_connect_timeout 3s;
    proxy_read_timeout 30s;
    proxy_send_timeout 30s;
  }
}
```

## 3) Discord payload 最小格式

`transport_profiles.yaml` 需要 `content`、`channel_id`、`author.id`：

```json
{
  "content": "你好，測試訊息",
  "channel_id": "guild123-thread456",
  "author": {
    "id": "user-42"
  },
  "persona": "core"
}
```

本地送測：

```powershell
curl -X POST "http://127.0.0.1:16000/webhook/discord" `
  -H "Content-Type: application/json" `
  -d "{\"content\":\"你好，測試訊息\",\"channel_id\":\"guild123-thread456\",\"author\":{\"id\":\"user-42\"}}"
```

## 4) LINE payload 最小格式

```json
{
  "events": [
    {
      "type": "message",
      "replyToken": "test-reply-token",
      "source": {
        "userId": "line-user-1"
      },
      "message": {
        "type": "text",
        "text": "你好，LINE 測試"
      }
    }
  ]
}
```

本地送測：

```powershell
curl -X POST "http://127.0.0.1:16000/webhook/line" `
  -H "Content-Type: application/json" `
  -d "{\"events\":[{\"type\":\"message\",\"replyToken\":\"test-reply-token\",\"source\":{\"userId\":\"line-user-1\"},\"message\":{\"type\":\"text\",\"text\":\"你好，LINE 測試\"}}]}"
```

## 5) 多角色 Relay（Discord）

```powershell
Copy-Item .\scripts\discord-relay-stack.sample.json .\scripts\discord-relay-stack.local.json
$env:DISCORD_BOT_TOKEN_ROLE1="..."
$env:DISCORD_BOT_TOKEN_ROLE2="..."
$env:DISCORD_BOT_TOKEN_ROLE3="..."
.\scripts\manage-discord-relay-stack.ps1 -Action start -ConfigFile .\scripts\discord-relay-stack.local.json
.\scripts\manage-discord-relay-stack.ps1 -Action status -ConfigFile .\scripts\discord-relay-stack.local.json
```

## 6) 收尾（清理殘留）

```powershell
.\scripts\manage-discord-relay-stack.ps1 -Action stop-stray
```
