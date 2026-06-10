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


# ⭐ V3-O.15.39 (2026-06-10 user 拍板): RAG 撈廣度 + 注入長度 config 化, 每 turn 重讀, 改即生效.
# 預設值 = §A.33「整張塞」設計 — 50 層技能 25000、40 層 KB 25000、20 層朋友卡 5000、90 層日誌 400.
# 過去 hardcode 散在 memory_router.py 多處 (50 step15=4000, 40 step15=2000, 50 L3=25000) 不一致,
# 此次統一走 yaml 一個來源, 想試保守值 (例 8000) 或激進 (50000+) 都改 yaml 即可.
@dataclass
class RAGRetrievalConfig:
    skill_top_k: int = 3                # 50 層 撈幾張技能卡
    skill_max_chars: int = 25000        # 50 層 每張技能卡注入字數 (對齊 §A.33 整張塞)
    kb_top_k: int = 3                   # 40 層 撈幾張知識卡
    kb_max_chars: int = 25000           # 40 層 每張知識卡注入字數 (KB schema v12 full_content ≤22000 字)
    friend_top_k: int = 3               # 20 層 撈幾張朋友卡
    friend_max_chars: int = 5000        # 20 層 每張朋友卡注入字數
    daily_journal_top_k: int = 2        # 90 層 撈幾篇日誌
    daily_journal_max_chars: int = 300  # 90 層 每篇日誌注入字數 (對齊 memory_router L3 歷史值)


@dataclass
class CompanionConfig:
    owner: OwnerConfig = field(default_factory=OwnerConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    hermes: HermesConfig = field(default_factory=HermesConfig)
    skill_learning: SkillLearningConfig = field(default_factory=SkillLearningConfig)
    rag_retrieval: RAGRetrievalConfig = field(default_factory=RAGRetrievalConfig)


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

    # V3-O.15.39: rag_retrieval 區段 (撈廣度 + 注入長度)
    rr = data.get("rag_retrieval", {}) or {}
    rag_retrieval = RAGRetrievalConfig(
        skill_top_k=max(1, int(rr.get("skill_top_k", 3) or 3)),
        skill_max_chars=max(500, int(rr.get("skill_max_chars", 25000) or 25000)),
        kb_top_k=max(1, int(rr.get("kb_top_k", 3) or 3)),
        kb_max_chars=max(500, int(rr.get("kb_max_chars", 25000) or 25000)),
        friend_top_k=max(1, int(rr.get("friend_top_k", 3) or 3)),
        friend_max_chars=max(500, int(rr.get("friend_max_chars", 5000) or 5000)),
        daily_journal_top_k=max(1, int(rr.get("daily_journal_top_k", 2) or 2)),
        daily_journal_max_chars=max(100, int(rr.get("daily_journal_max_chars", 300) or 300)),
    )

    return CompanionConfig(
        owner=owner, channels=channels, hermes=hermes,
        skill_learning=skill_learning, rag_retrieval=rag_retrieval,
    )


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
