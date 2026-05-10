---
type: skill
source: agent
created: '2026-05-07T08:02:41.174244+00:00'
updated: '2026-05-07T08:02:41.174244+00:00'
agent: manager-b
status: active
schema_version: 1
tags:
- skill
- merged
char_count: 1197
extras:
  merged_from:
  - code-review-basic
  - code-review-advanced
  merged_at: '2026-05-07T08:02:41.174244+00:00'
---

# code-review-playbook / merged skill

## Purpose

- 合併多個相近技能，避免重複與碎片化。
- 若任務需求不足，先向使用者確認關鍵參數。

## Merged From

- `code-review-basic` (00_System/Skills/code-review-basic/SKILL.md)
- `code-review-advanced` (00_System/Skills/code-review-advanced/SKILL.md)

## Combined Playbook

### Source: code-review-basic

```markdown
# code-review-basic / Code Review Basic

## Purpose

- 由 `Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\code_review_basic.md` 內化成系統技能格式。
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
# Code Review Basic

- 先看需求
- 再看測試
```
```

### Source: code-review-advanced

```markdown
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
```
