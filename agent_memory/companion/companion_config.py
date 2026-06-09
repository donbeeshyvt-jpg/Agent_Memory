"""V3 companion_config.yaml — 夥伴核心設定讀取器.

對齊 V3 §3.2 first-run-wizard + §4 Mode A + §17.2 personality.

一個 yaml 統管:
  owner     — 各平台 owner user_id (可直接編輯, 每 turn 重讀)
  channels  — Discord / YouTube / LINE 頻道設定
  hermes    — hermes 信任識別 + 直接進 40_Knowledge_Base
  personality — 沿用 personality_switcher 既有格式 (不衝突)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_RELPATH = "00_System_Core/companion_config.yaml"


@dataclass
class OwnerConfig:
    discord_user_id: str = ""
    youtube_channel_id: str = ""
    line_user_id: str = ""
    label: str = "中之人"
    directive_acceptance_weight: float = 0.85


@dataclass
class ChannelDiscordConfig:
    enabled: bool = True
    owner_dm_channel_ids: list[str] = field(default_factory=list)


@dataclass
class ChannelYouTubeConfig:
    enabled: bool = False
    live_channel_id: str = ""


@dataclass
class ChannelLineConfig:
    enabled: bool = True


@dataclass
class ChannelsConfig:
    discord: ChannelDiscordConfig = field(default_factory=ChannelDiscordConfig)
    youtube: ChannelYouTubeConfig = field(default_factory=ChannelYouTubeConfig)
    line: ChannelLineConfig = field(default_factory=ChannelLineConfig)


@dataclass
class HermesConfig:
    enabled: bool = False
    trusted_user_ids: list[str] = field(default_factory=list)
    ingest_direct: bool = True


# ⭐ V3-O.15.30 (2026-06-09 user 拍板): 教學升格門檻 + 合併間隔 config 化, 每 turn / 每 daemon tick 重讀.
# 預設 = 歷史值 (向後相容). 設 evidence_threshold=1 = 「一次教學即升格」極端模式試.
@dataclass
class SkillLearningConfig:
    evidence_threshold: int = 3                    # 教幾次升格 (1=一次即升, 3=歷史保守值)
    consolidate_interval_s: int = 900              # 15 min 合併間隔 (秒)
    weekly_consolidate_interval_s: int = 604800    # 7 天跨層合併間隔 (秒)


@dataclass
class CompanionConfig:
    owner: OwnerConfig = field(default_factory=OwnerConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    hermes: HermesConfig = field(default_factory=HermesConfig)
    skill_learning: SkillLearningConfig = field(default_factory=SkillLearningConfig)


def load_companion_config(vault_root: Path) -> CompanionConfig:
    """讀 companion_config.yaml — 回傳 CompanionConfig. 不存在 → 全 default."""
    p = vault_root / CONFIG_RELPATH
    if not p.exists():
        return CompanionConfig()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return CompanionConfig()

    o = data.get("owner", {}) or {}
    owner = OwnerConfig(
        discord_user_id=str(o.get("discord_user_id", "") or ""),
        youtube_channel_id=str(o.get("youtube_channel_id", "") or ""),
        line_user_id=str(o.get("line_user_id", "") or ""),
        label=str(o.get("label", "中之人")),
        directive_acceptance_weight=float(o.get("directive_acceptance_weight", 0.85)),
    )

    ch = data.get("channels", {}) or {}
    dc = ch.get("discord", {}) or {}
    yt = ch.get("youtube", {}) or {}
    ln = ch.get("line", {}) or {}
    channels = ChannelsConfig(
        discord=ChannelDiscordConfig(
            enabled=bool(dc.get("enabled", True)),
            owner_dm_channel_ids=[str(x) for x in (dc.get("owner_dm_channel_ids") or [])],
        ),
        youtube=ChannelYouTubeConfig(
            enabled=bool(yt.get("enabled", False)),
            live_channel_id=str(yt.get("live_channel_id", "") or ""),
        ),
        line=ChannelLineConfig(enabled=bool(ln.get("enabled", True))),
    )

    h = data.get("hermes", {}) or {}
    hermes = HermesConfig(
        enabled=bool(h.get("enabled", False)),
        trusted_user_ids=[str(x) for x in (h.get("trusted_user_ids") or [])],
        ingest_direct=bool(h.get("ingest_direct", True)),
    )

    # V3-O.15.30: skill_learning 區段 (升格門檻 + 合併間隔)
    sl = data.get("skill_learning", {}) or {}
    skill_learning = SkillLearningConfig(
        evidence_threshold=max(1, int(sl.get("evidence_threshold", 3) or 3)),  # 最小 1 防呆
        consolidate_interval_s=max(60, int(sl.get("consolidate_interval_s", 900) or 900)),  # 最小 60s 防呆
        weekly_consolidate_interval_s=max(3600, int(sl.get("weekly_consolidate_interval_s", 604800) or 604800)),
    )

    return CompanionConfig(owner=owner, channels=channels, hermes=hermes, skill_learning=skill_learning)


def get_owner_user_id_for_transport(vault_root: Path, transport: str) -> str:
    """給 transport 名稱 → 對應平台的 owner user_id."""
    cfg = load_companion_config(vault_root)
    t = (transport or "").lower()
    if "discord" in t:
        return cfg.owner.discord_user_id
    if "youtube" in t or "yt" in t:
        return cfg.owner.youtube_channel_id
    if "line" in t:
        return cfg.owner.line_user_id
    # fallback: 任何非空 id 都試
    for uid in (cfg.owner.discord_user_id, cfg.owner.youtube_channel_id, cfg.owner.line_user_id):
        if uid:
            return uid
    return ""


def is_hermes_sender(vault_root: Path, user_id: str) -> bool:
    """user_id 是否為受信任的 hermes 實例."""
    if not user_id:
        return False
    cfg = load_companion_config(vault_root)
    return cfg.hermes.enabled and user_id in cfg.hermes.trusted_user_ids
