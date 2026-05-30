# 第8次重生 DC 測試交接 prompt（V3-O.11）

> 貼這份給下一個 context window 即可無縫接手。現況細節另見同目錄
> **`第7次重生_V3-O.11_驗收與第8次重生待查清單.md`**（C1–C14 檢查 + 待查 + 背景觀察）。

---

我要開第 8 次重生 DC 測試 Agent_Memory V3-O.11 夥伴大腦。環境已備好，你幫我啟動 bot + 全程背景觀察。**本次重點：驗收第7次做的「統一回話動態彙整（正式直播型態，不單一回覆留言）+ 朋友卡記憶層」是否真的生效。**

## == 當前狀態（第7次重生已完成）==
- **main repo**：`Z:\Cursor練習用\Agent_Memory\agent-memory-core`，HEAD `ca87d92`，**tag `v3-o.11-rebirth7`**（已 push github `donbeeshyvt-jpg/Agent_Memory`）。code 在 `63e912b`。
- **test 副本（bot 實際用）**：`Z:\Cursor練習用\Agent_Memory\test\agent-memory-core`（code 與 main 一致，最新）。
- **vault**：`Z:\Cursor練習用\Agent_Memory\test\SecondBrains\companion_test`（已重置 **fresh**：DB 不存在、朋友卡 0、SOUL v11/config 保留）。重置前備份在 `test\_companion_test_resetbak_20260530`。
- **新格式已實證**：fresh+新code 跑出 P0 寫入(affect/appraisal) + 朋友卡反思段/對話彙整段/中文 stage 都正確。

## == V3-O.11 新架構（第8次要驗的核心）==
1. **統一回話彙整**：viewer 訊息 → `record_only` 個別記錄(完整, 不生回覆) → 進 per-channel 佇列 → debounce 滑動視窗(**安靜6s / 5–10動態有效句 / 30s上限**, 程式快篩短句表情不算) → relay 背景 task 觸發 → bridge 用 **main_chat(deepseek)** 統一彙整生成 → **channel.send 發頻道(不單一 reply)**。**owner 豁免、即時獨立回**。
2. **朋友卡記憶層**：撈卡帶「反思 + 近10句對話彙整」(本地 gemma)；日重整(curator layer3 24h)/7天昇華(layer4 7d)；重要性加權(emotional_salience/intimacy)。
3. **模型角色**：出口回覆=`main_chat`(deepseek) / 記憶彙整反思昇華=`sub_tasks`(本地 gemma)，統一在 `companion_config.yaml` 切換；`max_packet_tokens: 160000`。

## == LLM 架構 ==
- 主對話/彙整出口：OpenRouter `deepseek/deepseek-v4-pro` → `qwen/qwen3.6-35b-a3b` → `deepseek/deepseek-v4-flash:free`（三者實打過：pro/qwen 200、free 429 限流）
- 記憶子任務：本地 `gemma-4-E4B-it-Q8`（RTX 3090 全 GPU, n_gpu_layers=-1, 首次載入~6s 之後<1s）
- CUDA 已修；import llama_cpp 失敗 → `python scripts/setup_local_llm_cuda.py`

## == 啟動 bot（2 視窗背景）==
視窗 A — bridge(16001)：
```
cd Z:\Cursor練習用\Agent_Memory\test\agent-memory-core
Get-Content ..\.env.test.local | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { Set-Item "env:$($Matches[1].Trim())" $Matches[2].Trim() } }
$env:PYTHONUNBUFFERED = "1"
python -X utf8 -u -c "from pathlib import Path; from agent_memory.transport_bridge_server import serve_transport_bridge; serve_transport_bridge(Path('Z:/Cursor練習用/Agent_Memory/test/SecondBrains/companion_test'), port=16001)"
```
視窗 B — relay：
```
cd Z:\Cursor練習用\Agent_Memory\test\agent-memory-core
Get-Content ..\.env.test.local | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { Set-Item "env:$($Matches[1].Trim())" $Matches[2].Trim() } }
$env:PYTHONUNBUFFERED = "1"
python -X utf8 -u scripts\discord_bridge_relay.py --token-env DISCORD_BOT_TOKEN_COMPANION --bridge-url http://127.0.0.1:16001 --mode executor --persona companion --channel-id 1502434065880449086 --allow-bot-author 1502621329663332432 --split-by-display-name --allow-llm-degraded --vault "Z:/Cursor練習用/Agent_Memory/test/SecondBrains/companion_test"
```
> ⚠ relay 已加**背景 flush task**（每 3s 輪詢 bridge `_aggregator_flush_check`）+ **held 不個別回 viewer** + **channel.send 發頻道**。owner 走原 reply。

## == 測試帳號 ==
A 我本人 owner (1264637379789197342) / B AI BOT viewer pool（訊息開頭「<名字>: 」測中文小米奇/暗夜貓貓 + 英文 Lila.0214；bot id 1502621329663332432）/ C 真人第3帳號

## == 要檢查的程序（重啟後逐項驗，詳見驗收 doc C1–C14）==
1. **啟動健康**：bridge online、relay online、owner 第一句 deepseek 不 404
2. **C3 owner 即時** → **C2 viewer held(不個別回)** → **C4 多人 barrage 彙整發頻道(非單一reply)**
3. **C5 程式快篩**：viewer 連發「666」「哈」「😂」不觸發；有意義句才算
4. **C6 debounce**：安靜6s / 滿5–10句 / 30s 上限，先到先發
5. **C7 多頻道分桶**：config 填 `channel_ids` 兩頻道，各自彙整不混
6. **C8 朋友卡新格式**：看 `20_Audience_Graph/22_Casual_Viewers/<id>.md` 有「## 我對這位的理解(反思)」+「## 近期對話彙整」+ 中文 stage
7. **C1 P0**：`python test\_db_peek.py`（或自寫）看 affect_states/appraisal_records > 0
8. **C11 壓測不雪崩**：barrage → gemma 快速 degrade(~15s 不卡120s) + 出口 O(1)
9. **C12 反思不外漏**、**C13 模型角色**、**C14 昇華接線**(跑 curator layer3/4 看 viewer_cards_daily_refined/weekly_consolidated)

## == 背景觀察（★ 最重要，掛 2 個 Monitor）==
- **Monitor A**（per-turn 延遲）：tail `companion_test\.ai\turn_timings.jsonl`，解析 `step15_llm_call`(deepseek 出口/彙整)、`step4_5_llm_emotion_fallback`(gemma)、`total_ms`、`top3_slowest`、`is_owner`。**註：turn_timings 欄位是 `steps`(純ms數字)/`trace_id`/`is_owner`/`total_ms`/`top3_slowest`，不是 step_timings/turn_id**（前次踩過坑）。
- **Monitor B**（relay 三段）：tail `companion_test\.ai\relay.log`，抓 `[TIMING] recv_pre/bridge_roundtrip/reply_send` + `[ERR]`/Traceback/degraded。relay.log 是 PowerShell 寫的、用 python `errors="replace"` 讀。
- **三類觀察點**（詳見 doc 第四節）：① 運算時間（彙整出口 deepseek 大input延遲 / gemma 是否快速degrade / barrage 是否 O(1) 出口）② 記憶成長（DB 各表 + 朋友卡演化）③ **壓縮對話**（朋友卡「近期對話彙整」是否壓縮非堆積 / 日重整7天昇華 / 重要性加權 / 彙整 input 大小）
- 基準：deepseek 個別 3–15s（彙整大input可更久但省N次）/ gemma 0.2–2s 駐留、degrade~15s / reply_send 0.2–1.5s

## == 測完喊停要做 ==
`python scripts\audit_v3o10_round6.py --vault <companion_test>`（須在 agent-memory-core 下）+ 撈 turn_timings 算 p50/p95 延遲分布 + DB/00-99 比對 + 找彙整/雪崩卡點 → 寫**第8次重生實測紀錄** doc 到「AI_agent_程式編碼協作用」資料夾。

## == 已知限制 / 待續（第8次可接的下階段）==
- **owner 記憶內化層（Part B 確定需求、本輪未做）**：夥伴對 viewer 說話時要帶 owner 教導記憶(內化)。本輪彙整 context 為骨架(朋友卡+最近對話)；下階段補「owner 教導記憶層」注入。**待 user 確認邊界**：owner 私密身份是否對 viewer 體現，或僅 owner 教的道理/風格/知識。
- 動態句數 5→10 目前程式固定門檻 5，LLM 動態調整為後續。
- emotion_event_md 在 record_only 寫空 bot_reply（情緒核心記錄在 emotion_state DB、不受影響）。
- 真實多 viewer barrage 端到端發頻道 = 本次第8次要實打確認的重點。

## == 重要工作流程提醒 ==
**正確順序：弄上核心 → 紀錄 doc → 更新測試環境 → 重置大腦 → 確認 fresh 出來的檔案對 → 才推**（確認檔案格式正確後才 push/tag，勿提早推）。
