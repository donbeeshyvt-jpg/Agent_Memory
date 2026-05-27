# V3-O.4 → V3-O.5 Transition Record (2026-05-28)

> **Checkpoint**: HEAD `4ad244c` (V3-O.4 pushed origin/main + test repo sync) — 此前 prompt builder 走錯方向, 接下來 V3-O.5 整套重寫對齊 user 拍板 spec.

---

## §0. user 拍板 spec (放本資料夾)

**Spec 檔**: [`FULL_CONTEXT_PROMPT_PACKET_BUILDER_SPEC.md`](FULL_CONTEXT_PROMPT_PACKET_BUILDER_SPEC.md) (v1.1, 1178 行)

核心原則:
> **Builder 只負責整理資料結構, 不負責替角色寫人格旁白.**
> **FULL_CONTEXT_MODE = 保留完整原始資料 + 加結構標籤 + 加 usage rule, 但不翻譯、不演繹、不寫情緒解釋句.**

---

## §1. V3-O.2 → V3-O.3 → V3-O.4 為什麼都錯

| 版本 | 我做的 | spec 角度 |
|---|---|---|
| V3-O.2 | 4946 chars, 含 emoji + V3-XX meta tag + hardcoded 例子「水做的史萊姆」 | 多重違規: emoji / 人工演繹 |
| V3-O.3 | 1039 chars, 拿太多, 變數沒語意 | 違反「不要壓縮 / 不要刪 raw 資料」 |
| V3-O.4 | 4593 chars, 每變數帶 hardcoded 解釋句 | **嚴重違規** — Builder 寫了大量「(心情正負, -1 最壞 ~ +1 最好; 此值 = 心情好壞)」這類解釋句 (spec §2.1 / §11.2 明確列禁) |

### V3-O.4 具體違規 (9 條)

1. ❌ **Builder 自己生成情緒解釋句** — `_compose_state_block_v4` 內每個變數附「(...)」解釋, 違反 spec §2
2. ❌ **Builder 自己寫關係口吻句** — 「目前 = 陌生」「不要深度共情、不裝熟」spec 明確列禁
3. ❌ **Parse SOUL.md 重新組合** — spec §7.5 要求 raw passthrough, 我 parse 成 key:value 雙重違規
4. ❌ **沒 XML 結構** — 我用 `【】` 中文括弧 freeform, spec §6+§12 要求 `<full_context_prompt_packet>` XML
5. ❌ **缺 5 個 sections**: `packet_policy` / `parameter_dictionary` (純規格) / `current_parameter_values` (純數字) / `parameter_usage_rules` / `final_generation_instruction`
6. ❌ **dictionary 跟 values 混在一起** — spec §5+§7.2+§7.3 要求分開
7. ❌ **Safety Rules 硬編碼在 Builder** — spec §7.6 要求從 `00.04_Safety_Rules.md` raw 拉
8. ❌ **禁詞 / 多語言 / 5 步驟 chain 混在主 prompt** — spec 要求分到對應 sections
9. ❌ **`current_user_message` 沒標 `priority="highest"`** — spec §7.11 + Test 6 明確要求

---

## §2. V3-O.5 規劃 (FullContextPromptPacketBuilder)

對齊 spec §5 12-section 固定順序:

```xml
<full_context_prompt_packet version="1.1" mode="FULL_CONTEXT">
  <packet_policy>            ← spec §7.1 宣告 full context + 禁止行為
  <parameter_dictionary>     ← spec §7.2 純規格 (range/meaning/usage/not_usage)
  <current_parameter_values> ← spec §7.3 純數字, 不附解釋
  <parameter_usage_rules>    ← spec §7.4 control signals only
  <soul_and_persona_context> ← spec §7.5 SOUL/Persona/Brand_Voice raw passthrough
  <safety_and_boundary_rules>← spec §7.6 Safety_Rules.md raw
  <recent_learning_memory>   ← spec §7.7 00.07 raw
  <relationship_and_viewer_memory>  ← spec §7.8 owner/viewer raw
  <retrieved_second_brain_context>  ← spec §7.9 memory_router 4-layer + RAG raw
  <recent_dialogue_context>  ← spec §7.10 近 12 turn raw
  <current_user_message priority="highest">  ← spec §7.11 最新 user msg
  <final_generation_instruction>  ← spec §7.12 鎖任務
</full_context_prompt_packet>
```

### 重要砍除

- 砍 `_compose_role_block_v4` (V3-O.4 加的, 寫文案違規)
- 砍 `_compose_state_block_v4` (V3-O.4 加的, 寫文案違規)
- 砍 `_parse_soul_yaml` (parse 違反 raw passthrough)
- `_humanize_affect` 不在 prompt 用 (留給 audit / debug)
- 砍 `_extract_filled_role_content` (V3-O.3 加的, 已被 V3-O.4 改成 _compose_role_block_v4, 整個方向都廢)

### 重要新加

- `_render_packet_policy()` — hardcoded
- `_render_parameter_dictionary()` — hardcoded field 定義
- `_render_current_parameter_values(...)` — XML 包數字
- `_render_parameter_usage_rules()` — hardcoded 規則
- `_render_soul_and_persona_context(vault)` — read 00.06/00.01/00.05 raw
- `_render_safety_and_boundary_rules(vault)` — read 00.04 raw
- `_render_recent_learning_memory(vault, is_owner)` — read 00.07 raw
- `_render_relationship_and_viewer_memory(is_owner, vault, viewer_ctx)` — owner 00.08 raw 或 viewer 動態
- `_render_retrieved_second_brain_context(memory_ctx, knowledge_hits)` — 4-layer + RAG raw
- `_render_recent_dialogue_context()` — usage policy 包 messages array
- `_render_current_user_message(msg)` — `<current_user_message priority="highest">`
- `_render_final_generation_instruction(decision)` — hardcoded 含 decision 條件分支
- `PromptPacketValidator` — 加 unit test 防 builder 自動產情緒解釋句 (對齊 spec §11)

### 驗收 (對齊 spec §10 7 個 Test)

1. ✓ 不得生成「能量偏高, 有一點興奮感」等解釋句
2. ✓ 不得生成「對方是觀眾, 熟悉度 0.16... 不要裝熟」等關係文案
3. ✓ `parameter_dictionary` 必在 `current_parameter_values` 之前
4. ✓ 必含 `parameter_usage_rules` section
5. ✓ FULL_CONTEXT 不得刪 raw source 內容
6. ✓ `<current_user_message priority="highest">` 必須存在
7. ✓ `final_generation_instruction` 必含「Do not create additional interpretation prose」「Answer only current_user_message」

---

## §3. 第 4 次測試 V3-O.5 後 expected 行為

新 prompt LLM 行為預期:

| section | LLM 怎麼讀 |
|---|---|
| `<packet_policy>` | 知道這是 full context, builder 沒寫文案, 我自己整合 |
| `<parameter_dictionary>` | 學一次 valence/arousal/intimacy 是什麼 + range |
| `<current_parameter_values>` | 看純數字 `<valence>0.05</valence>`, 配上面 dict 自己理解 |
| `<parameter_usage_rules>` | 知道參數只是 control signal, 不可外顯 |
| `<soul_and_persona_context>` | RAW 讀 SOUL.md, 直接看中之人寫的角色設定, 不被 builder 綁架 |
| `<safety_and_boundary_rules>` | RAW 讀 Safety_Rules.md |
| `<recent_learning_memory>` | RAW 讀 00.07 self_reflection |
| `<relationship_and_viewer_memory>` | RAW 讀 viewer profile / owner profile |
| `<retrieved_second_brain_context>` | RAW 讀 memory_router 4-layer + knowledge RAG hits |
| `<recent_dialogue_context>` | 看 messages array 連續性 |
| `<current_user_message priority="highest">` | 知道焦點 |
| `<final_generation_instruction>` | 鎖任務, 用角色語氣回, 不洩漏 packet |

---

## §4. 工程量預估

| 階段 | 時間 |
|---|---|
| 砍 V3-O.4 加的 helper (compose_role/state/parse_soul) | 30 min |
| 寫 12 個 `_render_*` helper | 1.5h |
| 重寫 `_build_companion_system_prompt` | 30 min |
| 加 `PromptPacketValidator` + 7 個 test | 1h |
| Sanity 雙綠 + commit + push | 30 min |
| Dump 新 prompt 給 user 看 (跟 V3-O.4 對照) | 15 min |
| **合計** | **~4h** |

---

## §5. 下一步

1. 此 transition record commit + push (本 doc + spec.md 進 git)
2. 開動 V3-O.5 rewrite

對齊 user 拍板:
> 「幫下版本紀錄 先推一版 然後來修正吧, 你沒改 我也不知道你有沒有真懂.」
> 「就是要每一句都附帶參數解釋的多agent結構分析變數然後包含記憶帶下結果回傳才有意義」

→ V3-O.5 直接 rewrite code 證明.
