# V3-O.15.4 message.id Dedup — Cross-Platform 設計記錄

**拍板日期**: 2026-06-06 user
**狀態**: Discord Layer 1 已實作; YT/LINE 預留接口

---

## 問題

discord.py 在 gateway 重連 backfill / cache desync / 某些 reaction event 觸發時, 對同一 Discord message 物件 fire `on_message` 兩次. 結果:

```
trace 67489648  22:05:02  total=141ms   ← record_only #1 (個別記錄)
trace 22d48d47  22:05:03  total=139ms   ← record_only #2 (同 message 再來一次!)
trace c5d3038e  22:05:18  total=7694ms  ← flush full LLM
```

同 user message 被處理 2 次, raw_events 雙寫.

## 修法 — message.id LRU dedup

**Layer 1 (Discord relay)** 已實作於 `scripts/discord_bridge_relay.py`:

```python
# __init__:
from collections import OrderedDict
self._seen_message_ids: OrderedDict = OrderedDict()
self._SEEN_MSG_IDS_CAP: int = 2000

# on_message 開頭:
_msg_id = int(getattr(message, "id", 0) or 0)
if _msg_id:
    if _msg_id in self._seen_message_ids:
        return  # library 重 fire, 靜默 skip
    self._seen_message_ids[_msg_id] = True
    while len(self._seen_message_ids) > self._SEEN_MSG_IDS_CAP:
        self._seen_message_ids.popitem(last=False)
```

## 為什麼不會誤殺真連發

| 情境 | message_id | 結果 |
|---|---|---|
| user 連發 3 句同內容 | 1001, 1002, 1003 (3 個 unique) | ✓ 全過 |
| user edit 訊息 1001 | 1001 (同 ID, on_message 重 fire) | ✗ 擋 |
| discord lib 重連 backfill | 1003 (重複) | ✗ 擋 ⭐ |
| 路人重發 (你叫他再說) | 2000 (新 ID) | ✓ 過 |
| 跨 channel 同 content | 各自 unique | ✓ 全過 |

每條真訊息 Discord 給的 message_id 都不同 (snowflake 64-bit), **物理上不可能撞**.

---

## 跨平台 — YT/LINE 接口預留

**設計原則**: 各平台都用各自原生的「message_id」當 dedup key. 各平台 message_id 在各自平台都全域 unique.

### YouTube Live Chat (接口預留)

YouTube Live Chat 用 `LiveChatMessage.id` 識別每條訊息. 該 ID 在整個 YouTube 範圍內 unique.

**未來 youtube_bridge_relay.py 實作 pattern**:
```python
# __init__:
self._seen_yt_message_ids: OrderedDict = OrderedDict()
self._SEEN_YT_CAP: int = 2000

# poll callback / message handler 開頭:
yt_msg_id = str(message.get("id", "") or "")
if yt_msg_id and yt_msg_id in self._seen_yt_message_ids:
    return
if yt_msg_id:
    self._seen_yt_message_ids[yt_msg_id] = True
    while len(self._seen_yt_message_ids) > self._SEEN_YT_CAP:
        self._seen_yt_message_ids.popitem(last=False)
```

YouTube data API 用 poll (`liveChatMessages.list`) 不會被 retry 重 fire, 但用戶端如果 reconnect 可能 backfill 同訊息. 也加 dedup 一勞永逸.

### LINE Messaging API (接口預留)

LINE webhook 用 `event.message.id` 識別. LINE 偶爾 HTTP 失敗 retry 同 webhook payload.

**未來 line_bridge_relay.py 實作 pattern**:
```python
# __init__:
self._seen_line_message_ids: OrderedDict = OrderedDict()
self._SEEN_LINE_CAP: int = 2000

# webhook handler 開頭:
line_msg_id = str(event.get("message", {}).get("id", "") or "")
if line_msg_id and line_msg_id in self._seen_line_message_ids:
    return
if line_msg_id:
    self._seen_line_message_ids[line_msg_id] = True
    while len(self._seen_line_message_ids) > self._SEEN_LINE_CAP:
        self._seen_line_message_ids.popitem(last=False)
```

LINE webhook 重 retry **必擋** (不然付了費的事件雙處理).

### Twitch IRC (接口預留)

Twitch IRC 訊息有 `msg-id` tag (來自 IRCv3 message tag). IRC stream 連線狀態好時不會重發, 但連線中斷重連時可能重收最近 N 條.

**未來 twitch_bridge_relay.py 實作 pattern**: 同上, key 換成 IRC `msg-id` tag.

---

## bridge 端可選加 Layer 2 (未來)

如果未來想要二層保險, `transport_ingest.py` 可加 source-platform-aware dedup:

```python
# payload 各 relay 帶:
"source_message_id": str(message.id),
"source_platform": "discord",  # / "youtube" / "line" / "twitch"

# bridge 端:
_GLOBAL_DEDUP_LRU: OrderedDict = OrderedDict()
key = f"{source_platform}:{source_message_id}"
if key in _GLOBAL_DEDUP_LRU:
    return {"deduplicated": True, ...}
_GLOBAL_DEDUP_LRU[key] = True
```

**現階段不做** (user 2026-06-06 拍板 Layer 1 only). 接 YT 時若 Layer 1 證明夠用, 可繼續不做; 若發現需要可改加 Layer 2.

---

## 檔案改動 (Layer 1 only)

- `scripts/discord_bridge_relay.py`:
  - `__init__`: 加 `self._seen_message_ids: OrderedDict` + `self._SEEN_MSG_IDS_CAP = 2000`
  - `on_message`: 開頭 LRU check + insert + evict
- `docs/V3-O.15.4_message_id_dedup_cross_platform.md`: 本檔 (跨平台設計記錄)

不動 bridge / chat_runtime / DB schema.
