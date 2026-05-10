---
type: skill
source: user
created: '2026-05-07T08:17:16.416920+00:00'
updated: '2026-05-07T08:17:16.416920+00:00'
agent: agent-b
status: active
schema_version: 1
tags:
- skill
- normalized
- agent-b
- persona
char_count: 531
extras:
  source_path: Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\daily_news_pipeline.md
  owner_persona: agent-b
  scope: persona
  persona_id: agent-b
  normalized_at: '2026-05-07T08:17:16.416920+00:00'
---

# daily-news-pipeline / Daily News Pipeline

## Purpose

- 由 `Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\daily_news_pipeline.md` 內化成系統技能格式。
- owner_persona: `agent-b`

## Trigger

- 當任務與此技能描述相符時，可提案套用。
- 若缺關鍵參數，先向使用者提問再執行。

## Steps

1. 讀取任務與上下文。
2. 套用技能步驟並產生可追蹤輸出。
3. 回寫必要記錄（任務/記憶/台帳）。

## Raw Source

```markdown
# Daily News Pipeline

## Purpose

- 追蹤每日重大新聞並分區整理。

## Trigger

- 使用者要求今日新聞總覽。

## Steps

1. 搜尋新聞關鍵字。
2. 蒐集來源並標記可信度。
3. 產出重點摘要與後續追蹤列表。

## QA

- 不確定資訊要標示待確認。
```
