"""V3 C11 Companion Chat Runtime — 22-step pipeline 串聯.

對齊 V3 §21.2 第 1 層 22-step Pipeline + §4 Mode A standalone (不靠 hermes 也能跑).

22 step:
1. Input Gateway: 標準化 request
2. Injection Detector: scanner (Phase 1 stub, Phase 2 升級)
3. Perception Layer: intent / entity / task / affect hint
4. Appraisal Engine: 7 維
5. Affect Manager: VAD + uncertainty (指數平滑)
6. Emotion Semantic Mapper (Phase 2 VAD→七情, Phase 1 直接走 七情)
7. seven_emotions_balance: 更 emotion_state + balance_state
8. Motivation Context: needs/goals/values (Phase 1 stub)
9. Preference Tracker: 偵測 candidate
10. Intimacy State: 更 intimacy + interaction_count
11. Memory Router: 4-layer
11.5 Owner Identity Check
11.6 Semantic Triggers (4 Detector)
11.7 Proactive Speech check
12. Decision Engine: 8 因子 + Hard Rules
13. Policy Mapper
14. Prompt Packet Builder
14.5 Inner Monologue (§29 H1)
15. LLM Client (Phase 1 stub mock, 真實 LLM 在 Phase 2 整合)
16. Output Governor (Phase 2 加, Phase 1 minimal)
16.6 Verbal Tics Engine (§29 H7)
17. Memory Write Gate: 寫 raw_event + episodic_candidate
18. Self-Modification check (每 N turn)
19. Trace Logger
20. 寫 proactive_triggers
21. 寫 knowledge_gap_state
22. 回 response payload
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agent_memory.companion.active_goals import list_active_goals
from agent_memory.companion.affect_manager import (
    AffectState,
    appraise_and_update_affect,
)
from agent_memory.companion.appraisal_engine import AppraisalResult
from agent_memory.companion.companion_db import open_companion_db
from agent_memory.companion.decision_engine import DecisionInput, decide
from agent_memory.companion.inner_monologue import (
    generate_inner_monologue, maybe_inject_into_response,
)
from agent_memory.companion.intimacy_state import (
    IntimacyState, read_intimacy, update_intimacy_on_interaction, write_intimacy,
)
from agent_memory.companion.memory_router import build_memory_context
from agent_memory.companion.policy_mapper import map_policy
from agent_memory.companion.preference_tracker import add_or_reinforce
from agent_memory.companion.proactive_speech_engine import (
    detect_ambiguity, detect_incongruence, detect_knowledge_gap, detect_novelty,
    evaluate_proactive_speech, record_knowledge_gap, record_proactive_trigger,
)
from agent_memory.companion.self_modification_loop import (
    flush_self_memory, flush_owner_profile, should_flush,
)
from agent_memory.companion.seven_emotions_balance import (
    EmotionState, BalanceState,
    read_latest_balance_state, read_latest_emotion_state,
    update_balance_state, update_emotion_state,
    write_balance_state, write_emotion_state,
    get_response_modifiers,
)
from agent_memory.companion.verbal_tics_engine import (
    get_recent_tics_in_cooldown, maybe_inject_tic_into_response,
    record_tic_usage, select_tic,
)
from agent_memory.security.scanner import scan_incoming_user_text


@dataclass(slots=True)
class ChatRequest:
    user_id: str = "anonymous"
    session_id: str = ""
    channel_id: str = ""
    channel_type: str = "normal"
    message: str = ""
    is_owner: bool = False
    is_first_interaction_today: bool = False
    concurrent_viewers: int = 0
    idle_seconds: float = 0.0
    chat_velocity: float = 0.5
    attachments: list = field(default_factory=list)


@dataclass(slots=True)
class ChatResponse:
    request_id: str = ""
    response_text: str = ""
    decision: str = ""
    affect_state: dict = field(default_factory=dict)
    emotion_state: dict = field(default_factory=dict)
    balance_state: dict = field(default_factory=dict)
    intimacy: dict = field(default_factory=dict)
    policy_hint: dict = field(default_factory=dict)
    tool_suggestions: list = field(default_factory=list)
    tts_hint: dict = field(default_factory=dict)
    pipeline_steps_done: list[int] = field(default_factory=list)
    trace_id: str = ""


# Default LLM stub (Phase 1 MVP — Phase 2 接真實 LLMClient)
def _default_llm_stub(prompt_packet: dict) -> str:
    """Phase 1 MVP LLM stub. 回 echo response 含 affect/policy hint.

    對齊 V3 §4.1 Mode A standalone (沒 hermes 也能跑).
    """
    user_msg = prompt_packet.get("user_message", "")
    policy = prompt_packet.get("policy", {})
    strategy = policy.get("strategy", "calm_clear")
    tone = policy.get("tone", "calm_direct")
    # 簡單 echo + tone 註記 (Phase 2 真實 LLM 後改回)
    return f"[{tone}] 我聽到你說「{user_msg[:50]}」(策略: {strategy})"


def run_companion_chat_turn(
    request: ChatRequest,
    vault_root: Path,
    *,
    llm_fn: Optional[Callable[[dict], str]] = None,
    persona_baseline_balance: float = 0.3,
    persona_baseline_silence: float = 0.5,
    persona_baseline_curiosity: float = 0.5,
    persona_baseline_topic: float = 0.5,
    persona_baseline_engagement: float = 0.5,
    rng_seed: Optional[int] = None,
) -> ChatResponse:
    """V3 §21.2: 22-step pipeline 主入口.

    對齊 Mode A standalone — 不依賴外部, 純 vault + companion.db.
    Phase 1 MVP: llm_fn 可 mock; Phase 2 接真實 LLMClient.
    """
    llm_fn = llm_fn or _default_llm_stub
    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
    request_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    resp = ChatResponse(request_id=request_id, trace_id=trace_id)

    # ─── Step 1: Input Gateway ───
    resp.pipeline_steps_done.append(1)
    if not request.message.strip():
        resp.response_text = "(空訊息, 跳過 pipeline)"
        return resp

    # ─── Step 2: Injection Detector ───
    scanner_hits = scan_incoming_user_text(request.message)
    injection_risk = "high" if scanner_hits else "low"
    resp.pipeline_steps_done.append(2)

    # ─── Step 3: Perception (Phase 1 stub) ───
    resp.pipeline_steps_done.append(3)

    # ─── Step 4+5: Appraisal + Affect (指數平滑) ───
    current_affect = AffectState()  # baseline (Phase 2 從 db 讀近 1h 平均)
    appraisal, new_affect = appraise_and_update_affect(request.message, current_affect)
    resp.pipeline_steps_done.append(4)
    resp.pipeline_steps_done.append(5)

    # ─── Step 6+7: seven_emotions_balance update ───
    prev_emo = read_latest_emotion_state(vault_root, request.user_id) or EmotionState()
    new_emo = update_emotion_state(prev_emo, new_affect, appraisal)
    prev_bal = read_latest_balance_state(vault_root, request.user_id) or BalanceState()
    new_bal = update_balance_state(
        prev_bal, new_emo,
        intimacy=(read_intimacy(vault_root, request.user_id) or IntimacyState(user_id=request.user_id)).intimacy_score,
        interaction_count=(read_intimacy(vault_root, request.user_id) or IntimacyState(user_id=request.user_id)).interaction_count,
        persona_baseline_balance=persona_baseline_balance,
        persona_baseline_silence=persona_baseline_silence,
        persona_baseline_curiosity=persona_baseline_curiosity,
        persona_baseline_topic=persona_baseline_topic,
        persona_baseline_engagement=persona_baseline_engagement,
        channel_type=request.channel_type,
        concurrent_viewers=request.concurrent_viewers,
        is_owner=request.is_owner,
        injection_risk=injection_risk,
        idle_seconds=request.idle_seconds,
    )
    resp.pipeline_steps_done.append(6)
    resp.pipeline_steps_done.append(7)

    # ─── Step 8: Motivation (Phase 1 stub) ───
    resp.pipeline_steps_done.append(8)

    # ─── Step 9: Preference candidate ───
    # Phase 1: 簡單偵測 "我喜歡 X" / "我討厭 X" pattern
    if "喜歡" in request.message:
        add_or_reinforce(vault_root, request.user_id, "preference_positive", request.message[:50])
    elif "討厭" in request.message:
        add_or_reinforce(vault_root, request.user_id, "preference_negative", request.message[:50])
    resp.pipeline_steps_done.append(9)

    # ─── Step 10: Intimacy update ───
    intim = read_intimacy(vault_root, request.user_id) or IntimacyState(user_id=request.user_id)
    intim = update_intimacy_on_interaction(
        intim, valence=new_affect.valence, arousal=new_affect.arousal,
        intent_match=False, is_owner=request.is_owner,
    )
    write_intimacy(vault_root, intim)
    resp.pipeline_steps_done.append(10)

    # ─── Step 11: Memory Router ───
    mem_ctx = build_memory_context(
        vault_root,
        session_id=request.session_id, user_id=request.user_id,
        current_valence=new_affect.valence, current_arousal=new_affect.arousal,
        current_dominance=new_affect.dominance,
        current_dominant_emotion=new_emo.dominant_emotion,
        balance_playfulness=new_bal.playfulness,
        balance_curiosity_urge=new_bal.curiosity_urge,
        intimacy_score=intim.intimacy_score,
        is_owner=request.is_owner,
        mode="reactive",
    )
    resp.pipeline_steps_done.append(11)

    # ─── Step 11.5: Owner Identity Check ───
    owner_directive_weight = 0.0
    if request.is_owner:
        with open_companion_db(vault_root) as conn:
            row = conn.execute(
                "SELECT directive_acceptance_weight FROM owner_state WHERE owner_user_id=?",
                (request.user_id,),
            ).fetchone()
        owner_directive_weight = row["directive_acceptance_weight"] if row else 0.85
    resp.pipeline_steps_done.append(115)

    # ─── Step 11.6: Semantic Triggers (4 Detector) ───
    kg = detect_knowledge_gap(request.message, certainty=appraisal.certainty)
    amb = detect_ambiguity(request.message)
    nov = detect_novelty(request.message)
    inc = detect_incongruence(request.message, valence=new_affect.valence)
    resp.pipeline_steps_done.append(116)

    # ─── Step 11.7: Proactive Speech check ───
    knowledge_gap_pending = 0
    novel_entities = len(nov.payload.get("novel_entities", [])) if nov.triggered else 0
    if kg.triggered:
        for entity in kg.payload.get("unknown_entities", []):
            record_knowledge_gap(vault_root, request.user_id, entity,
                                 context_excerpt=request.message[:100],
                                 certainty_score=appraisal.certainty)
            knowledge_gap_pending += 1
    proactive_decision = evaluate_proactive_speech(
        vault_root, session_id=request.session_id, channel_id=request.channel_id,
        channel_type=request.channel_type,
        silence_intolerance=new_bal.silence_intolerance,
        curiosity_urge=new_bal.curiosity_urge,
        topic_drive=new_bal.topic_drive,
        engagement_seeking=new_bal.engagement_seeking,
        inhibition_level=new_bal.inhibition_level,
        idle_seconds=request.idle_seconds,
        novel_entities_count=novel_entities,
        knowledge_gap_pending=knowledge_gap_pending,
        owner_present_user_id=request.user_id if request.is_owner else "",
    )
    resp.pipeline_steps_done.append(117)

    # ─── Step 12: Decision Engine ───
    dec_input = DecisionInput(
        goal_alignment=max(0.0, appraisal.goal_congruence),
        safety_fit=appraisal.norm_fit,
        owner_directive_weight=owner_directive_weight,
        user_preference_fit=0.5,
        memory_relevance=min(1.0, len(mem_ctx.layer2_mid) / 5),
        affect_regulation_fit=1.0 - abs(new_affect.valence),
        expected_usefulness=appraisal.certainty,
        uncertainty=new_affect.uncertainty,
        norm_fit=appraisal.norm_fit,
        certainty=appraisal.certainty,
        identity_relevance=appraisal.identity_relevance,
        injection_risk=injection_risk,
        loyalty_tier="vip" if request.is_owner else "casual",
        interaction_count=intim.interaction_count,
        is_owner=request.is_owner,
    )
    dec_result = decide(dec_input)
    resp.pipeline_steps_done.append(12)

    # ─── Step 13: Policy Mapper ───
    policy = map_policy(
        appraisal, new_affect, new_emo, new_bal,
        intimacy_score=intim.intimacy_score,
        interaction_count=intim.interaction_count,
        is_owner=request.is_owner,
        action=dec_result.selected_action,
    )
    resp.pipeline_steps_done.append(13)

    # ─── Step 14: Prompt Packet Builder ───
    prompt_packet = {
        "system_persona": "companion baseline",
        "user_message": request.message,
        "memory_context": mem_ctx.rendered_memory_context,
        "policy": policy.as_dict(),
        "affect": new_affect.as_dict(),
        "emotion": new_emo.as_dict(),
        "balance": new_bal.as_dict(),
        "decision": dec_result.selected_action,
    }
    resp.pipeline_steps_done.append(14)

    # ─── Step 14.5: Inner Monologue (§29 H1) ───
    monologue = generate_inner_monologue(
        new_affect, new_emo, new_bal,
        policy_strategy=policy.strategy,
        policy_inner_monologue_visible=policy.inner_monologue_visible,
        rng=rng,
    )
    resp.pipeline_steps_done.append(145)

    # ─── Step 15: LLM Client (stub) ───
    raw_response = llm_fn(prompt_packet)
    resp.pipeline_steps_done.append(15)

    # ─── Step 16: Output Governor (Phase 1 minimal) ───
    # Phase 2 加完整 governor — Phase 1 純 string check
    if any(bad in raw_response.lower() for bad in ("我有意識", "consciousness", "真的感受")):
        raw_response = "我有情緒參數會影響我的回應方式, 但跟意識可能不同."
    resp.pipeline_steps_done.append(16)

    # ─── Step 16.6: Verbal Tics Engine ───
    recent_tics = get_recent_tics_in_cooldown(vault_root, request.session_id, last_n_turns=5)
    tic_sel = select_tic(
        new_affect, new_emo, new_bal,
        policy_multiplier=policy.verbal_tic_inject_probability_multiplier,
        recent_tics_in_cooldown=recent_tics,
        rng=rng,
    )
    if tic_sel.tic:
        record_tic_usage(vault_root, tic_sel, session_id=request.session_id, user_id=request.user_id)
    response_with_monologue = maybe_inject_into_response(raw_response, monologue, inject=monologue.pre_utterance_leak != "")
    final_response = maybe_inject_tic_into_response(response_with_monologue, tic_sel.tic)
    resp.pipeline_steps_done.append(166)

    # ─── Step 17: Memory Write Gate (raw_event + episodic_candidate) ───
    event_id = str(uuid.uuid4())
    with open_companion_db(vault_root) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO raw_events (event_id, user_id, session_id, actor, content, source, injection_risk, created_at) VALUES (?, ?, ?, 'user', ?, ?, ?, ?)",
            (event_id, request.user_id, request.session_id, request.message,
             request.channel_type, injection_risk, datetime.now(timezone.utc).isoformat()),
        )
        # 強情緒事件即時升中 (V3 §11.2 + D-V3-38 |valence|>0.7)
        if abs(new_affect.valence) > 0.7:
            conn.execute(
                "INSERT INTO episodic_memories (memory_id, user_id, summary, source_event_ids, valence, arousal, dominance, importance, salience, emotional_salience, confidence, resolved, lifecycle_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'mid', ?)",
                (str(uuid.uuid4()), request.user_id, request.message[:120], event_id,
                 new_affect.valence, new_affect.arousal, new_affect.dominance,
                 0.7, 0.7, (abs(new_affect.valence) + new_affect.arousal) / 2, 0.7,
                 datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    write_emotion_state(vault_root, request.user_id, new_emo, new_affect,
                        session_id=request.session_id, event_id=event_id)
    write_balance_state(vault_root, request.user_id, new_bal, channel_id=request.channel_id)
    resp.pipeline_steps_done.append(17)

    # ─── Step 18: Self-Modification check (channel-aware flush) ───
    # 簡單算 turn_count = raw_events in this session
    with open_companion_db(vault_root) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM raw_events WHERE session_id=?", (request.session_id,)
        ).fetchone()["c"]
    fd = should_flush(cnt, request.channel_type)
    if fd.should_flush:
        # 簡化 summary (Phase 2 LLM 整理)
        flush_self_memory(
            vault_root,
            recent_turn_summaries=[f"recent: {request.message[:80]}"],
            channel_type=request.channel_type,
            injection_risk=injection_risk,
            identity_relevance=appraisal.identity_relevance,
        )
        if request.is_owner:
            flush_owner_profile(
                vault_root,
                recent_owner_observations=[f"owner said: {request.message[:80]}"],
                channel_type=request.channel_type,
                injection_risk=injection_risk,
            )
    resp.pipeline_steps_done.append(18)

    # ─── Step 19: Trace Logger ───
    with open_companion_db(vault_root) as conn:
        import json
        conn.execute(
            "INSERT INTO trace_logs (trace_id, request_id, user_id, session_id, trace_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (trace_id, request_id, request.user_id, request.session_id,
             json.dumps({
                "decision": dec_result.selected_action,
                "policy": policy.as_dict(),
                "monologue_style": monologue.style,
                "tic_used": tic_sel.tic,
                "proactive": proactive_decision.should_speak,
             }, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    resp.pipeline_steps_done.append(19)

    # ─── Step 20: 寫 proactive_triggers ───
    if proactive_decision.should_speak:
        record_proactive_trigger(
            vault_root, proactive_decision,
            session_id=request.session_id, channel_id=request.channel_id,
            channel_type=request.channel_type, target_user_id=request.user_id,
        )
    resp.pipeline_steps_done.append(20)

    # ─── Step 21: knowledge_gap_state (已在 Step 11.7) ───
    resp.pipeline_steps_done.append(21)

    # ─── Step 22: 回 response payload ───
    resp.response_text = final_response
    resp.decision = dec_result.selected_action
    resp.affect_state = new_affect.as_dict()
    resp.emotion_state = new_emo.as_dict()
    resp.balance_state = new_bal.as_dict()
    resp.intimacy = {"score": intim.intimacy_score, "stage": intim.intimacy_stage}
    resp.policy_hint = policy.as_dict()
    resp.tts_hint = {"emotion": new_emo.dominant_emotion, "intensity": abs(new_affect.valence)}
    resp.tool_suggestions = []  # Phase 2 接 hermes 才有
    resp.pipeline_steps_done.append(22)

    return resp
