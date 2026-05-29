# -*- coding: utf-8 -*-
"""V3-F1 (V3-E8 2026-05-27): 觀眾 profile markdown 自動寫入.

對齊:
- V3 §5 vault skeleton 雙寫設計 (DB + Markdown)
- V3 §10.5 / §10.6 VIP/Casual/Banned 分層
- V3 §13 Memory Router L3 self/owner→viewer 擴展
- V3-F_待做清單 F1
- user 2026-05-27 第 3 輪深度觀察 Q2+Q3 (觀眾應該有個別記憶塊)

每次 non-owner turn 在 chat_runtime Step 17.5 觸發:
- 寫 / 更新 20_Audience_Graph/{22_Casual_Viewers,21_VIP_Viewers,23_Banned_Viewers}/<user_id>.md
- frontmatter: loyalty_tier / intimacy_score / intimacy_stage / interaction_count / emotional_resonance_density / first_seen / updated_at
- body: 偏好觀察 (preference_memories) / 對話 highlight (近 5 turn raw_events) / 下次策略提示

owner 不寫 (已有 00.08_Owner_Profile.md, 對齊 V3-E5 動態讀).
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_memory.companion.companion_db import open_companion_db
from agent_memory.security.atomic import atomic_write


CASUAL_DIR = "20_Audience_Graph/22_Casual_Viewers"
VIP_DIR = "20_Audience_Graph/21_VIP_Viewers"
BANNED_DIR = "20_Audience_Graph/23_Banned_Viewers"

MAX_HIGHLIGHTS = 5  # 對話 highlight 保留近 5 pair (user+bot)
MAX_PREF_OBS = 10  # 偏好觀察 keep 10
MAX_DISPLAY_NAME_LEN = 80
MAX_CONTENT_PREVIEW = 80


def _intimacy_stage(intimacy_score: float) -> str:
    """親密度 5 stage (對齊 V3 §10.2)."""
    if intimacy_score >= 0.8:
        return "close"      # 摯友
    elif intimacy_score >= 0.6:
        return "familiar"   # 熟識
    elif intimacy_score >= 0.4:
        return "acquaintance"  # 認識
    elif intimacy_score >= 0.2:
        return "approaching"   # 接近中
    return "stranger"          # 初識


def get_viewer_profile_path(vault_root: Path, user_id: str, loyalty_tier: str) -> Path:
    """根據 loyalty_tier 算出 markdown 路徑."""
    safe_uid = user_id.replace("/", "_").replace("\\", "_")[:120]
    if loyalty_tier == "vip":
        return vault_root / VIP_DIR / f"{safe_uid}.md"
    elif loyalty_tier == "banned":
        return vault_root / BANNED_DIR / f"{safe_uid}.md"
    return vault_root / CASUAL_DIR / f"{safe_uid}.md"


def write_viewer_profile(
    vault_root: Path,
    user_id: str,
    *,
    display_name: str = "",
) -> Optional[Path]:
    """V3-F1: 寫 / 更新 viewer profile markdown.

    撈 DB users + intimacy_states + preference_memories + raw_events + emotion_state,
    組裝 markdown + atomic write 到 20_Audience_Graph/<tier>/<user_id>.md.

    Returns: 寫入的檔案路徑 (失敗回 None, non-critical 不阻塞 chat).
    """
    if not user_id or user_id == "anonymous":
        return None

    try:
        with open_companion_db(vault_root) as conn:
            user_row = conn.execute(
                "SELECT user_id, display_name, role, loyalty_tier, is_banned, first_seen_at, last_seen_at FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if not user_row:
                return None  # user 不存在於 DB, 跳過

            intim_row = conn.execute(
                "SELECT interaction_count, intimacy_score, last_interaction_at FROM intimacy_states WHERE user_id=?",
                (user_id,),
            ).fetchone()

            # 撈最近 raw_events (該 user 自己的, 含 bot reply)
            highlights = conn.execute(
                "SELECT actor, content, created_at FROM raw_events "
                "WHERE user_id=? AND actor IN ('user','bot') "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, MAX_HIGHLIGHTS * 2),
            ).fetchall()

            # 撈 preference_memories (status active/proposed/verified)
            try:
                prefs = conn.execute(
                    "SELECT preference_type AS topic, claim, strength, status, last_seen_at AS updated_at FROM preference_memories "
                    "WHERE user_id=? AND status NOT IN ('rejected','expired') "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (user_id, MAX_PREF_OBS),
                ).fetchall()
            except Exception:
                prefs = []

            # 算 emotional_resonance_density (近 30 turn 強情緒 / 總 turn)
            try:
                total_30 = conn.execute(
                    "SELECT COUNT(*) AS c FROM raw_events WHERE user_id=? AND actor='user'",
                    (user_id,),
                ).fetchone()["c"] or 0
                strong_30 = conn.execute(
                    "SELECT COUNT(*) AS c FROM emotion_state "
                    "WHERE user_id=? AND (sadness > 0.5 OR anger > 0.5 OR fear > 0.5 OR love > 0.5 OR joy > 0.75)",
                    (user_id,),
                ).fetchone()["c"] or 0
                emo_density = (strong_30 / total_30) if total_30 > 0 else 0.0
            except Exception:
                emo_density = 0.0

    except Exception:
        return None

    loyalty_tier = user_row["loyalty_tier"] or "casual"
    interaction_count = (intim_row["interaction_count"] if intim_row else 0) or 0
    intimacy_score = (intim_row["intimacy_score"] if intim_row else 0.0) or 0.0
    last_interaction = (intim_row["last_interaction_at"] if intim_row else None) or user_row["last_seen_at"]
    intim_stage = _intimacy_stage(intimacy_score)

    now_iso = datetime.now(timezone.utc).isoformat()
    final_name = (display_name or user_row["display_name"] or user_id)[:MAX_DISPLAY_NAME_LEN]

    profile_path = get_viewer_profile_path(vault_root, user_id, loyalty_tier)
    profile_path.parent.mkdir(parents=True, exist_ok=True)

    # ─── 組裝 markdown ───
    lines = []
    lines.append("---")
    lines.append("type: viewer_profile")
    lines.append("schema_version: 10")
    lines.append(f"user_id: {user_id}")
    lines.append(f"display_name: {final_name}")
    lines.append(f"loyalty_tier: {loyalty_tier}")
    lines.append(f"intimacy_score: {intimacy_score:.4f}")
    lines.append(f"intimacy_stage: {intim_stage}")
    lines.append(f"interaction_count: {interaction_count}")
    lines.append(f"emotional_resonance_density: {emo_density:.4f}")
    lines.append(f"last_interaction_at: {last_interaction}")
    lines.append(f"first_seen_at: {user_row['first_seen_at']}")
    lines.append(f"updated_at: {now_iso}")
    lines.append(f"role: {user_row['role']}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Viewer Profile — {final_name}")
    lines.append("")
    lines.append("## 我對這個觀眾的觀察 (auto)")
    lines.append("")
    lines.append(f"- loyalty: **{loyalty_tier}** / 親密度: **{intim_stage}** ({intimacy_score:.2f})")
    lines.append(f"- 互動次數: {interaction_count} turn")
    lines.append(f"- 情緒共鳴密度: {emo_density:.2%} (強情緒 turn 占比)")
    lines.append(f"- 首次見面: {user_row['first_seen_at']}")
    lines.append(f"- 最近互動: {last_interaction}")
    lines.append("")

    if prefs:
        lines.append("## 偏好觀察 (我學到的)")
        lines.append("")
        for p in prefs[:MAX_PREF_OBS]:
            strength = float(p["strength"] or 0.0)
            stars = "★" * max(1, int(round(strength * 5)))
            topic = (p["topic"] or "")[:30]
            claim = (p["claim"] or "")[:120].replace("\n", " ")
            status = p["status"] or "?"
            lines.append(f"- **{topic}**: {claim} ({stars} strength={strength:.2f}, status={status})")
        lines.append("")
    else:
        lines.append("## 偏好觀察 (我學到的)")
        lines.append("")
        lines.append("- (還沒累積到夠多 preference_memories)")
        lines.append("")

    if highlights:
        lines.append("## 對話 highlight (近 5 pair)")
        lines.append("")
        for h in highlights[:MAX_HIGHLIGHTS * 2]:
            time_short = (h["created_at"] or "")[:19]
            actor_label = "你" if h["actor"] == "user" else "我"
            content = (h["content"] or "")[:MAX_CONTENT_PREVIEW].replace("\n", " ")
            lines.append(f"- [{time_short}] {actor_label}: {content}")
        lines.append("")
    else:
        lines.append("## 對話 highlight (近 5 pair)")
        lines.append("")
        lines.append("- (還沒有對話紀錄)")
        lines.append("")

    # V3-O.10 #38: 情緒貢獻軌跡段 (per-viewer emotion impact tracking)
    try:
        from agent_memory.companion.companion_db import open_companion_db as _odb
        with _odb(vault_root) as _ec:
            _emo_rows = _ec.execute(
                "SELECT valence, arousal, dominant_emotion, timestamp FROM emotion_state "
                "WHERE user_id=? ORDER BY timestamp DESC LIMIT 5",
                (user_id,),
            ).fetchall()
        if _emo_rows:
            lines.append("## 情緒貢獻軌跡 (近 5 turn)")
            lines.append("")
            for _er in _emo_rows:
                _ts = (_er["timestamp"] or "")[:16]
                _v = float(_er["valence"] or 0.0)
                _a = float(_er["arousal"] or 0.0)
                _emo = _er["dominant_emotion"] or "neutral"
                _sign = "+" if _v >= 0 else ""
                lines.append(f"- [{_ts}] val={_sign}{_v:.2f} aro={_a:.2f} emo={_emo}")
            lines.append("")
    except Exception:
        pass

    lines.append("## 我下次對這個觀眾的策略 (dispatcher hint)")
    lines.append("")
    if loyalty_tier == "banned":
        lines.append("- ⚠️ banned, 全拒回應")
    elif loyalty_tier == "vip":
        lines.append(f"- VIP 觀眾, intim={intimacy_score:.2f}, 可以熟一點, 引用過去 inside joke, 但仍不裝主人熟度")
    elif intimacy_score < 0.3:
        lines.append("- 初次/不熟, 保持禮貌 + 不深度共情 + 不裝熟 (對齊 V3 §27.2 紅線)")
    elif intimacy_score < 0.6:
        lines.append(f"- casual 認識中 ({intim_stage}), 可正常對話, 仍保留新鮮感")
    else:
        lines.append(f"- casual 但熟識 ({intim_stage}), 可正常對話 + 偶爾引用過去, 但不裝主人熟度")
    lines.append("")

    lines.append("---")
    lines.append(f"*Auto-generated by audience_writer.py V3-F1 ({now_iso[:19]})*")
    lines.append("*只有夥伴可以寫此檔, 中之人不該手動改 (除非清理).*")

    content_text = "\n".join(lines) + "\n"

    try:
        atomic_write(profile_path, content_text)
        return profile_path
    except Exception:
        return None


def load_viewer_profile_md(vault_root: Path, user_id: str) -> str:
    """V3-O.7 Round D (朋友卡 input 收束): 讀取 viewer profile markdown 給 LLM context 用.

    在 chat_runtime Step 14 prompt_packet 組裝時呼叫，把朋友卡注入 viewer_dynamic_context.
    先查 DB 確認 loyalty_tier，再讀對應路徑的 .md.
    回傳空字串表示尚無卡片 (新觀眾 / RC1 還沒跑過).
    """
    if not user_id or user_id in ("", "anonymous"):
        return ""
    try:
        with open_companion_db(vault_root) as conn:
            row = conn.execute(
                "SELECT loyalty_tier FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return ""
        tier = row["loyalty_tier"] or "casual"
        profile_path = get_viewer_profile_path(vault_root, user_id, tier)
        if profile_path.exists():
            return profile_path.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""
