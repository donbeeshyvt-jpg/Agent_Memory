"""V3 C18e Emotion Contagion — §29.11 H11.

對齊 V3 §29.11 + D-V3-30 + D35-V3 (owner 映射係數 0.4).

觀眾情緒會「傳染」給夥伴, 形成共情:
- owner: 0.4 (最高, 親密)
- VIP viewer (intimacy ≥ 0.4): 0.2
- casual viewer (intimacy ≥ 0.4): 0.1
- 其他: 0.0

自己 affect = (1 - contagion) × own_affect + contagion × viewer_affect
"""

from __future__ import annotations

from agent_memory.companion.affect_manager import AffectState


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def get_contagion_factor(*, is_owner: bool = False, intimacy_score: float = 0.0) -> float:
    """V3 §29.11 + D35-V3."""
    if is_owner:
        return 0.4
    if intimacy_score >= 0.4:
        return 0.2
    if intimacy_score >= 0.2:
        return 0.1
    return 0.0


def apply_contagion(
    own_affect: AffectState,
    viewer_affect: AffectState,
    *,
    is_owner: bool = False,
    intimacy_score: float = 0.0,
) -> AffectState:
    """V3 §29.11: viewer affect 部分映射到自己 affect."""
    factor = get_contagion_factor(is_owner=is_owner, intimacy_score=intimacy_score)
    if factor == 0.0:
        return own_affect

    return AffectState(
        valence=_clamp((1 - factor) * own_affect.valence + factor * viewer_affect.valence, -1.0, 1.0),
        arousal=_clamp((1 - factor) * own_affect.arousal + factor * viewer_affect.arousal, 0.0, 1.0),
        dominance=_clamp((1 - factor) * own_affect.dominance + factor * viewer_affect.dominance, 0.0, 1.0),
        uncertainty=_clamp((1 - factor) * own_affect.uncertainty + factor * viewer_affect.uncertainty, 0.0, 1.0),
    )
