---
type: skill
source: user
created: '2026-05-07T08:02:35.546186+00:00'
updated: '2026-05-07T08:02:41.182246+00:00'
agent: user
status: archived
schema_version: 1
tags:
- skill
- normalized
- agent-a
char_count: 422
extras:
  source_path: Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\code_review_advanced.md
  owner_persona: agent-a
  normalized_at: '2026-05-07T08:02:35.546186+00:00'
  archive_reason: merged_into:code-review-playbook
---


# code-review-advanced / Code Review Advanced

## Purpose

- 由 `Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\code_review_advanced.md` 內化成系統技能格式。
- owner_persona: `agent-a`

## Trigger

- 當任務與此技能描述相符時，可提案套用。
- 若缺關鍵參數，先向使用者提問再執行。

## Steps

1. 讀取任務與上下文。
2. 套用技能步驟並產生可追蹤輸出。
3. 回寫必要記錄（任務/記憶/台帳）。

## Raw Source

```markdown
# Code Review Advanced

- 比對架構一致性
- 評估風險與回滾策略
```
