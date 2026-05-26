"""V3 C18h Daydream Engine — §29.3 H3 白日夢 / 心智模擬.

對齊 V3 §29.3 + dead_chat_mode 主舞台 (D-V3-45).

idle ≥ X 秒或 dead_chat_mode → background thread 跑 mini chat simulation:
- 預想下個話題
- 回顧 highlight
- 模擬「如果 viewer-A 等下說 X 我會怎麼回」

Phase 2 MVP: 純 template / rule, Phase 3 可換 mini LLM call.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db


# 內部 mini-simulation 模板
_DAYDREAM_TEMPLATES = (
    "想到剛才 viewer 提到的 {topic}, 我可以多展開 {detail}",
    "如果有人問 {topic}, 我會說 {detail}",
    "回顧 turn N {topic}, 那個反應其實可以 {detail}",
    "突然想到 {topic} 跟之前的 {detail} 有關",
)


@dataclass(slots=True)
class DaydreamResult:
    daydream_text: str = ""  # 內部 simulation 紀錄 (不一定外顯)
    externally_visible: bool = False  # dead_chat_mode 才 True
    candidate_for_next_topic: str = ""
    elapsed_idle_seconds: int = 0


def generate_daydream(
    *,
    idle_seconds: int,
    recent_topics: Optional[list[str]] = None,
    knowledge_gap_entities: Optional[list[str]] = None,
    flow_mode: str = "normal_mode",
    rng: Optional[random.Random] = None,
) -> DaydreamResult:
    """V3 §29.3: 生成 daydream content + 是否外顯判定."""
    rng = rng or random.Random()
    recent_topics = recent_topics or []
    knowledge_gap_entities = knowledge_gap_entities or []

    if idle_seconds < 30:
        # 太短不算 idle
        return DaydreamResult(elapsed_idle_seconds=idle_seconds)

    # 選 topic
    topic = ""
    detail = ""
    if knowledge_gap_entities:
        topic = knowledge_gap_entities[0]
        detail = "我還沒查清楚"
    elif recent_topics:
        topic = recent_topics[0]
        detail = "再看一下"
    else:
        topic = "等下要說什麼"
        detail = "預想一下"

    template = rng.choice(_DAYDREAM_TEMPLATES)
    text = template.format(topic=topic, detail=detail)

    # 對 dead_chat_mode 外顯 (D-V3-45)
    externally_visible = (flow_mode == "dead_chat_mode")

    return DaydreamResult(
        daydream_text=text,
        externally_visible=externally_visible,
        candidate_for_next_topic=topic,
        elapsed_idle_seconds=idle_seconds,
    )


def maybe_emit_daydream(response_text: str, daydream: DaydreamResult) -> str:
    """V3 §29.3: dead_chat_mode 把 daydream 外顯給觀眾."""
    if not daydream.externally_visible or not daydream.daydream_text:
        return response_text
    return f"(自言自語: {daydream.daydream_text})\n{response_text}"
