# -*- coding: utf-8 -*-
"""V3-E6/E7/E9: dump 完整 system prompt 給 user 看 (owner + viewer 兩個 scenario)

V3-E6: G+ section 綜合應用 framing
V3-E7: section E 數字 → 主觀感受句翻譯 (_humanize_affect)
V3-E9 (E5+6): section D' 對 non-owner 加觀眾個別記憶塊

跑法:
  cd Z:\\Cursor練習用\\Agent_Memory\\agent-memory-core
  python -X utf8 scripts/dump_v3e6_system_prompt.py
"""
import sys
from pathlib import Path

# Ensure repo path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_memory.companion.companion_chat_runtime import (
    _build_companion_system_prompt,
    _load_viewer_dynamic_context,
)

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
print("【1】OWNER turn — V3-E6/E7 完整 SYSTEM PROMPT (含 section C 00.08 主人 profile)")
print("vault_root =", VAULT)
print("=" * 80)
print()

system_prompt_owner = _build_companion_system_prompt(prompt_packet, vault_root=VAULT)

print(system_prompt_owner)
print()
print("=" * 80)
print(f"[STATS owner] 總長度 = {len(system_prompt_owner)} chars / 約 {len(system_prompt_owner) // 3} tokens")
print("=" * 80)

# ─── V3-E9 (E5+6) viewer scenario ───
print()
print("=" * 80)
print("【2】VIEWER turn — V3-E9 (E5+6) 完整 SYSTEM PROMPT (section D' 取代 C, 對該 viewer 個別記憶)")
print("=" * 80)
print()

# 模擬一個 viewer turn (生氣的觀眾刷頻, intim 0.15 初識, balance>0 想戳)
viewer_packet = {
    "affect": {"valence": 0.05, "arousal": 0.65, "dominance": 0.55, "uncertainty": 0.25},
    "emotion": {
        "joy": 0.30, "sadness": 0.10, "anger": 0.20, "fear": 0.10,
        "love": 0.05, "disgust": 0.15, "desire": 0.20,
        "dominant_emotion": "joy",
    },
    "balance": {
        "balance_axis": 0.30,
        "playfulness": 0.50, "mischief": 0.40, "whimsy": 0.20, "impulsivity": 0.35,
        "silence_intolerance": 0.30, "curiosity_urge": 0.50,
        "topic_drive": 0.45, "engagement_seeking": 0.55,
        "inhibition_level": 0.70,
    },
    "policy": {
        "strategy": "playful_brief",
        "tone": "casual_polite",
        "intimacy_score": 0.15,
        "is_owner": False,
    },
    "decision": "ALLOW_PLAYFUL",
    "memory_context": "",
    "system_persona": "companion baseline",
}

# 真的撈 vault DB 看實際 viewer (如果有) — fallback synthetic context
real_viewer_ctx = _load_viewer_dynamic_context(VAULT, "1502621329663332432")  # 冬蜜核心-3 AI 觀眾 (.env.test.local 白名單)
if not real_viewer_ctx:
    # Synthetic sample
    real_viewer_ctx = (
        "- 觀眾: TestViewer (id=test_viewer_001)\n"
        "- 等級: casual / 親密度: stranger (0.15) / 互動次數: 3\n"
        "- 我學到他的偏好:\n"
        "  - 喜歡: 直接聊遊戲, 不喜歡長篇大論\n"
        "- 跟他過去說過 (近 5 pair, 由舊→新):\n"
        "  - [2026-05-25 14:30] 他: 你好啊\n"
        "  - [2026-05-25 14:31] 我: 你好, 你今天有空一起聊嗎\n"
        "  - [2026-05-26 16:42] 他: 嘿主播你在幹嘛\n"
        "  - [2026-05-26 16:43] 我: 我在跟主人聊天呀\n"
        "  - [2026-05-27 03:15] 他: 你也太愛主人了吧\n"
        "- ⚠️ intim 很低, 不要太自來熟, 保持禮貌距離 (對齊 V3 §27.2 防裝熟紅線)"
    )

system_prompt_viewer = _build_companion_system_prompt(
    viewer_packet, vault_root=VAULT, viewer_profile_context=real_viewer_ctx,
)

print(system_prompt_viewer)
print()
print("=" * 80)
print(f"[STATS viewer] 總長度 = {len(system_prompt_viewer)} chars / 約 {len(system_prompt_viewer) // 3} tokens")
print("=" * 80)
