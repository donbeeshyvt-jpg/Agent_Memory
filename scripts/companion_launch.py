"""V3-O.10 #19 — 統一啟動入口 companion_launch.py.

1 條指令啟動 bridge + relay, 全從 companion_config.yaml 讀:
  python scripts/companion_launch.py --vault <path>

自動讀取:
  - channels.discord.bot_token_env → 從 env 撈 token
  - channels.discord.channel_id_env → 從 env 撈 channel_id
  - channels.discord.allow_bot_author_ids
  - channels.discord.split_by_display_name
  - channels.discord.relay_timeout_s
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _load_yaml(yaml_path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[launch] 無法讀取 {yaml_path}: {e}", file=sys.stderr)
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Companion unified launch script")
    parser.add_argument("--vault", required=True, help="Vault root path")
    parser.add_argument("--bridge-port", default=16001, type=int)
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running")
    args = parser.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    if not vault.exists():
        print(f"[launch] vault 不存在: {vault}", file=sys.stderr)
        sys.exit(1)

    yaml_path = vault / "00_System_Core" / "companion_config.yaml"
    cfg = _load_yaml(yaml_path)
    dc = cfg.get("channels", {}).get("discord", {}) or {}

    # 讀 bot token
    token_env = dc.get("bot_token_env", "DISCORD_BOT_TOKEN_COMPANION")
    token = os.getenv(token_env, "").strip()
    if not token:
        print(f"[launch] 警告: env {token_env} 未設定, relay 可能無法啟動", file=sys.stderr)

    # 讀 channel_id
    ch_env = dc.get("channel_id_env", "DISCORD_CHANNEL_ID_COMPANION")
    channel_id = os.getenv(ch_env, "").strip()

    # 讀其他設定
    allow_bots = dc.get("allow_bot_author_ids", []) or []
    split_by_name = bool(dc.get("split_by_display_name", True))
    relay_timeout = int(dc.get("relay_timeout_s", 240))

    bridge_port = args.bridge_port

    # ── 組裝 bridge 指令 ──
    bridge_cmd = [
        sys.executable, "-m", "agent_memory",
        "companion-bridge",
        "--vault", str(vault),
        "--port", str(bridge_port),
    ]

    # ── 組裝 relay 指令 ──
    relay_cmd = [
        sys.executable,
        str(Path(__file__).parent / "discord_bridge_relay.py"),
        "--bridge-url", f"http://localhost:{bridge_port}",
        "--vault", str(vault),
        "--timeout", str(relay_timeout),
    ]
    if token:
        relay_cmd += ["--token-env", token_env]
    if channel_id:
        relay_cmd += ["--channel-id", channel_id]
    if split_by_name:
        relay_cmd += ["--split-by-display-name"]
    for bot_id in allow_bots:
        relay_cmd += ["--allow-bot-author", str(bot_id)]

    print(f"[launch] vault: {vault}")
    print(f"[launch] bridge: {' '.join(bridge_cmd)}")
    print(f"[launch] relay:  {' '.join(relay_cmd)}")

    if args.dry_run:
        print("[launch] dry-run mode — 不實際啟動")
        return

    # 啟 bridge
    bridge_proc = subprocess.Popen(bridge_cmd)
    print(f"[launch] bridge PID={bridge_proc.pid}, 等待 2s 啟動...")
    time.sleep(2.0)

    # 啟 relay
    relay_proc = subprocess.Popen(relay_cmd)
    print(f"[launch] relay PID={relay_proc.pid}")
    print("[launch] 按 Ctrl+C 停止")

    try:
        bridge_proc.wait()
    except KeyboardInterrupt:
        print("\n[launch] 停止...")
        relay_proc.terminate()
        bridge_proc.terminate()


if __name__ == "__main__":
    main()
