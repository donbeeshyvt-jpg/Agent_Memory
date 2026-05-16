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
    """Curator 配置 — 從 promotion.yaml 讀 (缺 → defaults)."""

    daily_interval_hours: float = 24.0
    weekly_interval_hours: float = 168.0  # 7d
    min_idle_hours: float = 2.0
    first_run_defer: bool = True
    paused: bool = False
    circuit_breaker_max_failures: int = 3
    circuit_breaker_cooldown_minutes: float = 60.0

    @classmethod
    def from_yaml(cls, payload: dict) -> "CuratorConfig":
        if not isinstance(payload, dict):
            return cls()
        curator = payload.get("curator", {})
        if not isinstance(curator, dict):
            curator = {}
        return cls(
            daily_interval_hours=float(curator.get("daily_interval_hours", 24.0)),
            weekly_interval_hours=float(curator.get("weekly_interval_hours", 168.0)),
            min_idle_hours=float(curator.get("min_idle_hours", 2.0)),
            first_run_defer=bool(curator.get("first_run_defer", True)),
            paused=bool(curator.get("paused", False)),
            circuit_breaker_max_failures=int(curator.get("circuit_breaker_max_failures", 3)),
            circuit_breaker_cooldown_minutes=float(curator.get("circuit_breaker_cooldown_minutes", 60.0)),
        )


@dataclass(slots=True)
class CuratorState:
    """Curator 持久化 state — 寫到 .ai/curator_state.json (本機時區 ISO)."""

    last_daily_run_at: Optional[datetime] = None
    last_weekly_run_at: Optional[datetime] = None
    last_chat_at: Optional[datetime] = None
    first_daily_seeded_at: Optional[datetime] = None
    first_weekly_seeded_at: Optional[datetime] = None
    consecutive_failures: int = 0
    last_failure_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        def _iso(dt: datetime | None) -> str:
            return dt.isoformat() if dt else ""

        return {
            "last_daily_run_at": _iso(self.last_daily_run_at),
            "last_weekly_run_at": _iso(self.last_weekly_run_at),
            "last_chat_at": _iso(self.last_chat_at),
            "first_daily_seeded_at": _iso(self.first_daily_seeded_at),
            "first_weekly_seeded_at": _iso(self.first_weekly_seeded_at),
            "consecutive_failures": int(self.consecutive_failures),
            "last_failure_at": _iso(self.last_failure_at),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CuratorState":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            last_daily_run_at=_parse_iso(payload.get("last_daily_run_at", "")),
            last_weekly_run_at=_parse_iso(payload.get("last_weekly_run_at", "")),
            last_chat_at=_parse_iso(payload.get("last_chat_at", "")),
            first_daily_seeded_at=_parse_iso(payload.get("first_daily_seeded_at", "")),
            first_weekly_seeded_at=_parse_iso(payload.get("first_weekly_seeded_at", "")),
            consecutive_failures=int(payload.get("consecutive_failures", 0)),
            last_failure_at=_parse_iso(payload.get("last_failure_at", "")),
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
            "daily_interval_hours": 24,
            "weekly_interval_hours": 168,
            "min_idle_hours": 2,
            "first_run_defer": True,
            "use_local_timezone": True,
            "paused": False,
            "circuit_breaker_max_failures": 3,
            "circuit_breaker_cooldown_minutes": 60,
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
    mode: str = "daily",
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """抄 hermes agent/curator.py:198-248 — AND 條件 should_run_now.

    Args:
        state: 當前 CuratorState (注意: first_run_defer 會就地改 seeded_at, caller 要 save_state)
        config: CuratorConfig
        mode: 'daily' | 'weekly'
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

    if mode == "daily":
        last_run = state.last_daily_run_at
        interval_hours = config.daily_interval_hours
        seeded_at = state.first_daily_seeded_at
    elif mode == "weekly":
        last_run = state.last_weekly_run_at
        interval_hours = config.weekly_interval_hours
        seeded_at = state.first_weekly_seeded_at
    else:
        return False, f"unknown_mode: {mode}"

    # First-run defer (hermes 抄): 第一次 call 只 seed 不跑
    if config.first_run_defer and seeded_at is None and last_run is None:
        if mode == "daily":
            state.first_daily_seeded_at = now
        else:
            state.first_weekly_seeded_at = now
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
        if idle_hours < config.min_idle_hours:
            return False, f"not_idle_enough ({idle_hours:.1f}h / {config.min_idle_hours}h)"

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


def run_daily_light(vault_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Daily light run — 短→中 aggregate + 標 stale (C19 接降級).

    R7 C18 階段範圍:
    - 短→中 aggregate (call C17 aggregate_to_midterm) ✓
    - 標 stale (留 C19 demote_long_to_stale_or_archive)
    - keyword umbrella (留 C20a)
    """

    root = Path(vault_root).expanduser().resolve()
    started_at = _now_local()
    result: dict[str, Any] = {
        "mode": "daily_light",
        "started_at": started_at.isoformat(),
        "dry_run": bool(dry_run),
        "aggregated": [],
        "errors": [],
    }

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
                session_id=f"curator-daily-{started_at.date().isoformat()}",
            )
            result["aggregated"].append({"path": flush_path, **agg})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"path": flush_path, "error": str(exc)})

    # R7 C20a: keyword-based umbrella consolidation (daily light step 2)
    try:
        umbrella_result = consolidate_umbrella_keyword(root, dry_run=dry_run)
        result["umbrella"] = {
            "groups_count": len(umbrella_result.get("groups", [])),
            "consolidated_count": len(umbrella_result.get("consolidated", [])),
            "consolidated": umbrella_result.get("consolidated", []),
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append({"step": "umbrella", "error": str(exc)})

    result["ended_at"] = _now_local_iso()
    _append_curator_log(root, result)
    return result


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

    # Step 3-5: umbrella consolidation (C20a) + skill 升格提議 (C20b) — 後續 commit 加

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

    Args:
        vault_root: vault 根目錄
        background: True → 跑背景 thread 不阻擋對話; False → 同步 (給 menu [D] 確認結果用)

    Returns: dict {daily_ran, weekly_ran, daily_reason, weekly_reason, scheduled (bool)}
    """

    config = load_config(vault_root)
    state = load_state(vault_root)
    result: dict[str, Any] = {}

    daily_ok, daily_reason = should_run_now(state, config, "daily")
    weekly_ok, weekly_reason = should_run_now(state, config, "weekly")

    result["daily_reason"] = daily_reason
    result["weekly_reason"] = weekly_reason
    result["daily_ran"] = bool(daily_ok)
    result["weekly_ran"] = bool(weekly_ok)
    result["scheduled"] = bool(daily_ok or weekly_ok)

    # First-run defer / seeded_at 寫回 (因為 should_run_now 改了 state)
    if daily_reason == "first_run_deferred" or weekly_reason == "first_run_deferred":
        try:
            save_state(vault_root, state)
        except Exception:  # noqa: BLE001
            pass

    if not (daily_ok or weekly_ok):
        return result

    def _do_run() -> None:
        new_state = load_state(vault_root)  # 重讀避免兩 thread race
        if daily_ok:
            try:
                run_daily_light(vault_root)
                new_state.last_daily_run_at = _now_local()
                new_state.consecutive_failures = 0
            except Exception:  # noqa: BLE001
                new_state.consecutive_failures += 1
                new_state.last_failure_at = _now_local()
        if weekly_ok:
            try:
                run_weekly_deep(vault_root)
                new_state.last_weekly_run_at = _now_local()
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


def force_run(vault_root: Path, mode: str = "daily") -> dict[str, Any]:
    """Force-run skip should_run_now — 給 menu [D] / CLI 用."""

    if mode == "weekly":
        result = run_weekly_deep(vault_root)
        state = load_state(vault_root)
        state.last_weekly_run_at = _now_local()
        save_state(vault_root, state)
        return result
    result = run_daily_light(vault_root)
    state = load_state(vault_root)
    state.last_daily_run_at = _now_local()
    save_state(vault_root, state)
    return result
