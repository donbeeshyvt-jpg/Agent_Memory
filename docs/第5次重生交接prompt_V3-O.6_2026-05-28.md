# 第 5 次重生 — 給下個 session AI 用的接手 prompt

> 複製貼上下面整段給新 AI session.

---

```
我從上個 session 接手 Agent_Memory V3 夥伴大腦專案. 上個 session 完成 V3-O.1 → V3-O.6 共 8 個 commit (含 V3-O.5 FullContextPromptPacketBuilder XML 12-section 大重寫 + V3-O.6 3 個小修), HEAD `adc6a18`, 已 push origin/main + test repo 同步, 第 4 輪 V3-O.5 DC 測試結束. 我即將開第 5 次 DC 測試 (fresh vault 已砍, bot 已停).

請依以下順序讀檔再回我:

1. 第 5 次重生交接 doc (本輪完整狀態):
   Z:\Cursor練習用\Agent_Memory\專案製作進度須知\AI_agent_程式編碼協作用\第5次重生交接_V3-O.6_2026-05-28.md
   (含 commit chain + V3-O.6 3 條補丁細節 + 第 5 輪 3 個觀察點 + sanity 步驟 + 開 bot 指令 + 程式碼位置 + 紅線)

2. V3-O.5 spec (Builder 設計原則):
   Z:\Cursor練習用\Agent_Memory\agent-memory-core\docs\FULL_CONTEXT_PROMPT_PACKET_BUILDER_SPEC.md (v1.1)
   核心:「Builder 只負責整理資料結構, 不負責替角色寫人格旁白」
   12-section XML 固定順序: packet_policy → parameter_dictionary → current_parameter_values
     → parameter_usage_rules → soul_and_persona_context → safety_and_boundary_rules
     → recent_learning_memory → relationship_and_viewer_memory → retrieved_second_brain_context
     → recent_dialogue_context → current_user_message → final_generation_instruction

3. 第 4 輪測試總結 (上輪實況 + V3-O.6 補丁清單):
   Z:\Cursor練習用\Agent_Memory\agent-memory-core\docs\第4次重生_V3-O.5總結_2026-05-28.md

4. V3-O.4 → V3-O.5 transition record (歷史脈絡):
   Z:\Cursor練習用\Agent_Memory\agent-memory-core\docs\V3-O.4_to_O.5_transition_record_2026-05-28.md

關鍵狀態:
- main repo HEAD: `adc6a18` (V3-O.6 #3 兩個 self-mod prompt 都換「精神體 + 反思 + 合理反對」風格)
- 雙環境 + origin/main 三邊同步
- tags (10+ 個): v3-phase-0/1-mvp/2/3 + v3-realistic-sim-p1 + v3-g/h/k-complete + v3-o.4-pre-o5-rewrite + v3-o.5-round4-complete
- vault: 已砍, 等下次 setup. 路徑 Z:\Cursor練習用\Agent_Memory\test\SecondBrains\companion_test (legacy backup 在同層 companion_test_legacy_2026-05-28)
- bot: 停了 (bridge + relay 都 down), 即將開測

V3-O.6 完成 3 條 (上輪結束時推完):

#1 (`6a11908`) _enforce_output_limits 拿掉 mid-sentence cut:
  - V3-E5 加的「每句 18 字硬切」造成第 4 輪 bot 回應「資料記。」「我。」斷句怪 — 拿掉
  - 保留 max_sentences=6 整句 cap
  - 輸出限制改放進 _render_final_generation_instruction 的 <output_formatting_rules> (input 約束)

#2+#3 (`4d3bb47` + `adc6a18`) self-mod 兩個 prompt 都換「精神體 + 反思 + 合理反對」:
  - _llm_summarize_owner_profile (寫 00.08):
    「你是 精神體 你可能是任何物種. 整理你對「owner / 主人 / 中之人」的觀察成 profile.
     歸納owner的偏好或希望教導的事情，包含任何 ( 雷點 / 對話風格 / 與你的關係定位 / 反思提議提出想法 / 合理反對意見)」
  - _llm_summarize_self_memory (寫 00.07) 同風格 adapted:
    「你是 精神體 你可能是任何物種.
     請整理你剛剛的對話成「我學到了什麼」自我反思 note. 第一人稱「我」.
     歸納我從互動中學到的事情，包含任何 (情緒洞察 / 邊界發現 / 反應 pattern / 觀眾或 owner 教我的 / 反思提議提出想法 / 合理反對自己的想法)」
  - 砍舊「重點:」bullets
  - 關鍵新加「合理反對」鼓勵夥伴有自己判斷, 不無條件吸收

第 5 次重生 3 個觀察點 (user 拍板):

T1. 收納知識 (40_Knowledge_Base):
  - curator daemon 跑後看 41_Daily_Knowledge / 42_External_Knowledge 有沒 .md 生
  - 對話要有強情緒 + 知識性 turn (|val|>0.5)
  - 測完跑: .\scripts\companion-curator-daemon.ps1 -VaultRoot "..." -Force

T2. 辨別不同 DC ID:
  - user 用 2 個帳號測 (主 owner = 1264637379789197342, 副帳當 viewer)
  - 預期: companion.db raw_events 出現 2+ user_id, intimacy_states 分流, V3-O.5 packet
    內 <relationship_and_viewer_memory> section 對 owner 用 00.08 raw / 對 viewer 用
    _load_viewer_dynamic_context 動態 (intim + raw_events 近 5 pair + preference top 3)
  - 驗證指令在交接 doc §2 T2

T3. 新「反思 + 合理反對」prompt 效果:
  - 對 owner 對話 ≥ 6 turn 觸發 background flush, qwen 用新 prompt 寫 00.07/00.08
  - 檢核 bullet 內是不是真的有「合理反對」「反思提議」這類自主想法句
  - 對比上輪舊 prompt 寫的全是順從觀察, 沒「不過我覺得 X」這類

下一步 user 拍板可能:
(a) sanity 確認 → setup vault → 開 bridge+relay → 我開第 5 輪測試 standby
(b) V3-O.6 #4 修 scanner 冒充偵測 (owner_aliases.json 自學, 1h)
(c) V3-O.6 #5 transport_ingest 加 --split-by-display-name (AI viewer pool 分流, 1h)
(d) V3-O.6 #6 LLM serialize lock 65s → 120s (viewer barrage timeout)
(e) 先看哪邊還有疑問

不該做 (見交接 doc §6 紅線):
- 主動清 vault (剛 fresh, 對話資料累積後不要動)
- 改 V3-O.5 12-section XML 結構 (對齊 spec)
- 拿掉 raw passthrough 改 parse SOUL (違反 spec §2)
- 重新加 _compose_*_block_v4 Builder 寫文案 helper (V3-O.4 廢)
- commit 含 token / API key
- 動 SOUL.md 主路徑核心邏輯 (Drift Guard 紅線)
- 砍「合理反對 / 反思提議」段 (user 明確拍板)

讀完後給我:
- 當前狀態 1 句話總結 (HEAD / commit chain / V3-O.6 重點 / bot 狀態)
- 你準備好接哪個 path (a/b/c/d/e)
- 不清楚的地方
```

---

複製貼上完上面那段給新 AI 後, 它會 read 交接 doc + spec + 第 4 輪總結, 然後回你「狀態 1 句 + 接哪 path + 不清楚的」.

新 session 就有完整 context, 可以無縫接著做第 5 輪測試.
