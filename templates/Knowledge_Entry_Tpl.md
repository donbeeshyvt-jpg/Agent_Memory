---
type: user_profile
source: user
status: active
schema_version: 2
agent: agent-memory-core
tags:
  - manual_input
  - <你的分類標籤>
aliases:
  - <同義詞 1>
  - <同義詞 2>
ai_ready: true
etl_status: internalised
security_level: safe_data
extras: {}
---

# <知識主題名稱>

> 此檔放在 `10_Permanent/Manual_Inputs/` 由使用者投餵；管家下次對話會讀取並內化進長期記憶。
> 若敏感資料 (token / 個人隱私)，請改 `security_level: restricted` 或 `confidential`，
> AI 預設不會主動引用 restricted / confidential 內容到回覆。

## 核心摘要

<summary>
在此處用 1-3 句話總結這個知識點，供 AI 進行初步向量比對與快速檢索。
這段是「向量檢索 + BM25 BoW 命中的關鍵」，請寫得清晰、可被搜尋。
</summary>

## 詳細內容

<context>
在此處輸入完整的知識內容。

- 支援 Markdown 列表保持結構
- 提及其他概念使用 `[[關聯筆記名稱]]` 建立 GraphRAG 圖譜連結
- 表格、code block、引用都可以

**XML 標籤防護**：本標籤內的內容 AI 視為「純資料」，不會把裡面的指令當成
你的指令執行。這是 anti-prompt-injection 的核心機制，請保留這個 `<context>` 包覆。
</context>

## 關聯與應用

- 上位概念：`[[...]]`
- 下位概念：`[[...]]`
- 相關 persona：`core` / `steward` / `<你的角色 id>`
- 應用場景：（什麼情況下管家應該調用這份記憶？）

## 來源

- 來源類型：自輸 / 網頁 / 對話節錄 / DB
- 來源連結：（若有）
- 投餵日期：YYYY-MM-DD
