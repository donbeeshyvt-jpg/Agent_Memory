"""V3 Companion 大腦 — 對齊 V3_夥伴大腦_新規劃_2026-05-25.md.

Phase 1 MVP 模組:
- companion_db: SQLite 29 表 schema + migration
- appraisal_engine: 7 維 appraisal (Phase 1)
- affect_manager: VAD 指數平滑 (Phase 1)
- seven_emotions_balance: 七情 + 天平 8 子軸 (Phase 1)
- decision_engine: 8 因子 + H1-H9 (Phase 1)
- policy_mapper: strategy + tone + memory_bias (Phase 1)
- intimacy_state: 5 階段 (Phase 1)
- preference_tracker: Working/Episodic (Phase 1)
- inner_monologue: §29 H1 (Phase 1)
- active_goals: §29 H2 (Phase 1)
- verbal_tics_engine: §29 H7 (Phase 1)
- memory_router: 4-layer + emotion-modulated recall (Phase 1)
- self_modification_loop: 自寫 MEMORY/USER (Phase 1)
- proactive_speech_engine: 4 Detector (Phase 1)
- companion_chat_runtime: 22-step pipeline (Phase 1)

Phase 2-3 後續加 (api_server / multi_user_router / obsidian_watcher / curator 等).
"""

from agent_memory.companion.companion_db import (
    ensure_companion_db,
    get_companion_db_path,
    open_companion_db,
)

__all__ = [
    "ensure_companion_db",
    "get_companion_db_path",
    "open_companion_db",
]
