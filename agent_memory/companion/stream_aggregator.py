"""V3-O.11 — 統一回話動態彙整 (stream_aggregator).

不分日常/直播: 所有 viewer 訊息進佇列, debounce 滑動視窗彙整統一發言。
owner 豁免 (即時獨立回, 不進此佇列)。

觸發 (先到先發, per channel):
  ① 安靜滿 quiet_window_s (每來新訊息 reset) 且佇列非空
  ② 有效句達 meaningful_flush_threshold (動態 5-10; 程式快篩短句/表情不算)
  ③ 從第一條起滿 hard_cap_s 硬上限

V3-O.11 多頻道: per channel_id 分桶 (drain/should_flush 各頻道獨立), 支援多頻道並行。
階段2 接入: transport viewer record_only + add_message; relay 背景 task 輪詢 should_flush → 彙整發頻道。
"""

from __future__ import annotations

import bisect
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[a-zA-Z]")


def is_meaningful_message(content: str) -> bool:
    """程式快篩 (V3-O.11 user②): 短句/純表情/純數字/無意義 → False (累積但不觸發 flush).

    判準: 去空白後非純數字, 且實質字元 (中文 + 英文字母) >= 2。
    例: "666"→F, "哈"→F, "😂😂"→F, "你好"→T, "你好可愛"→T, "ok"→T。
    """
    s = (content or "").strip()
    if not s:
        return False
    if s.isdigit():
        return False
    cjk = len(_CJK_RE.findall(s))
    latin = len(_LATIN_RE.findall(s))
    return (cjk + latin) >= 2


@dataclass
class _PendingMessage:
    user_id: str
    display_name: str
    content: str
    timestamp: float           # bridge 收到時間 (monotonic), 給 debounce window 計算用
    meaningful: bool
    # V3-O.13 #ORD: 來源平台時間戳 (epoch sec). discord = message.created_at, YT = publishedAt.
    # add_message 用這個 insort 排, drain 出來自然按真實時序排.
    # 0.0 = 來源沒給 → 退化用 timestamp (current monotonic, append 順序).
    source_ts: float = 0.0


class StreamAggregator:
    """收集 viewer 訊息 (per channel 分桶), debounce 滑動視窗觸發彙整統一回覆。"""

    def __init__(
        self,
        vault_root: Path,
        *,
        quiet_window_s: float = 7.0,    # V3-O.11+ user 2026-06-02: 6→7 給連發更多空間
        meaningful_flush_threshold: int = 5,
        max_meaningful: int = 10,
        hard_cap_s: float = 28.0,       # V3-O.11+ user 2026-06-02: 30→28 控制體感上限
    ):
        self.vault_root = vault_root
        self.quiet_window_s = quiet_window_s
        self.meaningful_flush_threshold = meaningful_flush_threshold
        self.max_meaningful = max_meaningful
        self.hard_cap_s = hard_cap_s
        # V3-O.11 多頻道: channel_id → list[_PendingMessage]
        self._pending: dict[str, list] = {}
        self._lock = threading.Lock()

    def add_message(
        self,
        channel_id: str,
        user_id: str,
        display_name: str,
        content: str,
        *,
        source_ts: float = 0.0,
    ) -> None:
        """加一則 viewer 訊息進該頻道佇列 (附程式快篩 meaningful 標記).

        V3-O.13 #ORD (2026-06-04 user): 新增 source_ts (來源平台時間戳, discord
        message.created_at / YT publishedAt 等). 用 bisect.insort 按 source_ts 插入,
        drain 出來自然 sorted → 修 server→bridge 路徑 reorder. source_ts=0 退化用
        bridge 收到時間 (append 順序, backward compat).
        """
        ch = str(channel_id or "default")
        now_mono = time.monotonic()
        # 來源 ts 缺 → 用 bridge 收到時間, 退化成 append 順序 (跟舊行為一致)
        sort_key = float(source_ts) if source_ts > 0 else now_mono
        new_msg = _PendingMessage(
            user_id=user_id,
            display_name=display_name or user_id,
            content=content,
            timestamp=now_mono,
            meaningful=is_meaningful_message(content),
            source_ts=sort_key,
        )
        with self._lock:
            bucket = self._pending.setdefault(ch, [])
            # bisect.insort by source_ts (穩定維持 sorted) → drain 出來照真實時序
            bisect.insort(bucket, new_msg, key=lambda m: m.source_ts)

    def pending_count(self, channel_id: str) -> int:
        with self._lock:
            return len(self._pending.get(str(channel_id or "default"), []))

    def meaningful_count(self, channel_id: str) -> int:
        with self._lock:
            return sum(1 for m in self._pending.get(str(channel_id or "default"), []) if m.meaningful)

    def pending_channels(self) -> list:
        """回傳目前有 pending 的 channel_id 清單 (供背景輪詢)。"""
        with self._lock:
            return [c for c, msgs in self._pending.items() if msgs]

    def should_flush(self, channel_id: str) -> bool:
        """debounce 滑動視窗 (該頻道): 30s硬上限 / 有效句達門檻 / 安靜6s, 先到先發。"""
        ch = str(channel_id or "default")
        with self._lock:
            pend = self._pending.get(ch, [])
            if not pend:
                return False
            now = time.monotonic()
            first_ts = pend[0].timestamp
            last_ts = pend[-1].timestamp
            meaningful = sum(1 for m in pend if m.meaningful)
            # ① 硬上限: 從第一條起 hard_cap_s (連續刷也強制發)
            if (now - first_ts) >= self.hard_cap_s:
                return True
            # ② 有效句達門檻 (動態 5-10) → 不等安靜先發, 讓直播能持續接話
            if meaningful >= self.meaningful_flush_threshold:
                return True
            # ③ debounce: 安靜滿 quiet_window_s 且佇列非空 → flush (含單人/純表情批)
            if (now - last_ts) >= self.quiet_window_s and len(pend) >= 1:
                return True
            return False

    def drain(self, channel_id: str) -> list:
        """取出並清空該頻道 pending (給逐一序列個別處理 + 彙整用)。"""
        ch = str(channel_id or "default")
        with self._lock:
            msgs = self._pending.pop(ch, [])
            return msgs


# ── 全域 aggregator registry (per vault_root) ──────────────────────────────
_AGGREGATOR_REGISTRY: dict = {}
_REGISTRY_LOCK = threading.Lock()


def get_stream_aggregator(
    vault_root: Path,
    *,
    quiet_window_s: float = 6.0,
    meaningful_flush_threshold: int = 5,
    max_meaningful: int = 10,
    hard_cap_s: float = 30.0,
) -> StreamAggregator:
    """取得或建立對應 vault 的 StreamAggregator (單例 per vault, 內部 per channel 分桶)。"""
    key = str(vault_root)
    with _REGISTRY_LOCK:
        agg = _AGGREGATOR_REGISTRY.get(key)
        if agg is None:
            agg = StreamAggregator(
                vault_root,
                quiet_window_s=quiet_window_s,
                meaningful_flush_threshold=meaningful_flush_threshold,
                max_meaningful=max_meaningful,
                hard_cap_s=hard_cap_s,
            )
            _AGGREGATOR_REGISTRY[key] = agg
        return agg
