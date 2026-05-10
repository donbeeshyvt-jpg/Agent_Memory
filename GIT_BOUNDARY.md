# GIT_BOUNDARY

## 邊界決策
- 正式 Git 邊界：`agent-memory-core/`
- 不把上層工作區（`Z:/Cursor練習用` 或 `Agent_Memory` 其他目錄）一起納入首版。

## 原因
- 核心程式可獨立下載與啟動。
- 第二大腦與模型資料在核心外部，避免 repo 膨脹與機敏洩漏。
- 發版、回滾、協作責任範圍更清楚。

## 初始化建議
在 `agent-memory-core` 目錄內執行：
```powershell
git init
git add .
git commit -m "chore: v0.1.0 core bootstrap and tooling"
```

## 目錄策略
- 核心內保留：程式碼、腳本、文件、範例設定。
- 核心外保留：
  - 第二大腦（例：`../SecondBrains/default_second_brain`）
  - 模型（例：`../0_Models`）

## 上傳前檢查
```powershell
scripts/validate-entry-config.ps1 -Json
scripts/manage-discord-relay-stack.ps1 -Action stop-stray
```
