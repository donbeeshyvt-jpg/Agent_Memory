"""hermes bridge outbox queue + bridge_event telemetry (R22 stage 2.1).

stdlib sqlite-based async queue for hermes /chat calls + jsonl telemetry log
for all bridge endpoint events. 零第三方依賴, 對齊 MISSION §5.2 紅線.

Outbox queue (`.ai/hermes-bridge-outbox.db`):
    - schema: id PK / hermes_id / status (pending/sent/dead) / payload (json)
              / attempts / last_error / result (json) / created_at / updated_at
    - enqueue → daemon worker dequeue → processor 處理 → mark_sent / mark_failed
    - retry policy: attempts < MAX_ATTEMPTS 重回 pending; >= 進 dead-letter
    - daemon worker thread, daemon=True, 主程序結束自動清

Telemetry (`.ai/bridge_events.jsonl`):
    - 每個 bridge endpoint call append 一行 jsonl
    - schema: ts / kind (health/retrieve/chat) / hermes_id / status_code / latency_ms / error / extra
    - 線性 append, 不做 rotation (R22 stage 2.1 範圍, rotation 留 stage 3+)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


OUTBOX_DB_RELATIVE = ".ai/hermes-bridge-outbox.db"
TELEMETRY_LOG_RELATIVE = ".ai/bridge_events.jsonl"
MAX_ATTEMPTS = 3
DEFAULT_WORKER_POLL_SECONDS = 2.0


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outbox_db_path(vault_root: Path) -> Path:
    p = Path(vault_root) / OUTBOX_DB_RELATIVE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _telemetry_path(vault_root: Path) -> Path:
    p = Path(vault_root) / TELEMETRY_LOG_RELATIVE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(db_path: Path) -> sqlite3.Connection:
    # isolation_level=None → autocommit, 配合 short-lived `with` block 安全
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hermes_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bridge_outbox_status ON bridge_outbox(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bridge_outbox_hermes ON bridge_outbox(hermes_id)"
    )


class BridgeOutbox:
    """SQLite-backed async queue for bridge /chat calls.

    Thread-safe through sqlite3 internal locking (autocommit + short transactions).
    """

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.db_path = _outbox_db_path(self.vault_root)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)

    def enqueue(self, *, hermes_id: str, payload: dict[str, Any]) -> int:
        """Insert pending row; return new row id."""
        now = _now_utc_iso()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO bridge_outbox
                    (hermes_id, status, payload, attempts, last_error, result, created_at, updated_at)
                VALUES (?, 'pending', ?, 0, '', '', ?, ?)
                """,
                (str(hermes_id or "hermes-default"), payload_json, now, now),
            )
            return int(cur.lastrowid)

    def dequeue_next_pending(self) -> dict[str, Any] | None:
        """Return next pending row (peek; mark via mark_sent/mark_failed)."""
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM bridge_outbox WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def mark_sent(self, row_id: int, *, result: dict[str, Any] | None = None) -> None:
        now = _now_utc_iso()
        result_json = json.dumps(result or {}, ensure_ascii=False)
        with _connect(self.db_path) as conn:
            conn.execute(
                "UPDATE bridge_outbox SET status='sent', result=?, updated_at=? WHERE id=?",
                (result_json, now, int(row_id)),
            )

    def mark_failed(self, row_id: int, *, error: str) -> str:
        """Increment attempts; if attempts >= MAX_ATTEMPTS → status='dead'. Returns new status."""
        now = _now_utc_iso()
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT attempts FROM bridge_outbox WHERE id=?", (int(row_id),)
            ).fetchone()
            if not row:
                return "missing"
            new_attempts = int(row["attempts"]) + 1
            new_status = "dead" if new_attempts >= MAX_ATTEMPTS else "pending"
            conn.execute(
                """
                UPDATE bridge_outbox
                SET status=?, attempts=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (new_status, new_attempts, (error or "")[:1000], now, int(row_id)),
            )
            return new_status

    def get_by_id(self, row_id: int) -> dict[str, Any] | None:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM bridge_outbox WHERE id=?", (int(row_id),)
            ).fetchone()
            return dict(row) if row else None

    def counts_by_status(self) -> dict[str, int]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM bridge_outbox GROUP BY status"
            ).fetchall()
            return {str(r["status"]): int(r["n"]) for r in rows}

    def list_dead(self, limit: int = 50) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM bridge_outbox WHERE status='dead' ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]


class BridgeOutboxWorker:
    """Daemon thread polling outbox + calling processor.

    Lifecycle:
        worker = BridgeOutboxWorker(outbox, processor=fn)
        worker.start()
        ...
        worker.stop()  # graceful stop
    """

    def __init__(
        self,
        outbox: BridgeOutbox,
        processor: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS,
    ) -> None:
        self.outbox = outbox
        self.processor = processor
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bridge-outbox-worker"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                row = self.outbox.dequeue_next_pending()
                if row is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                row_id = int(row["id"])
                try:
                    payload = json.loads(row["payload"])
                    if not isinstance(payload, dict):
                        payload = {}
                except Exception:  # noqa: BLE001
                    payload = {}
                try:
                    result = self.processor(payload)
                    if not isinstance(result, dict):
                        result = {"ok": False, "error": "processor_returned_non_dict"}
                    self.outbox.mark_sent(row_id, result=result)
                except Exception as exc:  # noqa: BLE001
                    self.outbox.mark_failed(row_id, error=str(exc))
            except Exception:  # noqa: BLE001
                # 防 worker thread 整個 die, 下次再 poll
                self._stop.wait(self.poll_seconds)


def write_bridge_event(
    vault_root: Path,
    *,
    kind: str,
    status_code: int,
    latency_ms: float,
    hermes_id: str = "",
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a bridge_event jsonl row to `.ai/bridge_events.jsonl`.

    telemetry 失敗不影響主流程 (swallow exception).
    """
    try:
        path = _telemetry_path(Path(vault_root))
        record: dict[str, Any] = {
            "ts": _now_utc_iso(),
            "kind": str(kind or "unknown"),
            "hermes_id": str(hermes_id or ""),
            "status_code": int(status_code),
            "latency_ms": round(float(latency_ms), 3),
            "error": (str(error or ""))[:500],
        }
        if extra:
            for k, v in extra.items():
                if k in record:
                    continue
                record[k] = v
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        return


def read_bridge_events(vault_root: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Tail last N bridge_event rows from `.ai/bridge_events.jsonl`."""
    path = _telemetry_path(Path(vault_root))
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        return []
    return rows[-int(limit):] if limit > 0 else rows
