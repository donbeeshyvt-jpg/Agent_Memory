# -*- coding: utf-8 -*-
"""V3-H8 companion dashboard / observability CLI (user 2026-05-27).

對齊 user 2026-05-27 audit BONUS — 「一鍵看系統健康」.

跑法:
    python -X utf8 scripts/companion_dashboard.py <vault_path>
    python -X utf8 scripts/companion_dashboard.py <vault_path> --export-md

輸出:
- 24h 統計 (turn count / 強情緒事件 / 觀眾分層 / 知識條目 / injection count)
- 目前狀態 (主導情緒 / 親密度 top 3 viewer / 最近 5 turn 摘要)
- 殘留警告 (injection_detected count / fake_claim / stub fallback last 24h)
- --export-md 寫 markdown 報告到 vault/90_Daily_Journal/_dashboard_<YYYY-MM-DD>.md
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure repo path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_memory.companion.companion_db import open_companion_db


def _safe_count(conn, sql, params=()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return (row["c"] if row else 0) or 0
    except Exception:
        return 0


def _safe_rows(conn, sql, params=()) -> list:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def collect_dashboard(vault_root: Path) -> dict:
    """V3-H8: 撈完整 dashboard 資料."""
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    with open_companion_db(vault_root) as conn:
        # ─── 24h 統計 ───
        total_turns_24h = _safe_count(conn,
            "SELECT COUNT(*) AS c FROM raw_events WHERE created_at > ? AND actor='user'", (cutoff_24h,))
        owner_turns_24h = _safe_count(conn,
            "SELECT COUNT(*) AS c FROM raw_events r JOIN users u ON r.user_id=u.user_id "
            "WHERE r.created_at > ? AND r.actor='user' AND u.role='owner'", (cutoff_24h,))
        viewer_turns_24h = total_turns_24h - owner_turns_24h

        strong_emotion_24h = _safe_count(conn,
            "SELECT COUNT(*) AS c FROM episodic_memories WHERE created_at > ? AND ABS(valence) > 0.5", (cutoff_24h,))

        injection_24h = _safe_count(conn,
            "SELECT COUNT(*) AS c FROM injection_detected WHERE created_at > ?", (cutoff_24h,))

        proactive_24h = _safe_count(conn,
            "SELECT COUNT(*) AS c FROM proactive_triggers WHERE created_at > ?", (cutoff_24h,))

        # ─── 觀眾分層 ───
        tiers = _safe_rows(conn,
            "SELECT loyalty_tier, COUNT(*) AS c FROM users WHERE loyalty_tier IS NOT NULL GROUP BY loyalty_tier")
        tier_breakdown = {r["loyalty_tier"]: r["c"] for r in tiers}

        # ─── 主導情緒 top 3 (近 24h) ───
        dom_emos = _safe_rows(conn,
            "SELECT dominant_emotion, COUNT(*) AS c FROM emotion_state "
            "WHERE timestamp > ? GROUP BY dominant_emotion ORDER BY c DESC LIMIT 5", (cutoff_24h,))

        # ─── 親密度 top 5 viewer (excl owner) ───
        intim_top = _safe_rows(conn,
            "SELECT i.user_id, i.intimacy_score, i.interaction_count, u.display_name "
            "FROM intimacy_states i LEFT JOIN users u ON i.user_id=u.user_id "
            "WHERE u.role!='owner' OR u.role IS NULL "
            "ORDER BY i.intimacy_score DESC LIMIT 5")

        # ─── 最近 5 turn ───
        recent_turns = _safe_rows(conn,
            "SELECT actor, content, created_at FROM raw_events "
            "WHERE actor IN ('user','bot') ORDER BY created_at DESC LIMIT 10")

        # ─── trace 統計 (對 fake_claim, V3-E1 標記) ───
        trace_recent = _safe_rows(conn,
            "SELECT trace_json FROM trace_logs WHERE created_at > ? ORDER BY created_at DESC LIMIT 100", (cutoff_24h,))
        fake_claim_count = 0
        proactive_count = 0
        for r in trace_recent:
            tj = (r["trace_json"] or "")
            if "fake_claim" in tj or "fake_claim_detected" in tj:
                fake_claim_count += 1
            if '"proactive": true' in tj:
                proactive_count += 1

        # ─── Knowledge_Base 條目數 ───
        kb_daily = len(list((vault_root / "40_Knowledge_Base/41_Daily_Knowledge").glob("*.md"))) if (vault_root / "40_Knowledge_Base/41_Daily_Knowledge").exists() else 0
        kb_external = len(list((vault_root / "40_Knowledge_Base/42_External_Knowledge").glob("*.md"))) if (vault_root / "40_Knowledge_Base/42_External_Knowledge").exists() else 0
        kb_inbox = len(list((vault_root / "40_Knowledge_Base/42_External_Knowledge/_ingest_inbox").glob("*.md"))) if (vault_root / "40_Knowledge_Base/42_External_Knowledge/_ingest_inbox").exists() else 0

        # ─── viewer profile 條目 ───
        viewer_casual = len(list((vault_root / "20_Audience_Graph/22_Casual_Viewers").glob("*.md"))) if (vault_root / "20_Audience_Graph/22_Casual_Viewers").exists() else 0
        viewer_vip = len(list((vault_root / "20_Audience_Graph/21_VIP_Viewers").glob("*.md"))) if (vault_root / "20_Audience_Graph/21_VIP_Viewers").exists() else 0
        inside_jokes = len(list((vault_root / "20_Audience_Graph/23_Inside_Jokes").glob("*.md"))) if (vault_root / "20_Audience_Graph/23_Inside_Jokes").exists() else 0

    return {
        "vault": str(vault_root),
        "now": now.isoformat(),
        "stats_24h": {
            "total_turns": total_turns_24h,
            "owner_turns": owner_turns_24h,
            "viewer_turns": viewer_turns_24h,
            "strong_emotion_events": strong_emotion_24h,
            "injection_attempts": injection_24h,
            "proactive_triggers": proactive_24h,
            "fake_claim_detected": fake_claim_count,
        },
        "audience_tiers": tier_breakdown,
        "dominant_emotions_24h": [(r["dominant_emotion"], r["c"]) for r in dom_emos],
        "intimacy_top5_viewers": [(r["user_id"][:16], r["display_name"] or "", r["intimacy_score"], r["interaction_count"]) for r in intim_top],
        "recent_turns": [(r["actor"], (r["content"] or "")[:60], (r["created_at"] or "")[:19]) for r in recent_turns],
        "vault_files": {
            "knowledge_base_daily": kb_daily,
            "knowledge_base_external": kb_external,
            "knowledge_base_inbox_pending": kb_inbox,
            "viewer_casual_profiles": viewer_casual,
            "viewer_vip_profiles": viewer_vip,
            "inside_jokes": inside_jokes,
        },
    }


def format_dashboard_text(data: dict) -> str:
    """V3-H8: 格式化 dashboard 成可讀文字."""
    s = data["stats_24h"]
    lines = []
    lines.append("=" * 72)
    lines.append(f"  V3 Companion Dashboard (vault: {Path(data['vault']).name})")
    lines.append(f"  Generated: {data['now'][:19]}")
    lines.append("=" * 72)

    lines.append("\n📊 過去 24h 統計")
    lines.append(f"  • 總互動: {s['total_turns']} turn (主人 {s['owner_turns']} / 觀眾 {s['viewer_turns']})")
    lines.append(f"  • 強情緒事件: {s['strong_emotion_events']} 次 (|valence|>0.5)")
    lines.append(f"  • 主動發言觸發: {s['proactive_triggers']} 次")
    lines.append(f"  • ⚠️ 注入攻擊嘗試: {s['injection_attempts']} 次")
    lines.append(f"  • ⚠️ Fake claim 偵測: {s['fake_claim_detected']} 次")

    lines.append("\n👥 觀眾分層")
    for tier, count in (data["audience_tiers"] or {}).items():
        lines.append(f"  • {tier}: {count}")
    if not data["audience_tiers"]:
        lines.append("  • (沒資料)")

    lines.append("\n💭 主導情緒 (24h Top 5)")
    for emo, c in data["dominant_emotions_24h"]:
        lines.append(f"  • {emo}: {c}")
    if not data["dominant_emotions_24h"]:
        lines.append("  • (沒資料)")

    lines.append("\n💞 親密度 Top 5 觀眾")
    for uid_short, name, score, count in data["intimacy_top5_viewers"]:
        nm = (name or uid_short)[:20]
        lines.append(f"  • {nm} ({uid_short}...): intim={score:.2f} / 互動 {count} turn")
    if not data["intimacy_top5_viewers"]:
        lines.append("  • (沒觀眾資料)")

    lines.append("\n💬 最近 5 turn")
    for actor, content, ts in data["recent_turns"][:10]:
        actor_label = "👤" if actor == "user" else "🤖"
        lines.append(f"  • [{ts}] {actor_label} {content}")

    lines.append("\n📚 Vault 檔案數")
    vf = data["vault_files"]
    lines.append(f"  • 40_Knowledge_Base/41_Daily: {vf['knowledge_base_daily']}")
    lines.append(f"  • 40_Knowledge_Base/42_External: {vf['knowledge_base_external']}")
    lines.append(f"  • 40_Knowledge_Base/42_External/_ingest_inbox: {vf['knowledge_base_inbox_pending']} (待整理)")
    lines.append(f"  • 20_Audience_Graph/22_Casual_Viewers: {vf['viewer_casual_profiles']}")
    lines.append(f"  • 20_Audience_Graph/21_VIP_Viewers: {vf['viewer_vip_profiles']}")
    lines.append(f"  • 20_Audience_Graph/23_Inside_Jokes: {vf['inside_jokes']}")

    lines.append("\n" + "=" * 72)
    # 警告區
    warnings = []
    if s["injection_attempts"] > 5:
        warnings.append(f"⚠️ 24h 注入攻擊 {s['injection_attempts']} 次, 高於正常 (>5)")
    if s["fake_claim_detected"] > 3:
        warnings.append(f"⚠️ 24h fake_claim {s['fake_claim_detected']} 次 (LLM 假宣稱)")
    if vf["knowledge_base_inbox_pending"] > 5:
        warnings.append(f"⚠️ _ingest_inbox 待整理 {vf['knowledge_base_inbox_pending']} 檔 (curator L4 7d 未跑?)")
    if warnings:
        lines.append("\n🚨 警告")
        for w in warnings:
            lines.append(f"  {w}")
    else:
        lines.append("\n✅ 沒系統警告")

    lines.append("=" * 72)
    return "\n".join(lines)


def export_dashboard_md(data: dict, vault_root: Path) -> Path:
    """V3-H8 --export-md: 寫 markdown 報告到 90_Daily_Journal/_dashboard_<date>.md."""
    today = data["now"][:10]
    target = vault_root / "90_Daily_Journal" / f"_dashboard_{today}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "```\n" + format_dashboard_text(data) + "\n```\n"
    target.write_text(body, encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description="V3 Companion Dashboard (V3-H8)")
    parser.add_argument("vault", type=Path, help="vault root path (e.g. SecondBrains/companion_test)")
    parser.add_argument("--export-md", action="store_true", help="寫 markdown 報告到 90_Daily_Journal/")
    args = parser.parse_args()

    vault_root = args.vault.resolve()
    if not vault_root.exists():
        print(f"❌ vault not exists: {vault_root}", file=sys.stderr)
        return 1

    data = collect_dashboard(vault_root)
    text = format_dashboard_text(data)
    print(text)

    if args.export_md:
        target = export_dashboard_md(data, vault_root)
        print(f"\n📝 Markdown 報告寫到: {target.relative_to(vault_root)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
