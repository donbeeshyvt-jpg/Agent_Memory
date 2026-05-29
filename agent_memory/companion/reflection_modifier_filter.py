"""V3-O.10 #34 — 反思過濾器 (reflection_modifier_filter).

用 bot 自我反思 (00.07_Companion_MEMORY.md) 過濾 modifier 注入:
  Phase 1: keyword 比對硬編碼 anti-pattern map (~10 條, 0ms)
  Phase 2: local LLM 補判斷 (~1-2s, yaml metacognition.reflection_modifier_filter.use_llm_fallback 控制)

每 turn Step 12.5 觸發; 00.07 flush mark 避免重複讀 (Q3 解法).
"""

from __future__ import annotations

import re
import time
import threading
from pathlib import Path
from typing import Optional

# ── Phase 1: 硬編碼 anti-pattern → 對應應抑制的 modifier ──────────────────
# key: 反思文字中出現的模式 (regex)
# value: 應抑制的 _humanize_affect modifier 關鍵詞清單
_ANTI_PATTERN_MAP: dict[str, list[str]] = {
    r"不應.*拋.*問題|不要.*拋問題|避免.*反問|不.*出題|少.*提問": ["好想問問題", "想多互動"],
    r"急著填補|填補空白|急著.*轉移|快速翻頁":               ["不想冷場"],
    r"討好|應付|任務感|完美演出":                           ["想多互動", "不想冷場"],
    r"切換頻道|換話題|切換語氣":                            ["特別想聊這個"],
    r"允許.*安靜|允許.*留白|靜靜.*在場":                    ["不想冷場", "好想問問題"],
    r"用戶主導|對方.*主導":                                ["特別想聊這個", "想多互動"],
}


class ReflectionModifierFilter:
    """依 00.07 反思過濾 modifier 注入."""

    def __init__(self, vault_root: Path, *, use_llm_fallback: bool = False):
        self.vault_root = vault_root
        self.use_llm_fallback = use_llm_fallback
        self._reflection_cache: str = ""
        self._cache_mtime: float = 0.0
        self._lock = threading.Lock()
        self._extra_patterns: list[str] = []

    def load_extra_patterns(self, patterns: list[str]) -> None:
        """載入 yaml metacognition.reflection_modifier_filter.anti_pattern_extra."""
        self._extra_patterns = patterns or []

    def _read_reflection(self) -> str:
        """讀 00.07_Companion_MEMORY.md，有 flush mark 時才重讀 (Q3 解法)."""
        p = self.vault_root / "00_System_Core" / "00.07_Companion_MEMORY.md"
        if not p.exists():
            return ""
        try:
            mtime = p.stat().st_mtime
            with self._lock:
                if mtime <= self._cache_mtime:
                    return self._reflection_cache
                content = p.read_text(encoding="utf-8")
                self._reflection_cache = content
                self._cache_mtime = mtime
                return content
        except Exception:
            return ""

    def _phase1_check(self, reflection: str) -> set[str]:
        """Phase 1: keyword 比對，回傳應抑制的 modifier set."""
        suppress: set[str] = set()
        for pattern, modifiers in _ANTI_PATTERN_MAP.items():
            if re.search(pattern, reflection):
                suppress.update(modifiers)
        # 加 yaml extra patterns
        for pattern in self._extra_patterns:
            if pattern.strip() and re.search(pattern, reflection):
                suppress.add(pattern)
        return suppress

    def _phase2_llm_check(self, reflection: str, modifiers: list[str]) -> set[str]:
        """Phase 2: LLM 判斷哪些 modifier 應抑制 (timeout_s=10)."""
        if not modifiers or not reflection:
            return set()
        prompt = (
            f"以下是 AI 夥伴的自我反思筆記（摘要）：\n{reflection[-800:]}\n\n"
            f"以下是目前準備注入的行動傾向描述符：{', '.join(modifiers)}\n\n"
            f"請判斷哪些描述符與反思中「不應做的行為」衝突，列出應抑制的描述符（逗號分隔）。"
            f"若無衝突則回答「無」。只輸出描述符清單，不要解釋。"
        )
        try:
            from agent_memory.llm_text_helpers import call_llm_for_text
            result = call_llm_for_text(
                self.vault_root, prompt,
                persona_id="companion", temperature=0.0, timeout_s=10.0,
                auxiliary="modifier_filter",
            )
            if not result or "無" in result:
                return set()
            return {m.strip() for m in result.split(",") if m.strip()}
        except Exception:
            return set()

    def filter_modifiers(self, modifiers: list[str]) -> list[str]:
        """過濾 modifier list，回傳移除 anti-pattern 後的清單."""
        if not modifiers:
            return modifiers
        reflection = self._read_reflection()
        if not reflection:
            return modifiers

        suppress = self._phase1_check(reflection)

        if self.use_llm_fallback and modifiers:
            suppress |= self._phase2_llm_check(reflection, modifiers)

        if suppress:
            filtered = [m for m in modifiers if not any(s in m for s in suppress)]
            return filtered
        return modifiers


# ── 全域 registry ─────────────────────────────────────────────────────────
_FILTER_REGISTRY: dict[str, ReflectionModifierFilter] = {}
_FILTER_LOCK = threading.Lock()


def get_reflection_filter(
    vault_root: Path,
    *,
    use_llm_fallback: bool = False,
    extra_patterns: Optional[list[str]] = None,
) -> ReflectionModifierFilter:
    key = str(vault_root)
    with _FILTER_LOCK:
        if key not in _FILTER_REGISTRY:
            f = ReflectionModifierFilter(vault_root, use_llm_fallback=use_llm_fallback)
            if extra_patterns:
                f.load_extra_patterns(extra_patterns)
            _FILTER_REGISTRY[key] = f
        return _FILTER_REGISTRY[key]
