"""V3 Companion SQLite 29 表 schema + migration.

對齊 V3_夥伴大腦_新規劃_2026-05-25.md §6 (20 表 + §29 補強 4 表 + §26.2.E 1 表 = 25+ 表).
本實作精簡到 29 表 (含 §29.13 補強表).

路徑: {vault_root}/.ai/companion.db
跟 sqlite-index.db (管家 R7 共用) 並列, 不同 file.

Schema 永久綁定 + migration: 每次 ensure_companion_db 跑 CREATE TABLE IF NOT EXISTS.
不破壞既有 — 純新增表 + 純新增欄位 (ALTER TABLE ADD COLUMN if not exists).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_COMPANION_DB_FILENAME = "companion.db"

# ─── 29 表 schema (對齊 V3 規劃書 §6 + §29 + §26.2) ──────────────────────
_SCHEMA_STATEMENTS = [
    # ─── §6.1 基礎 (4) ───
    """CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        display_name TEXT,
        role TEXT DEFAULT 'audience',  -- owner / audience / moderator / guest
        loyalty_tier TEXT DEFAULT 'casual',  -- casual / regular / vip / banned
        is_banned INTEGER DEFAULT 0,
        first_seen_at TEXT,
        last_seen_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS raw_events (
        event_id TEXT PRIMARY KEY,
        user_id TEXT, session_id TEXT, actor TEXT,
        content TEXT, source TEXT,
        trusted INTEGER DEFAULT 1, privacy_level TEXT,
        injection_risk TEXT DEFAULT 'low',
        attention_score REAL,
        hash TEXT, created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        live_stream_id TEXT, started_at TEXT, ended_at TEXT,
        channel_id TEXT, channel_type TEXT,
        topic TEXT, concurrent_viewers_max INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS trace_logs (
        trace_id TEXT PRIMARY KEY,
        request_id TEXT, user_id TEXT, session_id TEXT,
        trace_json TEXT, created_at TEXT
    )""",
    # ─── §6.2 情緒層 (5) ───
    """CREATE TABLE IF NOT EXISTS affect_states (
        state_id TEXT PRIMARY KEY,
        user_id TEXT, session_id TEXT,
        valence REAL, arousal REAL, dominance REAL, uncertainty REAL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS appraisal_records (
        appraisal_id TEXT PRIMARY KEY,
        user_id TEXT, event_id TEXT,
        novelty REAL, goal_congruence REAL, control REAL, certainty REAL,
        norm_fit REAL, identity_relevance REAL, relationship_impact REAL,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS emotion_state (
        user_id TEXT NOT NULL, timestamp TEXT NOT NULL,
        joy REAL DEFAULT 0.5, anger REAL DEFAULT 0.0,
        sadness REAL DEFAULT 0.0, fear REAL DEFAULT 0.0,
        love REAL DEFAULT 0.0, disgust REAL DEFAULT 0.0, desire REAL DEFAULT 0.0,
        dominant_emotion TEXT,
        valence REAL, arousal REAL, dominance REAL, uncertainty REAL,
        trigger_session_id TEXT, trigger_event_id TEXT,
        PRIMARY KEY (user_id, timestamp)
    )""",
    """CREATE TABLE IF NOT EXISTS balance_state (
        user_id TEXT NOT NULL, timestamp TEXT NOT NULL,
        balance_axis REAL DEFAULT 0.0,
        playfulness REAL DEFAULT 0.0, mischief REAL DEFAULT 0.0,
        whimsy REAL DEFAULT 0.0, impulsivity REAL DEFAULT 0.0,
        silence_intolerance REAL DEFAULT 0.3, curiosity_urge REAL DEFAULT 0.3,
        topic_drive REAL DEFAULT 0.3, engagement_seeking REAL DEFAULT 0.3,
        p_off_topic_joke REAL DEFAULT 0.0, p_provocative REAL DEFAULT 0.0,
        p_random_callback REAL DEFAULT 0.0, p_whimsy_suggest REAL DEFAULT 0.0,
        inhibition_level REAL DEFAULT 1.0,
        channel_id TEXT, trigger_event TEXT,
        PRIMARY KEY (user_id, timestamp)
    )""",
    # ⭐ V3-H4 殘-07 (user 2026-05-27 audit Plan B 拍板選 A): 廢 emotion_distribution 表
    # V3-G3 已用 emotion_state + balance_state 完整 cover, 此表為早期 V3 設計被新表取代.
    # 移除 CREATE TABLE, 同時加 DROP 對既有 db 清理 (見下方 _drop_legacy_tables).
    # ─── §6.3 動機 / 偏好 / 親密度 (3) ───
    """CREATE TABLE IF NOT EXISTS motivation_contexts (
        context_id TEXT PRIMARY KEY,
        user_id TEXT, needs_json TEXT, goals_json TEXT, values_json TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS preference_memories (
        preference_id TEXT PRIMARY KEY,
        user_id TEXT, preference_type TEXT, claim TEXT,
        scope TEXT, strength REAL, confidence REAL,
        evidence_count INTEGER DEFAULT 1, contradiction_count INTEGER DEFAULT 0,
        derived_from TEXT, status TEXT DEFAULT 'working',
        first_seen_at TEXT, last_seen_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS intimacy_states (
        user_id TEXT PRIMARY KEY,
        interaction_count INTEGER DEFAULT 0,
        emotional_resonance_density REAL DEFAULT 0.0,
        narrative_identification REAL DEFAULT 0.0,
        intimacy_score REAL DEFAULT 0.0,
        intimacy_stage TEXT DEFAULT '初識',
        last_interaction_at TEXT
    )""",
    # ─── §6.4 決策 / 記憶 (4) ───
    """CREATE TABLE IF NOT EXISTS decision_scores (
        score_id TEXT PRIMARY KEY,
        request_id TEXT, user_id TEXT,
        candidate_actions_json TEXT, selected_action TEXT,
        score_json TEXT, hard_rules_json TEXT, reason TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS episodic_memories (
        memory_id TEXT PRIMARY KEY,
        user_id TEXT, summary TEXT, source_event_ids TEXT,
        valence REAL, arousal REAL, dominance REAL,
        importance REAL DEFAULT 0.5, salience REAL DEFAULT 0.5,
        emotional_salience REAL,  -- §11.3 (|valence|+arousal)/2
        confidence REAL DEFAULT 0.5, resolved INTEGER DEFAULT 0,
        lifecycle_state TEXT DEFAULT 'short',
        valid_from TEXT, valid_to TEXT, created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS semantic_memories (
        memory_id TEXT PRIMARY KEY,
        user_id TEXT, claim TEXT, derived_from TEXT,
        confidence REAL, evidence_count INTEGER DEFAULT 1,
        contradiction_count INTEGER DEFAULT 0,
        tags TEXT, status TEXT DEFAULT 'episodic',
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS narrative_memories (
        narrative_id TEXT PRIMARY KEY,
        user_id TEXT, theme TEXT, events_chain_json TEXT,
        relationship_arc TEXT,
        emotional_arc_json TEXT,  -- §13.7 {start_valence, peak_valence, end_valence}
        created_at TEXT
    )""",
    # ─── §6.5 人格 / 安全 (4) ───
    """CREATE TABLE IF NOT EXISTS persona_versions (
        version_id TEXT PRIMARY KEY,
        version INTEGER, persona_json TEXT,
        affect_baseline_json TEXT, derived_from TEXT,
        drift_score REAL, approved INTEGER DEFAULT 0, active INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS trait_evolution (
        user_id TEXT NOT NULL, trait_name TEXT NOT NULL,
        evidence_count INTEGER DEFAULT 0, events_json TEXT,
        current_value REAL, proposed_value REAL,
        awaiting_drift_guard INTEGER DEFAULT 0,
        last_updated_at TEXT,
        PRIMARY KEY (user_id, trait_name)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_audit_logs (
        audit_id TEXT PRIMARY KEY,
        action TEXT, target_type TEXT, target_id TEXT,
        reason TEXT, before_json TEXT, after_json TEXT,
        trace_id TEXT, created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS injection_detected (
        detected_id TEXT PRIMARY KEY,
        user_id TEXT, event_id TEXT,
        pattern_matched TEXT, risk_score REAL,
        action_taken TEXT, created_at TEXT
    )""",
    # ─── §6.6 主動 / Owner / Recall Cache (4) ───
    """CREATE TABLE IF NOT EXISTS owner_state (
        owner_user_id TEXT PRIMARY KEY,
        soul_path TEXT, directive_acceptance_weight REAL DEFAULT 0.85,
        relationship_label TEXT,
        total_directive_count INTEGER DEFAULT 0,
        directive_accepted_count INTEGER DEFAULT 0,
        last_interaction_at TEXT, last_drift_check_at TEXT,
        notes TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS proactive_triggers (
        trigger_id TEXT PRIMARY KEY,
        session_id TEXT, channel_id TEXT, channel_type TEXT,
        target_user_id TEXT, trigger_type TEXT,
        trigger_score REAL, threshold_used REAL,
        context_json TEXT, action_taken TEXT,
        response_received_within_60s INTEGER DEFAULT 0,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_gap_state (
        gap_id TEXT PRIMARY KEY,
        user_id TEXT, entity TEXT, context_excerpt TEXT,
        certainty_score REAL,
        asked_count INTEGER DEFAULT 0,
        answered INTEGER DEFAULT 0, answered_at TEXT,
        resolved INTEGER DEFAULT 0, knowledge_path TEXT,
        last_seen_at TEXT, created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS memory_recall_cache (
        cache_key TEXT PRIMARY KEY,
        recall_result_json TEXT, expires_at TEXT
    )""",
    # ─── §26.2.E 流量模式 (1) ───
    """CREATE TABLE IF NOT EXISTS flow_mode_history (
        mode_id TEXT PRIMARY KEY,
        session_id TEXT, mode TEXT,  -- burst / normal / dead_chat / owner_solo
        started_at TEXT, ended_at TEXT,
        chat_velocity_avg REAL, concurrent_viewers_avg INTEGER,
        backlog_count INTEGER DEFAULT 0,
        backlog_processed INTEGER DEFAULT 0,
        backlog_dropped INTEGER DEFAULT 0,
        triggered_proactive_count INTEGER DEFAULT 0,
        transition_reason TEXT
    )""",
    # ─── §29 「活起來」補強表 (4) ───
    """CREATE TABLE IF NOT EXISTS active_goals (
        goal_id TEXT PRIMARY KEY,
        description TEXT NOT NULL, source TEXT,
        importance REAL DEFAULT 0.5,
        created_at TEXT, last_pursued_at TEXT,
        pursuit_count INTEGER DEFAULT 0,
        target_audience TEXT,
        status TEXT DEFAULT 'active',
        related_memory_ids TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS embodied_state (
        state_id TEXT PRIMARY KEY,
        timestamp TEXT,
        energy REAL DEFAULT 0.8, hunger REAL DEFAULT 0.0,
        thirst REAL DEFAULT 0.0, sleepiness REAL DEFAULT 0.0,
        voice_strain REAL DEFAULT 0.0,
        stream_duration_minutes INTEGER DEFAULT 0,
        triggered_state TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS verbal_tics_history (
        tic_event_id TEXT PRIMARY KEY,
        session_id TEXT, user_id TEXT,
        tic TEXT, trigger_condition TEXT,
        actual_probability REAL, used INTEGER DEFAULT 1,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS expectation_state (
        expectation_id TEXT PRIMARY KEY,
        session_id TEXT, metric TEXT,
        expected_value REAL, actual_value REAL, delta REAL,
        affect_impact_json TEXT,
        timestamp TEXT
    )""",
]

# 對應 INDEX (常用 query 加速)
_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_raw_events_session ON raw_events(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_raw_events_user ON raw_events(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_emotion_state_user_recent ON emotion_state(user_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_balance_state_user_recent ON balance_state(user_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_episodic_user_lifecycle ON episodic_memories(user_id, lifecycle_state, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_episodic_emotional_salience ON episodic_memories(emotional_salience DESC)",
    "CREATE INDEX IF NOT EXISTS idx_intimacy_score ON intimacy_states(intimacy_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_preference_status ON preference_memories(user_id, status, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_proactive_session ON proactive_triggers(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_proactive_target ON proactive_triggers(target_user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_gap_user_resolved ON knowledge_gap_state(user_id, resolved)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_gap_asked ON knowledge_gap_state(asked_count)",
    "CREATE INDEX IF NOT EXISTS idx_flow_mode_session ON flow_mode_history(session_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_active_goals_status ON active_goals(status, importance DESC)",
    "CREATE INDEX IF NOT EXISTS idx_verbal_tics_session ON verbal_tics_history(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_expectation_session ON expectation_state(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_trace_logs_user ON trace_logs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_trait_evolution_user ON trait_evolution(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_audit_target ON memory_audit_logs(target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_injection_user ON injection_detected(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_owner_last ON owner_state(last_interaction_at)",
]


def get_companion_db_path(vault_root: Path) -> Path:
    """Return path to companion.db in vault. {vault_root}/.ai/companion.db."""
    return Path(vault_root).expanduser().resolve() / ".ai" / _COMPANION_DB_FILENAME


_DROP_LEGACY_TABLES = [
    # V3-H4 殘-07: 廢 emotion_distribution (被 emotion_state + balance_state 取代)
    "DROP TABLE IF EXISTS emotion_distribution",
]


def ensure_companion_db(vault_root: Path) -> Path:
    """V3 C5+H4: 建 companion.db + 28 表 schema + INDEX. Idempotent — 重跑 no-op.

    V3-H4: 29→28 表 (廢 emotion_distribution, dead schema).
    """
    db_path = get_companion_db_path(vault_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(stmt)
        for stmt in _INDEX_STATEMENTS:
            conn.execute(stmt)
        # V3-H4: 對既有 db 清廢表 (對新 db 無影響, DROP IF EXISTS idempotent)
        for stmt in _DROP_LEGACY_TABLES:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()
    return db_path


@contextmanager
def open_companion_db(vault_root: Path) -> Iterator[sqlite3.Connection]:
    """Context manager 開 companion.db (auto-ensure schema)."""
    db_path = ensure_companion_db(vault_root)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def list_table_names(vault_root: Path) -> list[str]:
    """V3 C5: helper — 列 companion.db 內所有 table 名 (給 e2e 驗收用)."""
    with open_companion_db(vault_root) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]
