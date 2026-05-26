# -*- coding: utf-8 -*-
"""V3-E6: dump 完整 system prompt 給 user 看 (含新 G+ section 綜合應用 framing)

對齊 user 2026-05-27 拍板「展開更詳細的」.

跑法:
  cd Z:\\Cursor練習用\\Agent_Memory\\agent-memory-core
  python -X utf8 scripts/dump_v3e6_system_prompt.py
"""
import sys
from pathlib import Path

# Ensure repo path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt

VAULT = Path(r"Z:/Cursor練習用/Agent_Memory/test/SecondBrains/companion_test")

# 模擬一個 prompt_packet (對應 mid-chat 第 5 turn, user sadness, intim 0.8 owner)
prompt_packet = {
    "affect": {"valence": -0.4, "arousal": 0.55},
    "emotion": {"dominant_emotion": "sadness"},
    "balance": {"balance_axis": -0.15},
    "policy": {
        "strategy": "calm_clear",
        "tone": "soft_warm",
        "intimacy_score": 0.80,
        "is_owner": True,
    },
    "decision": "ALLOW_OWNER_DIRECTIVE",
    "memory_context": "[mid-term] 主人最近在做 V3 開發 / 喜歡用比喻溝通\n[episodic] 上次說過「想要更貼心的回應」",
    "system_persona": "companion baseline",
}

print("=" * 80)
print("V3-E6 完整 SYSTEM PROMPT (動態讀 vault + 新 G+ section 綜合應用 framing)")
print("vault_root =", VAULT)
print("=" * 80)
print()

system_prompt = _build_companion_system_prompt(prompt_packet, vault_root=VAULT)

print(system_prompt)
print()
print("=" * 80)
print(f"[STATS] 總長度 = {len(system_prompt)} chars / 約 {len(system_prompt) // 3} tokens")
print("=" * 80)
