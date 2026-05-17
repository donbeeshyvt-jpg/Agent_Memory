"""Curator — idle-trigger 自動進化 (R7 C18).

抄 hermes agent/curator.py:198-248 should_run_now + .curator_state JSON 持久化模型,
改本機時區 (datetime.now().astimezone() with offset), 拆 daily/weekly 雙節奏:

- Daily light  (24h interval + idle≥2h):
    * 短→中 aggregate (call C17 aggregate_to_midterm)
    * 標 stale (C19 接降級 — 本 commit stub)
    * keyword umbrella (C20a 接 — 本 commit stub)
- Weekly deep  (7d  interval + idle≥2h):
    * 中→長升格 (C19 接 — 本 commit stub list candidates)
    * Archive 移檔 (C19 接)
    * LLM umbrella consolidation (C20a 接)
    * Skill 升格提議寫 pending_skill_suggestions.json (C20b 接)

設計原則 (V2_Round7 §1 §5):
- 雙軌制: counter (使用者直覺) + 時間 (hermes 穩定性)
- AND 條件: idle ≥ N AND 距上次 ≥ interval — 不會在使用者連續對話時跑
- First-run defer: 首次 call 只 seed state, 真實跑要等下一輪 (避免 fresh install 立即動舊資料)
- Pinned skip: 任何 pinned=True 檔, 升降格邏輯都跳過 (由 C19 enforce)
- 本機時區: last_run_at 寫 ISO with +08:00 offset, AI 關掉重啟讀回不重置
- Circuit breaker (openclaw 抄): consecutive_failures + last_failure_at 追蹤, C19 升格邏輯內檢查

跟 C15 auto_evolve 並存分工:
- C15 auto_evolve = chat 對話導向的「即時短→中即時 promote」(chat-counter 10 次 fire-and-forget)
- R7 curator     = 時間導向的「daily/weekly 深度整理」(idle 才跑, 整輪掃 vault)
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from agent_memory.memory_promotion import (
    aggregate_to_midterm,
    consolidate_umbrella_keyword,
    demote_long_to_stale_or_archive,
    list_midterm_entries,
    promote_midterm_to_long,
)
from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock

CURATOR_STATE_RELATIVE_PATH = ".ai/curator_state.json"
PROMOTION_CONFIG_RELATIVE_PATH = "00_System/08_Runtime_Profiles/promotion.yaml"
CURATOR_LOG_RELATIVE_PATH = "11_AI_Mirror/ingestion_logs/curator_runs.jsonl"


def _now_local() -> datetime:
    """本機時區當下 (含 offset)."""

    return datetime.now().astimezone()


def _now_local_iso() -> str:
    return _now_local().isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ─── Config + State dataclass ────────────────────────────────────────────────


@dataclass(slots=True)
class CuratorConfig:
    """Curator 配置 — 從 promotion.yaml 讀 (缺 → defaults).

    R9 C34: 三層節奏 (對齊使用者 2026-05-17「2h 固定整理 + 7d LLM 統整」直覺)
    - light  (2h interval, min_idle 30min): 純機械短→中 aggregate (跨頻道對話即時可檢索)
    - medium (24h interval, min_idle 1h)  : 純機械 umbrella keyword + gap scan + 標 stale
    - deep   (7d interval, min_idle 2h)   : LLM 整理 (umbrella + narrative + 矛盾偵測) + 升長 + archive + skill scan + digest

    舊 daily_interval_hours / weekly_interval_hours 保留為 backward-compat alias.
    """

    # R9 C34: 三層節奏
    light_interval_hours: float = 2.0
    light_min_idle_hours: float = 0.5
    medium_interval_hours: float = 24.0
    medium_min_idle_hours: float = 1.0
    weekly_interval_hours: float = 168.0  # 7d (deep)
    min_idle_hours: float = 2.0  # weekly deep idle 條件 + 通用 fallback
    # 共通
    first_run_defer: bool = True
    paused: bool = False
    circuit_breaker_max_failures: int = 3
    circuit_breaker_cooldown_minutes: float = 60.0
    # Backward-compat (舊 R7 配置, 還有人在用): daily ≈ medium
    daily_interval_hours: float = 24.0

    @classmethod
    def from_yaml(cls, payload: dict) -> "CuratorConfig":
        if not isinstance(payload, dict):
            return cls()
        curator = payload.get("curator", {})
        if not isinstance(curator, dict):
            curator = {}
        return cls(
            light_interval_hours=float(curator.get("light_interval_hours", 2.0)),
            light_min_idle_hours=float(curator.get("light_min_idle_hours", 0.5)),
            medium_interval_hours=float(curator.get("medium_interval_hours", 24.0)),
            medium_min_idle_hours=float(curator.get("medium_min_idle_hours", 1.0)),
            weekly_interval_hours=float(curator.get("weekly_interval_hours", 168.0)),
            min_idle_hours=float(curator.get("min_idle_hours", 2.0)),
            first_run_defer=bool(curator.get("first_run_defer", True)),
            paused=bool(curator.get("paused", False)),
            circuit_breaker_max_failures=int(curator.get("circuit_breaker_max_failures", 3)),
            circuit_breaker_cooldown_minutes=float(curator.get("circuit_breaker_cooldown_minutes", 60.0)),
            daily_interval_hours=float(curator.get("daily_interval_hours", 24.0)),
        )


@dataclass(slots=True)
class CuratorState:
    """Curator 持久化 state — 寫到 .ai/curator_state.json (本機時區 ISO).

    R9 C34: 三層節奏 → 加 last_light_run_at / last_medium_run_at + 對應 seeded.
    保留 last_daily_run_at 為 backward-compat alias (= last_medium_run_at).
    """

    # R9 C34: 三層
    last_light_run_at: Optional[datetime] = None
    last_medium_run_at: Optional[datetime] = None
    last_weekly_run_at: Optional[datetime] = None
    last_chat_at: Optional[datetime] = None
    first_light_seeded_at: Optional[datetime] = None
    first_medium_seeded_at: Optional[datetime] = None
    first_weekly_seeded_at: Optional[datetime] = None
    consecutive_failures: int = 0
    last_failure_at: Optional[datetime] = None
    # Backward-compat aliases (R7 readers 還能讀)
    last_daily_run_at: Optional[datetime] = None
    first_daily_seeded_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        def _iso(dt: datetime | None) -> str:
            return dt.isoformat() if dt else ""

        return {
            "last_light_run_at": _iso(self.last_light_run_at),
            "last_medium_run_at": _iso(self.last_medium_run_at),
            "last_weekly_run_at": _iso(self.last_weekly_run_at),
            "last_chat_at": _iso(self.last_chat_at),
            "first_light_seeded_at": _iso(self.first_light_seeded_at),
            "first_medium_seeded_at": _iso(self.first_medium_seeded_at),
            "first_weekly_seeded_at": _iso(self.first_weekly_seeded_at),
            "consecutive_failures": int(self.consecutive_failures),
            "last_failure_at": _iso(self.last_failure_at),
            # Backward-compat
            "last_daily_run_at": _iso(self.last_daily_run_at or self.last_medium_run_at),
            "first_daily_seeded_at": _iso(self.first_daily_seeded_at or self.first_medium_seeded_at),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CuratorState":
        if not isinstance(payload, dict):
            return cls()
        # 從 R7 state 讀: last_daily_run_at 在 R9 變 last_medium_run_at (語意相同 - 一天一次)
        legacy_daily = _parse_iso(payload.get("last_daily_run_at", ""))
        legacy_daily_seed = _parse_iso(payload.get("first_daily_seeded_at", ""))
        return cls(
            last_light_run_at=_parse_iso(payload.get("last_light_run_at", "")),
            last_medium_run_at=_parse_iso(payload.get("last_medium_run_at", "")) or legacy_daily,
            last_weekly_run_at=_parse_iso(payload.get("last_weekly_run_at", "")),
            last_chat_at=_parse_iso(payload.get("last_chat_at", "")),
            first_light_seeded_at=_parse_iso(payload.get("first_light_seeded_at", "")),
            first_medium_seeded_at=_parse_iso(payload.get("first_medium_seeded_at", "")) or legacy_daily_seed,
            first_weekly_seeded_at=_parse_iso(payload.get("first_weekly_seeded_at", "")),
            consecutive_failures=int(payload.get("consecutive_failures", 0)),
            last_failure_at=_parse_iso(payload.get("last_failure_at", "")),
            last_daily_run_at=legacy_daily,
            first_daily_seeded_at=legacy_daily_seed,
        )


# ─── IO: config / state ──────────────────────────────────────────────────────


def load_config(vault_root: Path) -> CuratorConfig:
    """Load promotion.yaml or return defaults."""

    root = Path(vault_root).expanduser().resolve()
    cfg_path = root / PROMOTION_CONFIG_RELATIVE_PATH
    if not cfg_path.exists():
        return CuratorConfig()
    try:
        payload = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return CuratorConfig()
        return CuratorConfig.from_yaml(payload)
    except Exception:  # noqa: BLE001
        return CuratorConfig()


def load_state(vault_root: Path) -> CuratorState:
    """Load curator state from .ai/curator_state.json. 不存在 → 回新 state."""

    root = Path(vault_root).expanduser().resolve()
    state_path = root / CURATOR_STATE_RELATIVE_PATH
    if not state_path.exists():
        return CuratorState()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return CuratorState.from_dict(payload)
    except Exception:  # noqa: BLE001
        return CuratorState()


def save_state(vault_root: Path, state: CuratorState) -> None:
    root = Path(vault_root).expanduser().resolve()
    state_path = root / CURATOR_STATE_RELATIVE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict()
    with file_lock(state_path, timeout=5.0):
        atomic_write(state_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def ensure_promotion_config_file(vault_root: Path, *, overwrite: bool = False) -> Path:
    """Bootstrap default promotion.yaml at first install. 由 obsidian.py bootstrap 呼叫."""

    root = Path(vault_root).expanduser().resolve()
    cfg_path = root / PROMOTION_CONFIG_RELATIVE_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists() and not overwrite:
        return cfg_path

    default_payload = {
        "schema_version": 1,
        "short_to_mid": {
            "min_mention_count": 2,
            "min_grace_hours": 24,
            "min_unique_sessions": 2,
            "min_score": 0.6,
        },
        "mid_to_long": {
            "min_mention_count": 3,
            "min_stable_days": 7,
            "no_edit_days": 3,
            "llm_gate": False,
        },
        "long_lifecycle": {
            "stale_after_days": 90,
            "archive_after_days": 180,
        },
        "curator": {
            # R9 C34: 三層節奏 (對齊使用者「2h 機械 + 7d LLM」直覺)
            "light_interval_hours": 2,
            "light_min_idle_hours": 0.5,
            "medium_interval_hours": 24,
            "medium_min_idle_hours": 1,
            "weekly_interval_hours": 168,
            "min_idle_hours": 2,
            "first_run_defer": True,
            "use_local_timezone": True,
            "paused": False,
            "circuit_breaker_max_failures": 3,
            "circuit_breaker_cooldown_minutes": 60,
            # Backward-compat alias (R7 readers 用)
            "daily_interval_hours": 24,
        },
        "skill_suggestion": {
            "in_chat_proposal": True,
            "max_per_response": 1,
            "per_entity_cooldown_days": 7,
            "auto_dismiss_after_days": 7,
            "menu_batch_fallback": True,
        },
    }
    atomic_write(cfg_path, yaml.safe_dump(default_payload, allow_unicode=True, sort_keys=False))
    return cfg_path


# ─── Core: should_run_now (抄 hermes) ────────────────────────────────────────


def should_run_now(
    state: CuratorState,
    config: CuratorConfig,
    mode: str = "medium",
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """抄 hermes agent/curator.py:198-248 — AND 條件 should_run_now.

    R9 C34: 三層 mode — light (2h) / medium (24h, 舊 daily 別名) / weekly (7d).
    Backward-compat: mode='daily' → 視為 'medium'.

    Args:
        state: 當前 CuratorState (注意: first_run_defer 會就地改 seeded_at, caller 要 save_state)
        config: CuratorConfig
        mode: 'light' | 'medium' | 'weekly' (or 'daily' 為 medium 別名)
        now: 測試 inject; None → _now_local()

    Returns: (ok, reason) — reason 為 log/diag 用 string
    """

    now = now or _now_local()

    if config.paused:
        return False, "paused"

    # Circuit breaker — 若連續失敗達上限, 在 cooldown 期內不跑
    if (
        state.consecutive_failures >= config.circuit_breaker_max_failures
        and state.last_failure_at is not None
    ):
        cooldown_seconds = config.circuit_breaker_cooldown_minutes * 60
        elapsed = (now - state.last_failure_at).total_seconds()
        if elapsed < cooldown_seconds:
            return False, f"circuit_breaker_cooldown ({elapsed / 60:.1f}min / {config.circuit_breaker_cooldown_minutes}min)"

    # mode 標準化 + 對應參數
    mode_norm = mode.strip().lower()
    if mode_norm == "daily":
        mode_norm = "medium"  # backward-compat alias

    if mode_norm == "light":
        last_run = state.last_light_run_at
        interval_hours = config.light_interval_hours
        seeded_at = state.first_light_seeded_at
        min_idle = config.light_min_idle_hours
        seed_attr = "first_light_seeded_at"
    elif mode_norm == "medium":
        last_run = state.last_medium_run_at
        interval_hours = config.medium_interval_hours
        seeded_at = state.first_medium_seeded_at
        min_idle = config.medium_min_idle_hours
        seed_attr = "first_medium_seeded_at"
    elif mode_norm == "weekly":
        last_run = state.last_weekly_run_at
        interval_hours = config.weekly_interval_hours
        seeded_at = state.first_weekly_seeded_at
        min_idle = config.min_idle_hours
        seed_attr = "first_weekly_seeded_at"
    else:
        return False, f"unknown_mode: {mode}"

    # First-run defer (hermes 抄): 第一次 call 只 seed 不跑
    if config.first_run_defer and seeded_at is None and last_run is None:
        setattr(state, seed_attr, now)
        return False, "first_run_deferred"

    if last_run is None and seeded_at is not None:
        age_hours = (now - seeded_at).total_seconds() / 3600
        if age_hours < interval_hours:
            return False, f"first_run_defer_window ({age_hours:.1f}h / {interval_hours}h)"
    elif last_run is not None:
        age_hours = (now - last_run).total_seconds() / 3600
        if age_hours < interval_hours:
            return False, f"interval_not_reached ({age_hours:.1f}h / {interval_hours}h)"

    # AND idle 條件 — last_chat_at = None 視為「剛初始化, 可以跑」
    if state.last_chat_at is not None:
        idle_hours = (now - state.last_chat_at).total_seconds() / 3600
        if idle_hours < min_idle:
            return False, f"not_idle_enough ({idle_hours:.1f}h / {min_idle}h)"

    return True, "ok"


# ─── Daily light / Weekly deep run ───────────────────────────────────────────


def _recent_daily_flush_files(vault_root: Path, *, max_files: int = 3) -> list[str]:
    """列最近 N 天 daily_flush (預設 3)."""

    root = Path(vault_root).expanduser().resolve()
    flush_dir = root / "11_AI_Mirror/ingestion_logs/daily_flush"
    if not flush_dir.exists():
        return []
    files = [p for p in flush_dir.glob("*.md") if p.is_file() and not p.name.startswith("_")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p.relative_to(root)).replace("\\", "/") for p in files[:max_files]]


def run_light_2h(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """R9 C34: light run (每 2h) — 短→中 aggregate only, 最輕量.

    對齊使用者「2 小時固定機械整理」直覺. 不跑 umbrella / stale 標記 / weekly LLM
    那些都留給 medium / weekly. 目的是「跨頻道對話即時可檢索」感受.
    """

    root = Path(vault_root).expanduser().resolve()
    started_at = _now_local()
    result: dict[str, Any] = {
        "mode": "light_2h",
        "started_at": started_at.isoformat(),
        "dry_run": bool(dry_run),
        "aggregated": [],
        "errors": [],
    }

    # Light 只看「最近 1 天」daily_flush (避免重複工作)
    flushes = _recent_daily_flush_files(root, max_files=1)
    result["scanned_flushes"] = len(flushes)
    for flush_path in flushes:
        if dry_run:
            result["aggregated"].append({"path": flush_path, "skipped": "dry_run"})
            continue
        try:
            agg = aggregate_to_midterm(
                root,
                flush_path,
                session_id=f"curator-light-{started_at.strftime('%Y%m%d-%H%M')}",
            )
            result["aggregated"].append({"path": flush_path, **agg})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"path": flush_path, "error": str(exc)})

    result["ended_at"] = _now_local_iso()
    _append_curator_log(root, result)
    return result


def run_medium_24h(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """R9 C34: medium run (每 24h) — 機械中量整理.

    動作:
    - 短→中 aggregate (近 3 天 daily_flush, 比 light 更全)
    - keyword umbrella consolidation (C20a)
    - USER.md gap scan (C24, 不需 LLM 那段)
    - 標 stale (簡單 metadata 變動, 不 archive 移檔)

    跟 weekly deep 分工: medium 純機械, weekly 加 LLM.
    """

    root = Path(vault_root).expanduser().resolve()
    started_at = _now_local()
    result: dict[str, Any] = {
        "mode": "medium_24h",
        "started_at": started_at.isoformat(),
        "dry_run": bool(dry_run),
        "aggregated": [],
        "errors": [],
    }

    # Step 1: 短→中 aggregate 近 3 天
    flushes = _recent_daily_flush_files(root, max_files=3)
    result["scanned_flushes"] = len(flushes)
    for flush_path in flushes:
        if dry_run:
            result["aggregated"].append({"path": flush_path, "skipped": "dry_run"})
            continue
        try:
            agg = aggregate_to_midterm(
                root,
                flush_path,
                session_id=f"curator-medium-{started_at.date().isoformat()}",
            )
            result["aggregated"].append({"path": flush_path, **agg})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"path": flush_path, "error": str(exc)})

    # Step 2: keyword umbrella consolidation (R7 C20a)
    try:
        umbrella_result = consolidate_umbrella_keyword(root, dry_run=dry_run)
        result["umbrella"] = {
            "groups_count": len(umbrella_result.get("groups", [])),
            "consolidated_count": len(umbrella_result.get("consolidated", [])),
            "consolidated": umbrella_result.get("consolidated", []),
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append({"step": "umbrella", "error": str(exc)})

    # Step 3: USER.md gap scan (R8 C24, 機械 regex + entity 比對)
    try:
        from agent_memory.gap_analysis import scan_user_gaps
        gap_scan = scan_user_gaps(root, cooldown_days=7, min_midterm_mention=3)
        result["user_gaps_scan"] = {
            "new_added_count": len(gap_scan.get("new_added", [])),
            "total_pending": gap_scan.get("total_pending", 0),
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append({"step": "user_gaps_scan", "error": str(exc)})

    result["ended_at"] = _now_local_iso()
    _append_curator_log(root, result)
    return result


# R7 backward-compat alias — 舊 caller 還 call run_daily_light 不會壞
def run_daily_light(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """[Deprecated R9 C34] run_daily_light → run_medium_24h.

    R7 caller backward-compat. 新 caller 用 run_light_2h / run_medium_24h / run_weekly_deep.
    """
    return run_medium_24h(vault_root, dry_run=dry_run)


def run_weekly_deep(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Weekly deep run — 中→長升格 + 90/180d 降級 (C19) + umbrella/skill (C20a/b 接).

    R7 C18+C19 整合: 中→長 promote_midterm_to_long + 長期 demote_long_to_stale_or_archive.
    C20a umbrella + C20b skill 提議 由後續 commit 加進來.
    """

    root = Path(vault_root).expanduser().resolve()
    started_at = _now_local()
    result: dict[str, Any] = {
        "mode": "weekly_deep",
        "started_at": started_at.isoformat(),
        "dry_run": bool(dry_run),
        "steps": {},
    }

    # Step 1: 中→長升格 (C19)
    try:
        promote_result = promote_midterm_to_long(root, dry_run=dry_run)
        result["steps"]["promote_midterm_to_long"] = {
            "promoted_count": len(promote_result.get("promoted", [])),
            "candidates_count": len(promote_result.get("candidates", [])),
            "skipped_count": len(promote_result.get("skipped", [])),
            "thresholds": promote_result.get("thresholds"),
            "promoted": promote_result.get("promoted", []),
        }
    except Exception as exc:  # noqa: BLE001
        result["steps"]["promote_midterm_to_long"] = {"error": str(exc)}

    # Step 2: 長期 stale / archive 降級 (C19)
    try:
        demote_result = demote_long_to_stale_or_archive(root, dry_run=dry_run)
        result["steps"]["demote_long"] = {
            "staled_count": len(demote_result.get("staled", [])),
            "archived_count": len(demote_result.get("archived", [])),
            "skipped_count": len(demote_result.get("skipped", [])),
            "thresholds": demote_result.get("thresholds"),
        }
    except Exception as exc:  # noqa: BLE001
        result["steps"]["demote_long"] = {"error": str(exc)}

    # Step 3: R7 C20b skill 升格提議 scan (寫 .ai/pending_skill_suggestions.json)
    try:
        from agent_memory.skill_suggestions import scan_skill_candidates
        skill_scan = scan_skill_candidates(root, cooldown_days=7)
        result["steps"]["skill_suggestions_scan"] = {
            "new_added_count": len(skill_scan.get("new_added", [])),
            "skipped_count": len(skill_scan.get("skipped", [])),
            "total_pending": skill_scan.get("total_pending", 0),
        }
    except Exception as exc:  # noqa: BLE001
        result["steps"]["skill_suggestions_scan"] = {"error": str(exc)}

    # Step 4 (R8 C24): user gap scan (USER.md 空欄位 + Mid_Term 高頻 entity 不在 USER)
    try:
        from agent_memory.gap_analysis import scan_user_gaps
        gap_scan = scan_user_gaps(root, cooldown_days=7, min_midterm_mention=3)
        result["steps"]["user_gaps_scan"] = {
            "new_added_count": len(gap_scan.get("new_added", [])),
            "skipped_count": len(gap_scan.get("skipped", [])),
            "total_pending": gap_scan.get("total_pending", 0),
        }
    except Exception as exc:  # noqa: BLE001
        result["steps"]["user_gaps_scan"] = {"error": str(exc)}

    # Step 5 (R8 C25): weekly digest (對應 MISSION §3.7 主動歸納)
    try:
        from agent_memory.weekly_digest import generate_weekly_digest
        digest_result = generate_weekly_digest(root)
        result["steps"]["weekly_digest"] = {
            "week_id": digest_result.get("week_id"),
            "digest_path": digest_result.get("digest_path"),
            "stats": digest_result.get("stats"),
        }
    except Exception as exc:  # noqa: BLE001
        result["steps"]["weekly_digest"] = {"error": str(exc)}

    # Step 6 (R9 C27): LLM umbrella + procedure tag detect
    # 對應 MISSION §5.4 LLM 介入「A. Sleep cycle」+ Q5 修補 (skill 自動標 gap)
    # LLM 不可用時 fallback 不影響其他 step (try/except 包好)
    try:
        from agent_memory.umbrella_llm import consolidate_umbrella_with_llm
        llm_result = consolidate_umbrella_with_llm(root)
        result["steps"]["llm_umbrella"] = {
            "scanned": llm_result.get("scanned_entries", 0),
            "merges_added": len(llm_result.get("merges_added", [])),
            "procedure_tags_added": len(llm_result.get("procedure_tags_added", [])),
            "llm_called": llm_result.get("llm_called", False),
            "mock_used": llm_result.get("mock_used", False),
            "error": llm_result.get("error"),
        }
    except Exception as exc:  # noqa: BLE001
        result["steps"]["llm_umbrella"] = {"error": str(exc)}

    result["ended_at"] = _now_local_iso()
    _append_curator_log(root, result)
    return result


def _append_curator_log(vault_root: Path, payload: dict[str, Any]) -> None:
    """Append jsonl 到 11_AI_Mirror/ingestion_logs/curator_runs.jsonl."""

    root = Path(vault_root).expanduser().resolve()
    log_path = root / CURATOR_LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with file_lock(log_path, timeout=5.0):
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        atomic_write(log_path, existing + line)


# ─── Public entry: chat-end hook + force-run ─────────────────────────────────


def record_chat_ended(vault_root: Path) -> None:
    """Chat turn 結束時 call — 只更 last_chat_at, 不 trigger curator (curator 要等 idle)."""

    try:
        state = load_state(vault_root)
        state.last_chat_at = _now_local()
        save_state(vault_root, state)
    except Exception:  # noqa: BLE001
        # 不阻擋對話 — curator state 寫失敗只影響下次 idle 判斷
        pass


def maybe_trigger_curator(vault_root: Path, *, background: bool = True) -> dict[str, Any]:
    """主入口 — chat 結束或 menu [D] 開時 call. check should_run_now 後 fire-and-forget.

    R9 C34: 三層節奏 — light (2h) + medium (24h) + weekly (7d). 每次 chat 結束都 check 三層.
    每層 AND idle 條件不同 (light 30min / medium 1h / weekly 2h) 自然錯開.

    Returns: dict {light_ran, medium_ran, weekly_ran, *_reason, scheduled}
    """

    config = load_config(vault_root)
    state = load_state(vault_root)
    result: dict[str, Any] = {}

    light_ok, light_reason = should_run_now(state, config, "light")
    medium_ok, medium_reason = should_run_now(state, config, "medium")
    weekly_ok, weekly_reason = should_run_now(state, config, "weekly")

    result["light_reason"] = light_reason
    result["medium_reason"] = medium_reason
    result["weekly_reason"] = weekly_reason
    result["light_ran"] = bool(light_ok)
    result["medium_ran"] = bool(medium_ok)
    result["weekly_ran"] = bool(weekly_ok)
    result["scheduled"] = bool(light_ok or medium_ok or weekly_ok)
    # Backward-compat alias
    result["daily_ran"] = bool(medium_ok)
    result["daily_reason"] = medium_reason

    # First-run defer / seeded_at 寫回 (因為 should_run_now 改了 state)
    if "first_run_deferred" in (light_reason, medium_reason, weekly_reason):
        try:
            save_state(vault_root, state)
        except Exception:  # noqa: BLE001
            pass

    if not (light_ok or medium_ok or weekly_ok):
        return result

    def _do_run() -> None:
        new_state = load_state(vault_root)  # 重讀避免兩 thread race
        # 三層按重量級序: light → medium → weekly (medium 跑時順帶 cover light 範圍)
        # 但若三者同時 ok (極少: fresh first window), 只跑最重那層 (weekly 已 cover medium+light)
        if weekly_ok:
            try:
                run_weekly_deep(vault_root)
                new_state.last_weekly_run_at = _now_local()
                new_state.last_medium_run_at = _now_local()  # weekly 順帶覆蓋
                new_state.last_light_run_at = _now_local()
                new_state.consecutive_failures = 0
            except Exception:  # noqa: BLE001
                new_state.consecutive_failures += 1
                new_state.last_failure_at = _now_local()
        elif medium_ok:
            try:
                run_medium_24h(vault_root)
                new_state.last_medium_run_at = _now_local()
                new_state.last_light_run_at = _now_local()  # medium 順帶覆蓋
                new_state.last_daily_run_at = _now_local()  # backward-compat
                new_state.consecutive_failures = 0
            except Exception:  # noqa: BLE001
                new_state.consecutive_failures += 1
                new_state.last_failure_at = _now_local()
        elif light_ok:
            try:
                run_light_2h(vault_root)
                new_state.last_light_run_at = _now_local()
                new_state.consecutive_failures = 0
            except Exception:  # noqa: BLE001
                new_state.consecutive_failures += 1
                new_state.last_failure_at = _now_local()
        try:
            save_state(vault_root, new_state)
        except Exception:  # noqa: BLE001
            pass

    if background:
        threading.Thread(target=_do_run, daemon=True).start()
    else:
        _do_run()

    return result


def force_run(vault_root: Path, mode: str = "medium") -> dict[str, Any]:
    """Force-run skip should_run_now — 給 menu [D] / CLI 用.

    R9 C34: 支援 light / medium / weekly. 'daily' = medium 別名 (backward-compat).
    """

    mode_norm = mode.strip().lower()
    if mode_norm == "daily":
        mode_norm = "medium"

    if mode_norm == "weekly":
        result = run_weekly_deep(vault_root)
        state = load_state(vault_root)
        state.last_weekly_run_at = _now_local()
        save_state(vault_root, state)
        return result
    if mode_norm == "light":
        result = run_light_2h(vault_root)
        state = load_state(vault_root)
        state.last_light_run_at = _now_local()
        save_state(vault_root, state)
        return result
    # medium (預設)
    result = run_medium_24h(vault_root)
    state = load_state(vault_root)
    state.last_medium_run_at = _now_local()
    state.last_light_run_at = _now_local()  # medium 順帶覆蓋
    state.last_daily_run_at = _now_local()  # backward-compat
    save_state(vault_root, state)
    return result
