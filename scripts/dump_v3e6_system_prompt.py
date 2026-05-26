# -*- coding: utf-8 -*-
"""V3-E6/E7: dump 完整 system prompt 給 user 看

V3-E6: G+ section 綜合應用 framing
V3-E7: section E 數字 → 主觀感受句翻譯 (_humanize_affect)

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

# 模擬一個 prompt_packet (mid-chat 第 5 turn, user 難過 + 想搞笑掩飾, intim 0.8 owner)
# V3-E7 sample: 完整 emotion + balance 8 子軸 → 看 _humanize_affect 多軸組合效果
prompt_packet = {
    "affect": {"valence": -0.4, "arousal": 0.55, "dominance": 0.45, "uncertainty": 0.55},
    "emotion": {
        "joy": 0.25, "sadness": 0.62, "anger": 0.15, "fear": 0.30,
        "love": 0.40, "disgust": 0.05, "desire": 0.10,
        "dominant_emotion": "sadness",
    },
    "balance": {
        "balance_axis": -0.15,
        "playfulness": 0.55, "mischief": 0.20, "whimsy": 0.30, "impulsivity": 0.25,
        "silence_intolerance": 0.55, "curiosity_urge": 0.40,
        "topic_drive": 0.65, "engagement_seeking": 0.50,
        "inhibition_level": 0.85,
    },
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
