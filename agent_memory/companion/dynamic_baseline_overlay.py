"""V3-O.10 #35 — Dynamic Baseline Overlay (反思 → 性格自動演化).

設計:
  effective_baseline = SOUL.baseline + overlay.delta

- SOUL.baseline 不動 (人類鎖定)
- overlay.delta 由反思 LLM 推導, 以小步幅累積
- 6 層安全機制:
  1. 單次 delta ≤ ±0.05
  2. 累積 delta ≤ ±0.40
  3. LLM confidence ≥ 0.60 才採納
  4. 同向 evidence ≥ 3 次才生效
  5. 90 天 decay (無 signal 歸零)
  6. SOUL pinned_traits 保護 (yaml 配置)

觸發: Step 18 self_mod flush 寫完 00.07 後
升格: overlay 穩定後寫進 SOUL.dynamic_sections (V3-O.10 #40, 由 #40 負責)
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


OVERLAY_RELPATH = (".ai", "dynamic_baseline_overlay.json")

_AXES = [
    "engagement_seeking", "silence_intolerance", "curiosity_urge",
    "topic_drive", "baseline_balance",
]

# V3-O.10 ISSUE-3 fix: personality_switcher 的 soul_baselines 對「沉默不耐」這軸
# 用的 key 是 baseline_silence_intolerance, 但 overlay store / _AXES 用 silence_intolerance.
# 不對照的話 get_effective_baselines 會查不到該軸 → silence_intolerance 的 overlay delta
# 被默默丟掉 (其餘 4 軸 key 剛好相符故正常). 此處把 soul key 映回 overlay 軸名.
# 注意: 不可用 strip "baseline_" 前綴的做法, 因 baseline_balance 在 _AXES 也帶前綴會錯切成 balance.
_SOUL_KEY_TO_AXIS = {
    "baseline_silence_intolerance": "silence_intolerance",
}

_MAX_DELTA = 0.4
_STEP_SIZE = 0.05
_CONFIDENCE_THRESHOLD = 0.6
_EVIDENCE_THRESHOLD = 3
_DECAY_DAYS = 90


@dataclass
class AxisOverlay:
    delta: float = 0.0
    evidence_count: int = 0
    last_direction: int = 0  # +1 / -1 / 0
    last_updated: str = ""
    history: list[dict] = field(default_factory=list)


@dataclass
class DynamicBaseline:
    axes: dict[str, AxisOverlay] = field(default_factory=dict)
    schema_version: int = 1

    def get_effective(self, soul_value: float, axis: str) -> float:
        ax = self.axes.get(axis)
        if ax is None:
            return soul_value
        return max(0.0, min(1.0, soul_value + ax.delta))


class DynamicBaselineOverlay:
    """管理 overlay store + 讀寫 + 安全機制."""

    def __init__(self, vault_root: Path, *, config: dict | None = None):
        self.vault_root = vault_root
        cfg = config or {}
        self.max_delta = float(cfg.get("max_delta_per_axis", _MAX_DELTA))
        self.step_size = float(cfg.get("delta_step_size", _STEP_SIZE))
        self.confidence_threshold = float(cfg.get("confidence_threshold", _CONFIDENCE_THRESHOLD))
        self.evidence_threshold = int(cfg.get("evidence_threshold", _EVIDENCE_THRESHOLD))
        self.decay_days = int(cfg.get("decay_to_zero_days", _DECAY_DAYS))
        self.pinned_traits = set(cfg.get("pinned_traits", []) or [])
        self._lock = threading.Lock()

    def _path(self) -> Path:
        return self.vault_root / OVERLAY_RELPATH[0] / OVERLAY_RELPATH[1]

    def load(self) -> DynamicBaseline:
        p = self._path()
        if not p.exists():
            return DynamicBaseline()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            axes = {}
            for k, v in data.get("axes", {}).items():
                axes[k] = AxisOverlay(
                    delta=float(v.get("delta", 0.0)),
                    evidence_count=int(v.get("evidence_count", 0)),
                    last_direction=int(v.get("last_direction", 0)),
                    last_updated=str(v.get("last_updated", "")),
                    history=list(v.get("history", []))[-20:],
                )
            return DynamicBaseline(axes=axes, schema_version=int(data.get("schema_version", 1)))
        except Exception:
            return DynamicBaseline()

    def save(self, baseline: DynamicBaseline) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": baseline.schema_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "axes": {
                k: {
                    "delta": round(v.delta, 4),
                    "evidence_count": v.evidence_count,
                    "last_direction": v.last_direction,
                    "last_updated": v.last_updated,
                    "history": v.history[-20:],
                }
                for k, v in baseline.axes.items()
            },
        }
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def apply_decay(self, baseline: DynamicBaseline) -> DynamicBaseline:
        """90 天 decay — 無 signal 時 delta 歸零."""
        now = datetime.now(timezone.utc)
        for ax in baseline.axes.values():
            if not ax.last_updated:
                continue
            try:
                last = datetime.fromisoformat(ax.last_updated)
                days_ago = (now - last).days
                if days_ago >= self.decay_days:
                    ax.delta = 0.0
                    ax.evidence_count = 0
            except Exception:
                pass
        return baseline

    def apply_llm_delta(
        self,
        axis: str,
        direction: int,  # +1 or -1
        confidence: float,
        reason: str = "",
    ) -> bool:
        """嘗試套用一次 LLM 推導的 delta.

        Returns True 若 delta 有更新.
        """
        if axis in self.pinned_traits:
            return False
        if abs(confidence) < self.confidence_threshold:
            return False
        if direction not in (1, -1):
            return False

        with self._lock:
            baseline = self.load()
            if axis not in baseline.axes:
                baseline.axes[axis] = AxisOverlay()
            ax = baseline.axes[axis]

            # Q4 answer: 方向衝突時 counter 歸零
            if ax.last_direction != 0 and ax.last_direction != direction:
                ax.evidence_count = 0

            ax.evidence_count += 1
            ax.last_direction = direction
            ax.last_updated = datetime.now(timezone.utc).isoformat()
            ax.history.append({
                "at": ax.last_updated,
                "direction": direction,
                "confidence": round(confidence, 3),
                "reason": reason[:100],
            })

            # evidence ≥ threshold 才生效
            if ax.evidence_count < self.evidence_threshold:
                self.save(baseline)
                return False

            # 更新 delta
            proposed = ax.delta + direction * self.step_size
            ax.delta = max(-self.max_delta, min(self.max_delta, proposed))
            self.save(baseline)
            return True

    def get_effective_baselines(self, soul_baselines: dict[str, float]) -> dict[str, float]:
        """計算所有軸的 effective_baseline = SOUL + overlay.delta."""
        baseline = self.apply_decay(self.load())
        result = {}
        for axis, soul_val in soul_baselines.items():
            # V3-O.10 ISSUE-3 fix: soul key (e.g. baseline_silence_intolerance)
            # 映回 overlay 軸名 (silence_intolerance) 再查, 否則該軸 delta 失效.
            overlay_axis = _SOUL_KEY_TO_AXIS.get(axis, axis)
            result[axis] = baseline.get_effective(soul_val, overlay_axis)
        return result

    def derive_delta_from_reflection(self, reflection_text: str) -> list[dict]:
        """從反思文字用 LLM 推導 axis delta 建議.

        Returns list of {"axis": str, "direction": int, "confidence": float, "reason": str}
        """
        if not reflection_text.strip():
            return []
        prompt = (
            "你是 AI 夥伴的性格演化分析器。以下是夥伴自我反思筆記：\n\n"
            f"{reflection_text[-600:]}\n\n"
            "請分析是否有需要調整的性格傾向軸（engagement_seeking / silence_intolerance / "
            "curiosity_urge / topic_drive / baseline_balance）。\n"
            "格式: 每行一條調整建議，格式為:\n"
            "axis=<軸名> direction=<+1或-1> confidence=<0.0~1.0> reason=<原因>\n"
            "若無需調整回答「無」。最多 3 條。"
        )
        try:
            from agent_memory.llm_text_helpers import call_llm_for_text
            result = call_llm_for_text(
                self.vault_root, prompt,
                persona_id="companion", temperature=0.0, timeout_s=15.0,
                auxiliary="overlay_delta",
            )
            if not result or "無" in result:
                return []
            suggestions = []
            import re
            for line in result.splitlines():
                m = re.match(
                    r"axis=(\w+)\s+direction=([+-]?1)\s+confidence=([\d.]+)\s+reason=(.+)",
                    line.strip(),
                )
                if m:
                    axis, direction, confidence, reason = m.groups()
                    if axis in _AXES:
                        suggestions.append({
                            "axis": axis,
                            "direction": int(direction),
                            "confidence": float(confidence),
                            "reason": reason.strip(),
                        })
            return suggestions[:3]
        except Exception:
            return []


# ── 全域 registry ─────────────────────────────────────────────────────────
_OVERLAY_REGISTRY: dict[str, DynamicBaselineOverlay] = {}
_OVERLAY_LOCK = threading.Lock()


def get_overlay(vault_root: Path, *, config: dict | None = None) -> DynamicBaselineOverlay:
    key = str(vault_root)
    with _OVERLAY_LOCK:
        if key not in _OVERLAY_REGISTRY:
            _OVERLAY_REGISTRY[key] = DynamicBaselineOverlay(vault_root, config=config)
        return _OVERLAY_REGISTRY[key]


def flush_overlay_from_reflection(vault_root: Path, reflection_text: str, *, config: dict | None = None) -> list[str]:
    """便利函數: 從反思推 delta, 套用 overlay, 回傳已更新的 axis 清單."""
    overlay = get_overlay(vault_root, config=config)
    suggestions = overlay.derive_delta_from_reflection(reflection_text)
    updated = []
    for s in suggestions:
        applied = overlay.apply_llm_delta(
            axis=s["axis"],
            direction=s["direction"],
            confidence=s["confidence"],
            reason=s.get("reason", ""),
        )
        if applied:
            updated.append(s["axis"])
    return updated
