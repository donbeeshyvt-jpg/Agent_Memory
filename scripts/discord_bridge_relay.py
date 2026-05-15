from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any
from urllib import error as url_error
from urllib import request as url_request

import discord


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
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = bool(enable_message_content_intent)
        intents.messages = True
        intents.guilds = True
        super().__init__(intents=intents)
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

    async def on_message(self, message: discord.Message) -> None:
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

        payload = {
            "content": text,
            "channel_id": str(message.channel.id),
            "author": {"id": str(message.author.id)},
            "mode": self.mode,
            "allow_llm_degraded": self.allow_llm_degraded,
        }
        if self.persona:
            payload["persona"] = self.persona
        if attachments_payload:
            payload["attachments"] = attachments_payload
        url = f"{self.bridge_url}/webhook/discord"

        loop = asyncio.get_running_loop()
        try:
            async with message.channel.typing():
                result = await loop.run_in_executor(None, _post_json, url, payload, self.timeout_s)
        except url_error.HTTPError as exc:
            if processing_added:
                await self._try_remove_reaction(message, self.processing_reaction)
            error_reply = await message.reply(f"bridge HTTP error: {exc.code}", mention_author=False)
            if self.enable_response_reaction:
                await self._try_add_reaction(error_reply, "\u26A0\uFE0F")
            return
        except Exception as exc:  # noqa: BLE001
            if processing_added:
                await self._try_remove_reaction(message, self.processing_reaction)
            error_reply = await message.reply(f"bridge call failed: {exc}", mention_author=False)
            if self.enable_response_reaction:
                await self._try_add_reaction(error_reply, "\u26A0\uFE0F")
            return

        response_text = str(result.get("response", "")).strip()
        if not response_text:
            response_text = "(empty response)"
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
    parser.add_argument("--timeout", type=float, default=90.0, help="Bridge HTTP timeout in seconds.")
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
    )
    client.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
