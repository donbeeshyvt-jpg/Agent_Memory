---
type: skill
source: user
created: '2026-05-07T08:22:02.017540+00:00'
updated: '2026-05-07T08:22:02.017540+00:00'
agent: agent-f
status: active
schema_version: 1
tags:
- skill
- normalized
- agent-f
- persona
char_count: 544
extras:
  source_path: Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\verified_growth_skill.md
  owner_persona: agent-f
  scope: persona
  persona_id: agent-f
  normalized_at: '2026-05-07T08:22:02.017540+00:00'
---

# verified-growth-skill / Verified Growth Skill

## Purpose

- 由 `Z:\Cursor練習用\Agent_Memory\_tmp_persona_factory_vault\11_AI_Mirror\external_ingest\manual_skills\verified_growth_skill.md` 內化成系統技能格式。
- owner_persona: `agent-f`

## Trigger

- 當任務與此技能描述相符時，可提案套用。
- 若缺關鍵參數，先向使用者提問再執行。

## Steps

1. 讀取任務與上下文。
2. 套用技能步驟並產生可追蹤輸出。
3. 回寫必要記錄（任務/記憶/台帳）。

## Raw Source

```markdown
# Verified Growth Skill

## Purpose

- 以可驗證完成任務作為技能成長證據。

## Trigger

- 任務已完成，且需要沉澱流程。

## Steps

1. 確認任務完成證據存在。
2. 確認技能流程可重複。
3. 回寫技能使用與維護報告。

## Output

- 可升格技能候選。
```
