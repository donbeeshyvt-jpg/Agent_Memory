"""Auto-trigger memory promotion after N chat turns.

V2 Phase A C15. 解使用者觀察「短中長期升格應該自動, 不該有手動選項」.

機制: 每次 chat 結束累加 counter, 達門檻 → 背景 subprocess 跑 promote-cycle.
不需要 schtasks 排程, 不阻擋使用者對話 (fire-and-forget thread).

log 寫到 `<vault>/11_AI_Mirror/ingestion_logs/auto_evolve_runs.jsonl`,
每筆: {timestamp, trigger, exit_code, stdout_tail}.

menu [D] daemon 依然保留為「power user 手動 + Windows schtasks 重度排程」用,
但 90% 使用者只用對話模式時, 升格自動發生在 chat 之後.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_COUNTER_FILE = ".ai/chat_counter.txt"
_LOG_REL = "11_AI_Mirror/ingestion_logs/auto_evolve_runs.jsonl"
_DEFAULT_THRESHOLD = 10
# 太頻繁觸發會吃 CPU; 太少又延遲升格. 10 chats / 約 5-15 分鐘觸發一次是平衡點.


def _read_counter(vault_root: Path) -> int:
    f = vault_root / _COUNTER_FILE
    if not f.exists():
        return 0
    try:
        return int((f.read_text(encoding="utf-8") or "0").strip() or "0")
    except Exception:  # noqa: BLE001
        return 0


def _write_counter(vault_root: Path, n: int) -> None:
    f = vault_root / _COUNTER_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(int(n)), encoding="utf-8")


def _log_entry(vault_root: Path, entry: dict[str, Any]) -> None:
    log = vault_root / _LOG_REL
    log.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _spawn_promote_in_background(vault_root: Path) -> None:
    """Fire-and-forget promote-cycle. 不阻擋 chat. 出錯靜默 log."""

    def _run() -> None:
        try:
            cmd = [
                sys.executable, "-X", "utf8", "-m", "agent_memory.cli",
                "--vault-root", str(vault_root),
                "promote-cycle", "--phase", "light",
                "--max-promotions", "10",
                "--json",
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            _log_entry(vault_root, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trigger": "auto_after_chat_threshold",
                "exit_code": int(proc.returncode),
                "stdout_tail": (proc.stdout or "")[-400:],
                "stderr_tail": (proc.stderr or "")[-200:],
            })
        except subprocess.TimeoutExpired:
            _log_entry(vault_root, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trigger": "auto_after_chat_threshold",
                "exit_code": -1,
                "error": "timeout 120s",
            })
        except Exception as exc:  # noqa: BLE001
            try:
                _log_entry(vault_root, {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "trigger": "auto_after_chat_threshold",
                    "exit_code": -2,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_run, daemon=True, name="auto-evolve-promote")
    t.start()


def maybe_trigger_promotion(
    vault_root: Path,
    *,
    threshold: int = _DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Increment chat counter. 達 threshold 就 spawn background promote-cycle.

    Returns:
        {
            "counter": int,      # 累加後計數 (觸發後重置成 0)
            "threshold": int,
            "triggered": bool,   # 這次有沒有觸發 background promote
        }
    """
    try:
        vault_root = Path(vault_root)
        count = _read_counter(vault_root) + 1
        if count >= max(1, int(threshold)):
            _write_counter(vault_root, 0)
            _spawn_promote_in_background(vault_root)
            return {"counter": 0, "threshold": int(threshold), "triggered": True, "previous_count": count}
        _write_counter(vault_root, count)
        return {"counter": count, "threshold": int(threshold), "triggered": False}
    except Exception as exc:  # noqa: BLE001
        # 不阻擋對話流
        return {"counter": -1, "threshold": int(threshold), "triggered": False, "error": str(exc)}
