# -*- coding: utf-8 -*-
"""Dump 最新「路人(非 owner)」turn 的真實 system prompt。
看 viewer 回話的 prompt 經過什麼 / 奇怪口吻來源。
跑: cd test/agent-memory-core; python -X utf8 scripts/_dump_viewer_prompt.py
"""
import sys, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VAULT = Path(r"Z:/Cursor練習用/Agent_Memory/test/SecondBrains/companion_test")
OWNER = "1264637379789197342"

conn = sqlite3.connect(str(VAULT / ".ai" / "companion.db"))
conn.row_factory = sqlite3.Row
r = conn.execute(
    'SELECT user_id, session_id, content FROM raw_events WHERE actor="user" ORDER BY created_at DESC LIMIT 1',
).fetchone()
if not r:
    print("(無 user raw_event)")
    sys.exit()
vuid, sess, msg = r["user_id"], r["session_id"], r["content"]
IS_OWNER = (str(vuid) == OWNER)
emo = dict(conn.execute('SELECT * FROM emotion_state WHERE user_id=? ORDER BY rowid DESC LIMIT 1', (vuid,)).fetchone())
brow = conn.execute('SELECT * FROM balance_state WHERE user_id=? ORDER BY rowid DESC LIMIT 1', (vuid,)).fetchone()
bal = dict(brow) if brow else {}
irow = conn.execute('SELECT * FROM intimacy_states WHERE user_id=?', (vuid,)).fetchone()
intim = dict(irow) if irow else {"intimacy_score": 0.0}
conn.close()

from agent_memory.companion.companion_chat_runtime import _build_companion_system_prompt, _load_viewer_dynamic_context, _load_recent_history
from agent_memory.companion.memory_router import build_memory_context

mc = build_memory_context(
    VAULT, session_id=sess, user_id=vuid,
    current_valence=0.05, current_arousal=0.6,
    current_dominant_emotion=emo.get("dominant_emotion", "joy"),
    intimacy_score=intim.get("intimacy_score", 0.0), is_owner=IS_OWNER,
)
vctx = _load_viewer_dynamic_context(VAULT, vuid)
packet = {
    "affect": {"valence": 0.05, "arousal": 0.6, "dominance": 0.5, "uncertainty": 0.4},
    "emotion": {k: emo[k] for k in ("joy", "anger", "sadness", "fear", "love", "disgust") if k in emo},
    "balance": {k: bal[k] for k in bal},
    "policy": {"strategy": "playful_brief", "tone": "casual_polite", "intimacy_score": intim.get("intimacy_score", 0.0), "is_owner": False},
    "decision": "ALLOW_PLAYFUL",
    "memory_context": mc.rendered_memory_context,
    "user_message": msg,
    "is_owner": IS_OWNER,
}
try:
    hist = _load_recent_history(VAULT, user_id=vuid, session_id=sess, max_turns=12)
    packet["recent_history"] = hist
except Exception as e:
    print(f"[warn] _load_recent_history failed: {e}")
sp = _build_companion_system_prompt(packet, vault_root=VAULT, viewer_profile_context=vctx)
print("=" * 70)
print(f"VIEWER={vuid}  MSG={msg!r}  intim={intim.get('intimacy_score')}")
print("=" * 70)
print(sp)
print("=" * 70)
print(f"[system prompt 長度] {len(sp)} chars")
