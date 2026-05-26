# V3-G Pressure Tests

> 對齊 user 2026-05-27 goal 「每階段做壓測程式有 LOG 通過才下一步」
> 每個 V3-G* commit 都有對應 pressure test 程式, 跑通才下一步.

## 架構

```
test/v3g_pressure_tests/
├── README.md                                 # 本檔
├── logs/                                     # LOG 目錄 (timestamp 紀錄)
├── run_g01_memory_ctx_budget.py             # G1: memory_ctx[:2400] 真的撈完整 4-layer
├── run_g02_h3_daydream_integrated.py        # G2: daydream_engine 真的接 chat_runtime
├── run_g03_h4_embodied_state_integrated.py  # G3: embodied_state 進 prompt section E
├── run_g04_h10_metacognition_integrated.py  # G4: metacognition Step 16 後檢查
├── run_g05_h11_emotion_contagion.py         # G5: emotion_contagion Step 5 加群體
├── run_g06_h12_expectation_state.py         # G6: expectation Step 4 期待 diff
├── run_g07_curator_l4_llm_consolidation.py  # G7: L4 7d deep LLM 摘要
├── run_g08_40_knowledge_base_redesign.py    # G8: 40 區重設計 (日常/外部)
├── run_g09_v3f4_knowledge_pipeline.py       # G9: V3-F4 知識管道 + index + retrieve
├── run_g10_v3f2_emotion_event_md.py         # G10: F2 強情緒 markdown
├── run_g11_v3f3_drift_guard_candidate.py    # G11: F3 Drift Guard 候選
├── run_g12_v3f5_mood_diary.py               # G12: F5 Mood Diary + Daily Journal
├── run_g13_v3f6_preference_markdown.py      # G13: F6 Preference markdown
├── run_g14_v3f7_decision_trace.py           # G14: F7 Decision Trace markdown
└── run_g15_final_audit_full_prompt_dump.py  # G15: 清點 00-99 + dump 完整 prompt
```

## 跑法

```powershell
# 跑單一 test
cd Z:\Cursor練習用\Agent_Memory\agent-memory-core
python -X utf8 test/v3g_pressure_tests/run_g01_memory_ctx_budget.py

# 跑全部 (按順序)
for f in test/v3g_pressure_tests/run_g*.py; do python -X utf8 $f; done
```

## LOG 規範

每個 test 寫到 `logs/<test_name>.log`, 含:
- timestamp
- test step + 期望值 + 實際值
- ✅ PASS / ❌ FAIL
- exit code 0 = PASS, 1 = FAIL

## 規範

- ✅ test 失敗 → 改程式碼 → 再跑, 不要改 test 標準
- ✅ test 加新 case 不刪舊 case (regression guard)
- ✅ test 跑完 sanity 雙綠 (V3 stress 127/127 + e2e 177/177) 才 commit
- ✅ commit message 列出 pressure test 結果
