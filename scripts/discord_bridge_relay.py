from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time as _relay_time  # V3-O.9 #2: end-to-end timing
from concurrent.futures import ThreadPoolExecutor  # V3-O.13.2: 隔離 flush_check executor
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

import discord
from discord.ext import tasks


def _load_channels_from_vault_config(vault_root_str: str) -> dict:
    """V3-O.12 #F-channel: 從 vault companion_config.yaml channels.discord 補 channel sets.

    讓 relay 不必硬塞 --channel-id, user 改 vault config 就能切換頻道.
    優先序: channel_ids (多頻道清單) > channel_id_env (向後相容單頻道 env name) -> os.environ.
    mention_only_channel_ids 同步讀.
    錯誤/缺檔/缺 yaml -> 空 dict + 警告, 不阻擋 relay 啟動.
    (V3-O.13.5: allow_bot_author_ids 路徑已淨化, 不再讀.)
    """
    out: dict = {"channel_ids": set(), "mention_only": set()}
    if not vault_root_str:
        return out
    try:
        import yaml as _yaml
        from pathlib import Path as _Path
    except Exception as e:
        print(f"[WARN] config-fallback skipped (yaml/pathlib import fail): {e}")
        return out
    cfg_path = _Path(vault_root_str).expanduser().resolve() / "00_System_Core" / "companion_config.yaml"
    if not cfg_path.exists():
        return out
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            cfg = _yaml.safe_load(fh) or {}
        dc = ((cfg.get("channels") or {}).get("discord") or {})
        ids = dc.get("channel_ids") or []
        if ids:
            for x in ids:
                try:
                    out["channel_ids"].add(int(str(x).strip()))
                except (ValueError, TypeError):
                    pass
        else:
            env_name = (dc.get("channel_id_env") or "").strip()
            if env_name:
                raw = os.getenv(env_name, "").strip()
                if raw:
                    try:
                        out["channel_ids"].add(int(raw))
                    except ValueError:
                        pass
        for x in (dc.get("mention_only_channel_ids") or []):
            try:
                out["mention_only"].add(int(str(x).strip()))
            except (ValueError, TypeError):
                pass
    except Exception as e:
        print(f"[WARN] config-fallback parse failed: {e}")
    return out


# V3-O.6.2 #9 (user 2026-05-28): bridge timeout / HTTP error 友善訊息池.
# 取代原本「bridge call failed: timed out」/「bridge HTTP error: 500」這類技術字串
# 直接貼進聊天室 (對齊 user 觀察「不可以讓他說這句話出來」+ V3-O.2 timeout user msg
# 風格「等等，我沒聽懂，你再說一次」一致).
# 技術原文 (exc) 仍寫 stderr 給 ops 看 — 不是吞錯, 只是不對 chat user 暴露.
_FRIENDLY_TIMEOUT_POOL = (
    "等等，我這邊訊號好像卡到了，你再說一次給我聽？",
    "啊我剛剛沒接到，能再說一遍嗎？",
    "嗯…我這邊有點忙，等等再說一次好嗎？",
    "稍等我一下，剛剛訊號斷了，再講一次給我聽？",
)
_FRIENDLY_HTTP_POOL = (
    "等等，我這邊好像有點怪怪的，你再說一次給我聽？",
    "嗯…剛剛沒接好，能再說一遍嗎？",
    "稍等我一下，再說一次好嗎？",
)
# V3-O.10 #5 Q8: viewer drop 友善訊息 (DC 通道, 5s cooldown 觸發)
_FRIENDLY_DROP_POOL = (
    "等等，大家說太快了，慢一點給我聽？",
    "哈哈稍等我一下，剛剛沒跟上！",
    "嗯…太多訊息了，再說一次好嗎？",
    "稍等，讓我緩一下，再說一遍？",
)


def _friendly_timeout_reply() -> str:
    return random.choice(_FRIENDLY_TIMEOUT_POOL)


def _friendly_http_reply() -> str:
    return random.choice(_FRIENDLY_HTTP_POOL)


def _post_json(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = url_request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with url_request.urlopen(req, timeout=timeout_s) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise RuntimeError(f"bridge payload type invalid: {type(data).__name__}")
    return data


def _pick_response_reaction(response_text: str, *, degraded: bool) -> str:
    if degraded:
        return "\u26A0\uFE0F"  # warning

    text = response_text.strip()
    lowered = text.lower()
    if "?" in text or "？" in text:
        return "\U0001F914"  # thinking face
    if any(token in text for token in ("需要", "請提供", "澄清", "不確定", "無法判斷")):
        return "\U0001F914"
    if any(token in lowered for token in ("error", "failed", "missing", "invalid")):
        return "\u26A0\uFE0F"
    if any(token in text for token in ("完成", "成功", "已處理", "可以", "沒問題", "OK", "ok")):
        return "\u2705"  # white heavy check mark
    return "\U0001F4AC"  # speech balloon


class BridgeRelayClient(discord.Client):
    def __init__(
        self,
        *,
        bridge_url: str,
        allowed_channels: set[int],
        persona: str,
        mode: str,
        allow_llm_degraded: bool,
        timeout_s: float,
        read_reaction: str,
        processing_reaction: str,
        enable_read_reaction: bool,
        enable_processing_reaction: bool,
        enable_response_reaction: bool,
        enable_message_content_intent: bool,
        mention_only_channels: set[int],
        vault_root: "Path | None" = None,
    ) -> None:
        from pathlib import Path as _Path
        intents = discord.Intents.default()
        intents.message_content = bool(enable_message_content_intent)
        intents.messages = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.vault_root = _Path(vault_root).expanduser().resolve() if vault_root else None
        self.bridge_url = bridge_url.rstrip("/")
        self.allowed_channels = allowed_channels
        self.persona = persona.strip().lower()
        self.mode = mode
        self.allow_llm_degraded = allow_llm_degraded
        self.timeout_s = timeout_s
        self.read_reaction = read_reaction
        self.processing_reaction = processing_reaction
        self.enable_read_reaction = enable_read_reaction
        self.enable_processing_reaction = enable_processing_reaction
        self.enable_response_reaction = enable_response_reaction
        self.enable_message_content_intent = bool(enable_message_content_intent)
        self.mention_only_channels = set(mention_only_channels)
        # V3-O.13.5 (2026-06-04 user 拍板「測試/正式都不用」): 全淨化 AI viewer pool 模擬路徑.
        # 原 V3-D7 bot author whitelist + V3-O.6 #5 split-by-display-name 已移除 — 真人/真 bot
        # 各有自己的 Discord author.id, 不再需要 prefix hack 解 ID 衝突. 對齊 V3-O.6 #5 當時拍板
        # 「下次以 DC ID」收尾. 朋友卡只收真實 Discord 用戶的真實互動, 不再被 AI 模擬污染.
        # V3-O.11 階段4: viewer 訊息被 bridge held (進彙整佇列, 不個別回) 時,
        # 記下該頻道; 背景 flush loop 定期問 bridge 是否該發統一回覆。
        #   key=channel_id(int) → value=channel_type(str, "dm"/"public_text_channel")
        self._pending_channels: dict[int, str] = {}
        self._pending_lock = asyncio.Lock()
        # V3-O.13.2 relay 健壯性升級 (對齊 doc §5 優先 2):
        # (a) 拆獨立 executor — flush_check 不再跟主 reply HTTP 搶 default executor thread.
        # (b) per-channel circuit breaker — 連續 N 次失敗後 exp backoff cooldown, 避免雪崩.
        # (c) in-flight 防重複 dispatch — 上輪還沒回來的 channel 不再 fire 新 task.
        self._flush_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="bridge_flush",
        )
        # flush_check 用較短 timeout (60s) — 它只是 should_flush 詢問, 不該跟主 LLM 240s 平起平坐.
        self._flush_check_timeout_s: float = 60.0
        # 連續失敗 ≥3 次才開始 exp backoff. backoff = base * 2^(fail - threshold), cap @ max.
        self._FLUSH_FAIL_THRESHOLD: int = 3
        self._FLUSH_BACKOFF_BASE_S: float = 5.0
        self._FLUSH_BACKOFF_MAX_S: float = 60.0
        self._flush_failure_state: dict[int, dict[str, float]] = {}
        # 上輪 task 還在跑的 channel: 避免本輪重複 dispatch 把 executor 占爆.
        self._flush_in_flight: set[int] = set()
        # ⭐ V3-O.15.4 (2026-06-06 user 拍板): Discord message.id LRU dedup.
        # 防 discord.py library 邊緣 case (gateway 重連 backfill / cache desync / reaction event 觸發
        # on_message refresh) 對同一 Discord message 物件 fire on_message 2 次.
        # user 連發內容相同句 = 不同 message.id (Discord snowflake unique 64-bit), 不會誤殺.
        # 跨平台預留: 未來 YT/LINE relay 各自加同款 (各平台 message_id 各自全域 unique).
        # 詳見 docs/V3-O.15.4_message_id_dedup_cross_platform.md (YT 接口設計記錄).
        from collections import OrderedDict as _OD
        self._seen_message_ids: _OD = _OD()
        self._SEEN_MSG_IDS_CAP: int = 2000

    async def _post_streaming(self, loop, url: str, payload: dict, source_message) -> dict:
        """V3-O.10 #26: SSE streaming — 收 token，定期用 message.edit() 更新 Discord."""
        import json as _json_s
        from urllib import request as _url_req_s, error as _url_err_s
        import asyncio as _asyncio_s

        token_buf: list[str] = []
        result_holder: list[dict] = [{}]
        EDIT_INTERVAL_S = 0.8  # Discord rate limit: 每 0.8s edit 一次
        last_edit_time = [0.0]
        reply_msg = [None]

        def _stream_reader():
            body = _json_s.dumps(payload).encode("utf-8")
            req = _url_req_s.Request(url=url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "text/event-stream")
            try:
                with _url_req_s.urlopen(req, timeout=self.timeout_s + 15.0) as resp:
                    for line in resp:
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        line = line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            try:
                                data = _json_s.loads(line[6:])
                                if "token" in data:
                                    token_buf.append(data["token"])
                                if data.get("done"):
                                    result_holder[0] = data
                                    break
                                if "error" in data:
                                    result_holder[0] = {"response": "", "error": data["error"]}
                                    break
                            except Exception:
                                pass
            except Exception as e:
                result_holder[0] = {"response": "", "error": str(e)}

        # 背景跑 SSE reader
        reader_future = loop.run_in_executor(None, _stream_reader)

        # 等第一個 token 或 done
        reply_sent = False
        last_content = ""
        timeout_at = _relay_time.perf_counter() + self.timeout_s + 20.0

        while not reader_future.done() or token_buf:
            await _asyncio_s.sleep(0.2)
            now = _relay_time.perf_counter()
            if now > timeout_at:
                break

            current_text = "".join(token_buf)
            if not current_text:
                continue

            # 發或更新 Discord 訊息
            if not reply_sent:
                try:
                    reply_msg[0] = await source_message.reply(current_text + " ▌", mention_author=False)
                    reply_sent = True
                    last_edit_time[0] = now
                    last_content = current_text
                except Exception:
                    pass
            elif (now - last_edit_time[0]) >= EDIT_INTERVAL_S and current_text != last_content:
                try:
                    display = current_text + (" ▌" if not result_holder[0].get("done") else "")
                    if len(display) > 1800:
                        display = display[:1800] + "..."
                    await reply_msg[0].edit(content=display)
                    last_edit_time[0] = now
                    last_content = current_text
                except Exception:
                    pass

        # 等 reader 確實結束
        try:
            await asyncio.wait_for(reader_future, timeout=5.0)
        except Exception:
            pass

        # 最終更新 (移除 ▌)
        final_text = "".join(token_buf)
        if reply_msg[0] and final_text and final_text != last_content:
            try:
                if len(final_text) > 1800:
                    final_text = final_text[:1800] + "..."
                await reply_msg[0].edit(content=final_text)
            except Exception:
                pass

        # 回傳與非 streaming 相同格式
        r = result_holder[0]
        if not r.get("response") and final_text:
            r["response"] = final_text
        return r

    async def _try_add_reaction(self, target: discord.Message, emoji: str) -> bool:
        try:
            await target.add_reaction(emoji)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _try_remove_reaction(self, target: discord.Message, emoji: str) -> None:
        actor = self.user
        if actor is None:
            return
        try:
            await target.remove_reaction(emoji, actor)
        except Exception:  # noqa: BLE001
            return

    async def on_ready(self) -> None:
        user = self.user.name if self.user else "unknown"
        uid = self.user.id if self.user else "unknown"
        channels = ",".join(str(x) for x in sorted(self.allowed_channels)) or "(all)"
        print(f"[OK] discord relay online: {user} ({uid})")
        print(f"[OK] channel_filter={channels}")
        print(f"[OK] bridge={self.bridge_url}/webhook/discord")
        if not self.enable_message_content_intent:
            print("[WARN] message_content intent disabled; guild text content may be unavailable.")
        # V3-O.11 階段4: bot ready 後啟動彙整 flush 背景 loop (每 ~3s 問 bridge)。
        if not self._aggregator_flush_loop.is_running():
            self._aggregator_flush_loop.start()
            print("[OK] aggregator flush loop started (interval=3s)")

    @tasks.loop(seconds=3.0)
    async def _aggregator_flush_loop(self) -> None:
        """V3-O.11 階段4 → V3-O.13.2 健壯性升級: 定期問 bridge「該頻道彙整佇列是否該 flush」。

        對每個有 pending 的頻道 POST {_aggregator_flush_check, session_id, channel_type,
        channel_id}; bridge 回 aggregated=true 且 response 非空 → channel.send(response)
        (發到頻道, 不 reply 特定訊息)。debounce 時機由 bridge should_flush 判斷,
        relay 只負責定期詢問。channel.send 失敗只記 stderr, 不可 crash loop。

        V3-O.13.2 升級點:
        (a) 拆 _flush_one_channel — 每 channel 在獨立 ThreadPoolExecutor + 獨立 task 跑,
            sequential → fire-and-forget concurrent. 一個慢 channel 不再卡其他.
        (b) per-channel circuit breaker — 連續失敗 N 次後 exp backoff 不再 dispatch.
        (c) in-flight 防重複 — 上輪未回的 channel 本輪 skip, 避免 executor 雪崩.
        """
        # snapshot 目前 pending channels (複製出來避免持鎖做 HTTP/send)
        try:
            async with self._pending_lock:
                pending_items = list(self._pending_channels.items())
        except Exception:  # noqa: BLE001
            return
        if not pending_items:
            return

        now = _relay_time.perf_counter()
        for channel_id, channel_type in pending_items:
            # V3-O.13.2 (c): in-flight 跳過 (上輪 task 還沒回來)
            if channel_id in self._flush_in_flight:
                continue
            # V3-O.13.2 (b): circuit breaker — 在 backoff cooldown 內 skip
            st = self._flush_failure_state.get(channel_id)
            if st and st.get("next_allowed_at", 0.0) > now:
                continue
            # fire-and-forget; _flush_one_channel 自己管 in_flight set + failure state
            self._flush_in_flight.add(channel_id)
            asyncio.create_task(self._flush_one_channel(channel_id, channel_type))

    async def _flush_one_channel(self, channel_id: int, channel_type: str) -> None:
        """V3-O.13.2: 單 channel 的 flush_check + send 流程, 跑在 dedicated executor。

        - 用 self._flush_executor (max_workers=4) 隔離主 reply HTTP 的 default executor.
        - 用 self._flush_check_timeout_s (60s) 較短 timeout (vs 主 reply 240s).
        - 成功: reset failure state.
        - 失敗: fail_count++; 連續 ≥ threshold 時觸發 exp backoff (cap 60s).
        - 不論成敗一定從 _flush_in_flight 移除 (finally), 避免 stuck.
        """
        loop = asyncio.get_running_loop()
        payload = {
            "_aggregator_flush_check": True,
            "session_id": f"discord-{channel_id}",
            "channel_type": channel_type,
            "channel_id": str(channel_id),
        }
        url = f"{self.bridge_url}/webhook/discord"
        try:
            try:
                result = await loop.run_in_executor(
                    self._flush_executor, _post_json, url, payload, self._flush_check_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                # V3-O.13.2 (b): 累積失敗 → exp backoff (從 threshold 次後啟動)
                st = self._flush_failure_state.setdefault(
                    channel_id, {"fail_count": 0.0, "next_allowed_at": 0.0},
                )
                st["fail_count"] = float(st.get("fail_count", 0.0)) + 1.0
                fc = int(st["fail_count"])
                if fc >= self._FLUSH_FAIL_THRESHOLD:
                    backoff = min(
                        self._FLUSH_BACKOFF_MAX_S,
                        self._FLUSH_BACKOFF_BASE_S * (2 ** (fc - self._FLUSH_FAIL_THRESHOLD)),
                    )
                    st["next_allowed_at"] = _relay_time.perf_counter() + backoff
                    print(
                        f"[WARN] flush_check chan={channel_id} consecutive fail={fc}, "
                        f"backoff {backoff:.0f}s. {type(exc).__name__}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                else:
                    print(
                        f"[ERR] aggregator flush_check failed chan={channel_id} (fail={fc}): "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                return

            # 成功 → reset failure state (從 cooldown 解除)
            self._flush_failure_state.pop(channel_id, None)

            if not result.get("aggregated"):
                return

            # 彙整達標: bridge 已 drain 全佇列 → 本頻道 pending 已消化。
            async with self._pending_lock:
                self._pending_channels.pop(channel_id, None)

            response_text = str(result.get("response", "")).strip()
            if not response_text:
                return
            if len(response_text) > 1800:
                response_text = response_text[:1800] + "\n...(truncated)"

            # 取得 channel 物件: bridge 回傳的 channel_id 優先, fallback 查詢用的 id。
            target_id = channel_id
            try:
                rid = str(result.get("channel_id", "")).strip()
                if rid:
                    target_id = int(rid)
            except (TypeError, ValueError):
                target_id = channel_id
            channel = self.get_channel(target_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(target_id)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[ERR] aggregator flush channel unavailable id={target_id}: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    return
            try:
                await channel.send(response_text)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[ERR] aggregator flush send failed chan={target_id}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr, flush=True,
                )
        finally:
            # 不論成敗一定釋放 in_flight 槽位
            self._flush_in_flight.discard(channel_id)

    @_aggregator_flush_loop.before_loop
    async def _before_aggregator_flush_loop(self) -> None:
        # 確保 loop 在 bot 完全連線後才開始第一次 tick。
        await self.wait_until_ready()

    async def close(self) -> None:
        # V3-O.11 階段4: 關閉前停掉 flush loop (避免 shutdown warning)。
        try:
            if self._aggregator_flush_loop.is_running():
                self._aggregator_flush_loop.cancel()
        except Exception:  # noqa: BLE001
            pass
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        # V3-O.9 #2: end-to-end timing 起點 (從 Discord 收到 message 算)
        _t_recv = _relay_time.perf_counter()
        # ⭐ V3-O.15.4 (2026-06-06 user 拍板): Discord message.id LRU dedup (擋 lib 重 fire).
        # 真連發 = 不同 message.id, 不會誤殺. 同 message.id fire 2 次 → 第 2 次直接 skip.
        _msg_id = int(getattr(message, "id", 0) or 0)
        if _msg_id:
            if _msg_id in self._seen_message_ids:
                return  # 已處理過, library 重 fire, 靜默 skip
            self._seen_message_ids[_msg_id] = True
            while len(self._seen_message_ids) > self._SEEN_MSG_IDS_CAP:
                self._seen_message_ids.popitem(last=False)
        # V3-O.13.5 (淨化 AI viewer pool 後): bot author handling 簡化為「真人 only」.
        # 1. 自己 (self.user) 一律 ignore (避免無限 loop)
        # 2. 其他任何 bot: 一律 ignore (不再有 AI 模擬觀眾白名單)
        # 3. 真人 (author.bot=False) 才過
        me = self.user
        if me is not None and message.author.id == me.id:
            return
        if message.author.bot:
            return
        if self.allowed_channels and message.channel.id not in self.allowed_channels:
            return
        mention_only_mode = message.channel.id in self.mention_only_channels
        if mention_only_mode:
            me = self.user
            if me is None:
                return
            if me not in message.mentions:
                return
            bot_mentions = [user for user in message.mentions if getattr(user, "bot", False)]
            # In shared channel, only one bot may be tagged per message.
            # If multiple bots are tagged, no relay should reply.
            if len(bot_mentions) != 1 or bot_mentions[0].id != me.id:
                return
        text = (message.content or "").strip()
        me = self.user
        if me is not None and text:
            # Remove explicit bot mention tokens so the model sees clean task text.
            text = re.sub(rf"<@!?{me.id}>", "", text).strip()
        if not text and mention_only_mode:
            text = "請回覆：已收到你的標記，請提供要我處理的任務。"
        if not text:
            return

        if self.enable_read_reaction:
            await self._try_add_reaction(message, self.read_reaction)

        processing_added = False
        if self.enable_processing_reaction:
            processing_added = await self._try_add_reaction(message, self.processing_reaction)

        # Phase A C4 (A.6): 把 Discord attachments 帶進 payload, 讓 bridge 端下載 + extract
        attachments_payload: list[dict[str, Any]] = []
        for att in (message.attachments or []):
            try:
                attachments_payload.append({
                    "url": str(getattr(att, "url", "") or ""),
                    "filename": str(getattr(att, "filename", "") or ""),
                    "content_type": str(getattr(att, "content_type", "") or ""),
                    "size": int(getattr(att, "size", 0) or 0),
                })
            except Exception:  # noqa: BLE001
                continue

        # V3-E1 Bug 2: 帶 guild_id + channel_kind 給 V3 dispatcher 對齊 channel_type
        guild_id = ""
        try:
            if message.guild is not None:
                guild_id = str(message.guild.id)
        except Exception:
            pass

        # V3-O.6 #4: display_name 帶給 bridge — owner turn 自學進 .ai/owner_aliases.json
        # (Discord 真實 display_name). (V3-O.6 #5 split-by-display-name 已於 V3-O.13.5 淨化.)
        real_author_id = str(message.author.id)
        effective_user_id = real_author_id
        real_display_name = str(getattr(message.author, "display_name", "") or "").strip()
        viewer_prefix = ""

        # V3-O.13 #ORD: 帶上 discord server 端 timestamp (snowflake-based) 給 aggregator 排序.
        # 用 message.created_at (discord server 收到時間, monotonic per channel) 而不是
        # relay 收到時間 → 修「server→relay 路徑 reorder」(client→server 段無解).
        try:
            _src_ts_iso = message.created_at.isoformat()
        except Exception:
            _src_ts_iso = ""

        payload = {
            "content": text,
            "is_mention": bool(me is not None and me in message.mentions),
            "channel_id": str(message.channel.id),
            "guild_id": guild_id,
            "channel_kind": ("dm" if not guild_id else "public_text_channel"),
            "source_created_at": _src_ts_iso,  # V3-O.13 #ORD: discord server timestamp 給 aggregator 排序用
            "author": {
                "id": effective_user_id,
                "real_id": real_author_id,
                "display_name": viewer_prefix or real_display_name,
                "bot": bool(getattr(message.author, "bot", False)),
            },
            "mode": self.mode,
            "allow_llm_degraded": self.allow_llm_degraded,
        }
        if self.persona:
            payload["persona"] = self.persona
        if attachments_payload:
            payload["attachments"] = attachments_payload
        # V3-O.10 #26: streaming mode — 讀 vault yaml 決定是否啟用
        _streaming_enabled = False
        if self.vault_root:
            try:
                import yaml as _yaml_s
                _ccfg_s = self.vault_root / "00_System_Core" / "companion_config.yaml"
                if _ccfg_s.exists():
                    _ccfg_d_s = _yaml_s.safe_load(_ccfg_s.read_text(encoding="utf-8")) or {}
                    _streaming_enabled = bool(_ccfg_d_s.get("performance", {}).get("streaming_enabled", False))
            except Exception:
                pass

        url_base = f"{self.bridge_url}/webhook/discord"
        url = url_base + ("/stream" if _streaming_enabled else "")

        loop = asyncio.get_running_loop()
        _t_pre_post = _relay_time.perf_counter()
        try:
            if _streaming_enabled:
                # Streaming: 發出請求，SSE 逐 token 更新 Discord 訊息
                result = await self._post_streaming(loop, url, payload, message)
            else:
                async with message.channel.typing():
                    result = await loop.run_in_executor(None, _post_json, url, payload, self.timeout_s)
            _t_post_done = _relay_time.perf_counter()
        except url_error.HTTPError as exc:
            if processing_added:
                await self._try_remove_reaction(message, self.processing_reaction)
            # V3-O.6.2 #9: \u6280\u8853\u7D30\u7BC0 stderr, \u5C0D chat \u7D66\u89D2\u8272\u8A9E\u6C23\u8A0A\u606F
            print(
                f"[ERR] bridge HTTP {exc.code} chan={message.channel.id} user={message.author.id}: {exc}",
                file=sys.stderr, flush=True,
            )
            error_reply = await message.reply(_friendly_http_reply(), mention_author=False)
            if self.enable_response_reaction:
                await self._try_add_reaction(error_reply, "\u26A0\uFE0F")
            return
        except Exception as exc:  # noqa: BLE001
            if processing_added:
                await self._try_remove_reaction(message, self.processing_reaction)
            err_type = type(exc).__name__
            print(
                f"[ERR] bridge call failed chan={message.channel.id} user={message.author.id}: "
                f"{err_type}: {exc}",
                file=sys.stderr, flush=True,
            )
            # V3-O.10 #23: \u5BEB .ai/failed_turns.jsonl
            if self.vault_root:
                try:
                    import json as _json_ft
                    from datetime import datetime as _dt_ft, timezone as _tz_ft
                    _ft_path = self.vault_root / ".ai" / "failed_turns.jsonl"
                    _ft_path.parent.mkdir(parents=True, exist_ok=True)
                    _record = {
                        "ts": _dt_ft.now(_tz_ft.utc).isoformat(),
                        "user_id": str(message.author.id),
                        "chan_id": str(message.channel.id),
                        "error_type": err_type,
                        "error_msg": str(exc)[:200],
                    }
                    with open(_ft_path, "a", encoding="utf-8") as _ft_f:
                        _ft_f.write(_json_ft.dumps(_record, ensure_ascii=False) + "\n")
                except Exception:
                    pass
            error_reply = await message.reply(_friendly_timeout_reply(), mention_author=False)
            if self.enable_response_reaction:
                await self._try_add_reaction(error_reply, "\u26A0\uFE0F")
            return

        # V3-O.11 階段4: viewer 訊息已被 bridge 個別記錄 + 進彙整佇列 (aggregation_held=true,
        # response 為空) → 不個別回。記下該頻道, 交給背景 flush loop 統一發。
        # owner 訊息無此 flag → 照常往下 reply。
        if result.get("aggregation_held"):
            if processing_added:
                await self._try_remove_reaction(message, self.processing_reaction)
            try:
                guild_present = False
                try:
                    guild_present = message.guild is not None
                except Exception:
                    guild_present = False
                ch_type = "public_text_channel" if guild_present else "dm"
                async with self._pending_lock:
                    self._pending_channels[message.channel.id] = ch_type
            except Exception:  # noqa: BLE001
                pass
            return

        response_text = str(result.get("response", "")).strip()
        if not response_text:
            # V3-O.10 #5 Q8: viewer drop (5s cooldown) → DC 通道送友善訊息
            # owner 不會被 drop (Step 1.5 只對 non-owner 觸發), 所以空 response 一定是 viewer
            if processing_added:
                await self._try_remove_reaction(message, self.processing_reaction)
            try:
                drop_msg = random.choice(_FRIENDLY_DROP_POOL)
                await message.reply(drop_msg, mention_author=False)
            except Exception:
                pass
            return
        if len(response_text) > 1800:
            response_text = response_text[:1800] + "\n...(truncated)"

        degraded = bool(result.get("degraded", False))
        if degraded:
            response_text = f"[degraded]\n{response_text}"

        if processing_added:
            await self._try_remove_reaction(message, self.processing_reaction)

        reply_message = await message.reply(response_text, mention_author=False)
        if self.enable_response_reaction:
            reaction = _pick_response_reaction(response_text, degraded=degraded)
            await self._try_add_reaction(reply_message, reaction)

        # V3-O.9 #2: end-to-end timing log (stderr, 對 user「思考有點久」audit 用)
        # recv→pre_post: relay 端前置 (attachment download + payload build + split)
        # pre_post→post_done: bridge HTTP roundtrip (含 22-step pipeline 全部 ms)
        # post_done→reply_done: Discord reply 發送
        _t_reply_done = _relay_time.perf_counter()
        try:
            print(
                f"[TIMING] chan={message.channel.id} user={message.author.id} "
                f"recv_pre={int((_t_pre_post - _t_recv) * 1000)}ms "
                f"bridge_roundtrip={int((_t_post_done - _t_pre_post) * 1000)}ms "
                f"reply_send={int((_t_reply_done - _t_post_done) * 1000)}ms "
                f"total={int((_t_reply_done - _t_recv) * 1000)}ms "
                f"resp_len={len(response_text)}",
                file=sys.stderr, flush=True,
            )
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord relay: forward messages to Agent Memory bridge webhook.")
    parser.add_argument("--token-env", default="DISCORD_BOT_TOKEN", help="Environment variable containing bot token.")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:16000", help="Bridge base URL.")
    parser.add_argument("--channel-id", action="append", default=[], help="Allowed channel id (repeatable).")
    parser.add_argument("--persona", default="", help="Optional fixed persona id for this relay.")
    parser.add_argument("--mode", default="standard", help="dialogue mode passed to bridge.")
    parser.add_argument(
        "--mention-only-channel-id",
        action="append",
        default=[],
        help="Channel id that requires explicitly mentioning this bot before it responds (repeatable).",
    )
    parser.add_argument(
        "--allow-llm-degraded",
        action="store_true",
        help="Allow degraded responses from bridge.",
    )
    parser.add_argument(
        "--timeout", type=float, default=240.0,
        help=("Bridge HTTP timeout in seconds. V3-O.6.2: default 90→240 給 LLM lock (120s) + "
              "並行 (env AGENT_MEMORY_LLM_PARALLEL) buffer."),
    )
    # V3-O.13.5: --allow-bot-author + --split-by-display-name 兩 flag 已淨化移除.
    # 真實 Discord 用戶各有自己的 author.id, 不需要 single-bot prefix hack.
    parser.add_argument("--vault", default="", help="Vault root path (用於寫 .ai/failed_turns.jsonl, V3-O.10 #23)")
    parser.add_argument("--read-reaction", default="\U0001F440", help="Emoji for read-ack on incoming message.")
    parser.add_argument("--processing-reaction", default="\U0001F9E0", help="Emoji while waiting bridge response.")
    parser.add_argument("--no-read-reaction", action="store_true", help="Disable read reaction.")
    parser.add_argument("--no-processing-reaction", action="store_true", help="Disable processing reaction.")
    parser.add_argument("--no-response-reaction", action="store_true", help="Disable response reaction.")
    parser.add_argument(
        "--disable-message-content-intent",
        action="store_true",
        help="Disable privileged message_content intent (for bots without this intent enabled).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    token = os.getenv(args.token_env, "").strip()
    if not token:
        print(f"[ERR] missing env: {args.token_env}")
        return 2

    allowed_channels: set[int] = set()
    for raw in args.channel_id:
        text = str(raw).strip()
        if not text:
            continue
        try:
            allowed_channels.add(int(text))
        except ValueError:
            print(f"[ERR] invalid channel id: {text}")
            return 2
    mention_only_channels: set[int] = set()
    for raw in args.mention_only_channel_id:
        text = str(raw).strip()
        if not text:
            continue
        try:
            mention_only_channels.add(int(text))
        except ValueError:
            print(f"[ERR] invalid mention-only channel id: {text}")
            return 2

    # V3-O.12 #F-channel: 命令列兩 set 任一為空 + 有 --vault → 從 vault config 補.
    # 命令列覆寫 config (最高優先), 兩者都空時 = relay 收全部頻道 (legacy 行為).
    # 讓未來改頻道只動 vault config, 不必碰命令列.
    # V3-O.13.5: allow_bot_author_ids 路徑已淨化, 不再有對應 fallback.
    if args.vault:
        _fb = _load_channels_from_vault_config(args.vault)
        if not allowed_channels and _fb["channel_ids"]:
            allowed_channels = _fb["channel_ids"]
            print(f"[OK] channels from vault config: {sorted(allowed_channels)}")
        if not mention_only_channels and _fb["mention_only"]:
            mention_only_channels = _fb["mention_only"]
            print(f"[OK] mention-only channels from vault config: {sorted(mention_only_channels)}")

    client = BridgeRelayClient(
        bridge_url=args.bridge_url,
        allowed_channels=allowed_channels,
        persona=str(args.persona),
        mode=args.mode,
        allow_llm_degraded=bool(args.allow_llm_degraded),
        timeout_s=float(args.timeout),
        read_reaction=str(args.read_reaction),
        processing_reaction=str(args.processing_reaction),
        enable_read_reaction=not bool(args.no_read_reaction),
        enable_processing_reaction=not bool(args.no_processing_reaction),
        enable_response_reaction=not bool(args.no_response_reaction),
        enable_message_content_intent=not bool(args.disable_message_content_intent),
        mention_only_channels=mention_only_channels,
        vault_root=args.vault or None,
    )
    client.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
