---
type: skill
source: user
created: '2026-05-07T08:16:53.804927+00:00'
updated: '2026-05-07T08:16:53.804927+00:00'
agent: agent-a
status: active
schema_version: 1
tags:
- skill
- normalized
- agent-a
- persona
char_count: 514
extras:
  source_path: Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\news_digest_skill.md
  owner_persona: agent-a
  scope: persona
  persona_id: agent-a
  normalized_at: '2026-05-07T08:16:53.804927+00:00'
---

# news-digest-skill / News Digest Skill

## Purpose

- 由 `Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\news_digest_skill.md` 內化成系統技能格式。
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
# News Digest Skill

## Purpose

- 整理每日新聞重點並輸出報告。

## Trigger

- 使用者要求彙整新聞。

## Steps

1. 搜尋相關新聞來源。
2. 摘錄重點與事件脈絡。
3. 生成摘要與待確認問題。

## Output

- 報告草稿
```
