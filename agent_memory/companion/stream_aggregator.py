"""V3-O.10 #41 — 直播場景吸收彙整統一發言 (stream_aggregator).

情境: 直播聊天室不能每 viewer 都回, bot 應收集一段時間的訊息 → 彙整 → 主動發言.

流程:
  viewer A/B/C 連續發言 (aggregate_window_s 內)
  → StreamAggregator 收集
  → local_gemma/openrouter_sub 彙整 sentiment + topic
  → bot 主動統一回覆

觸發條件:
  - aggregate_window_s 內 viewer 訊息 ≥ min_messages 個
  - OR 同 viewer 連發 ≥ min_burst 句

yaml 配置:
  channels.discord.stream_mode.aggregate_window_s: 8
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class _PendingMessage:
    user_id: str
    display_name: str
    content: str
    timestamp: float


class StreamAggregator:
    """收集直播 viewer 訊息，觸發彙整發言."""

    def __init__(
        self,
        vault_root: Path,
        *,
        aggregate_window_s: float = 8.0,
        min_messages: int = 3,
        min_burst: int = 3,
    ):
        self.vault_root = vault_root
        self.aggregate_window_s = aggregate_window_s
        self.min_messages = min_messages
        self.min_burst = min_burst
        self._pending: list[_PendingMessage] = []
        self._lock = threading.Lock()
        self._last_flush: float = 0.0

    def add_message(self, user_id: str, display_name: str, content: str) -> None:
        """加入一則 viewer 訊息."""
        with self._lock:
            self._pending.append(_PendingMessage(
                user_id=user_id,
                display_name=display_name or user_id,
                content=content,
                timestamp=time.monotonic(),
            ))

    def should_flush(self) -> bool:
        """判斷是否應該觸發彙整發言.

        條件: pending ≥ min_messages AND 最早訊息已超過 aggregate_window_s (window 到期)
        OR pending ≥ min_messages * 2 (爆量立刻發)
        """
        with self._lock:
            if len(self._pending) < self.min_messages:
                return False
            now = time.monotonic()
            oldest = self._pending[0].timestamp
            window_elapsed = (now - oldest) >= self.aggregate_window_s
            burst = len(self._pending) >= self.min_messages * 2
            return window_elapsed or burst

    def flush_and_generate(self) -> Optional[str]:
        """彙整訊息並用 LLM 生成統一回覆，清空 pending."""
        with self._lock:
            if not self._pending:
                return None
            messages = list(self._pending)
            self._pending.clear()
            self._last_flush = time.monotonic()

        if not messages:
            return None

        # 組裝彙整 prompt
        lines = [f"{m.display_name}: {m.content[:100]}" for m in messages[:10]]
        block = "\n".join(lines)
        names = list({m.display_name for m in messages})[:5]
        names_str = "、".join(names)

        prompt = (
            f"你是一個 VTuber 陪伴 bot，剛剛在直播聊天室中收到以下多位觀眾的訊息：\n\n"
            f"{block}\n\n"
            f"請用 1-2 句話統一回應這些訊息（提及主要發言者 {names_str}），"
            f"語氣活潑自然，不要逐一點名所有人。\n"
            f"直接輸出回覆句子，不需要說明。"
        )

        try:
            from agent_memory.llm_text_helpers import call_llm_for_text
            result = call_llm_for_text(
                self.vault_root, prompt,
                persona_id="companion",
                temperature=0.7,
                timeout_s=15.0,
                auxiliary="umbrella_consolidation",
            )
            return result.strip() if result else None
        except Exception:
            return None


# ── 全域 aggregator registry (per vault_root) ──────────────────────────────
_AGGREGATOR_REGISTRY: dict[str, StreamAggregator] = {}
_REGISTRY_LOCK = threading.Lock()


def get_stream_aggregator(
    vault_root: Path,
    *,
    aggregate_window_s: float = 8.0,
) -> StreamAggregator:
    """取得或建立對應 vault 的 StreamAggregator."""
    key = str(vault_root)
    with _REGISTRY_LOCK:
        if key not in _AGGREGATOR_REGISTRY:
            _AGGREGATOR_REGISTRY[key] = StreamAggregator(
                vault_root,
                aggregate_window_s=aggregate_window_s,
            )
        return _AGGREGATOR_REGISTRY[key]
