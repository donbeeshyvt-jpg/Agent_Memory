"""V3-O.10 #34 — 反思過濾器 (reflection_modifier_filter).

用 bot 自我反思 (00.07_Companion_MEMORY.md) 過濾 modifier 注入:
  Phase 1: keyword 比對硬編碼 anti-pattern map (~10 條, 0ms)
  Phase 2: local LLM 補判斷 (~1-2s, yaml metacognition.reflection_modifier_filter.use_llm_fallback 控制)

每 turn Step 12.5 觸發; 00.07 flush mark 避免重複讀 (Q3 解法).
"""

from __future__ import annotations

import hashlib
import queue as _queue_mod
import re
import sys as _sys
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


# ─── V3-O.13 #R3 (2026-06-04 user): phase2 LLM 過濾改 async background (不阻塞主 turn) ───
# 設計: filter_modifiers 立刻回 phase1 結果 + 上次背景跑完的 phase2 cache (同段反思).
# phase2 LLM 在背景跑 (chain fallback 可能 40-50s 都 OK, 不阻塞主對話).
# 下個 turn 同段反思就能套上次的 phase2 suppress 結果, 達到「LLM 過濾不損失 + 不阻塞」.
def _hash_reflection(text: str) -> str:
    """同段反思指紋 (用前 1500 char md5). 反思檔 mtime 變化後 hash 也變 → cache 失效."""
    return hashlib.md5((text or "")[:1500].encode("utf-8", errors="replace")).hexdigest()


_REFLECTION_LLM_QUEUE: _queue_mod.Queue = _queue_mod.Queue()
_REFLECTION_LLM_WORKER_STARTED = False
_REFLECTION_LLM_WORKER_LOCK = threading.Lock()


def _reflection_llm_worker_loop() -> None:
    """Background worker: FIFO 跑 phase2 LLM check, 結果寫進 ReflectionModifierFilter cache."""
    while True:
        try:
            item = _REFLECTION_LLM_QUEUE.get()
            if item is None:
                break
            filter_instance, reflection_snap, modifiers_snap = item
            key = _hash_reflection(reflection_snap)
            try:
                result_set = filter_instance._run_phase2_sync(reflection_snap, modifiers_snap)
                with filter_instance._lock:
                    filter_instance._phase2_cache_reflection_key = key
                    filter_instance._phase2_cache_suppress = result_set
            except Exception as exc:
                try:
                    print(f"[reflection-llm worker FAIL] {type(exc).__name__}: {str(exc)[:200]}", file=_sys.stderr)
                except Exception:
                    pass
            finally:
                with filter_instance._lock:
                    filter_instance._phase2_inflight_keys.discard(key)
                try:
                    _REFLECTION_LLM_QUEUE.task_done()
                except Exception:
                    pass
        except Exception:
            pass  # 永不破 worker


def _ensure_reflection_llm_worker_started() -> None:
    global _REFLECTION_LLM_WORKER_STARTED
    with _REFLECTION_LLM_WORKER_LOCK:
        if not _REFLECTION_LLM_WORKER_STARTED:
            t = threading.Thread(
                target=_reflection_llm_worker_loop,
                daemon=True,
                name="reflection-llm-worker",
            )
            t.start()
            _REFLECTION_LLM_WORKER_STARTED = True


class ReflectionModifierFilter:
    """依 00.07 反思過濾 modifier 注入."""

    def __init__(self, vault_root: Path, *, use_llm_fallback: bool = False):
        self.vault_root = vault_root
        self.use_llm_fallback = use_llm_fallback
        self._reflection_cache: str = ""
        self._cache_mtime: float = 0.0
        self._lock = threading.Lock()
        self._extra_patterns: list[str] = []
        # V3-O.13 #R3: phase2 LLM 結果 cache (背景 worker 寫進來)
        self._phase2_cache_reflection_key: str = ""  # 對應反思指紋
        self._phase2_cache_suppress: set[str] = set()
        self._phase2_inflight_keys: set[str] = set()  # 避免同段反思並發 enqueue

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

    def _run_phase2_sync(self, reflection: str, modifiers: list[str]) -> set[str]:
        """V3-O.13 #R3 (renamed from _phase2_llm_check): 真正跑 phase2 LLM, 給背景 worker 用.

        Phase 2: LLM 判斷哪些 modifier 應抑制 (timeout_s=10, chain fallback 跑完可 40-50s).
        在背景 worker thread 跑, 不阻塞主對話 (step12_5).
        """
        if not modifiers or not reflection:
            return set()
        prompt = (
            f"以下是 AI 夥伴的自我反思筆記（摘要）：\n{reflection[-800:]}\n\n"
            f"以下是目前準備注入的行動傾向描述符:{', '.join(modifiers)}\n\n"
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
        """過濾 modifier list，回傳移除 anti-pattern 後的清單.

        V3-O.13 #R3: phase2 LLM 改 async — 立即回 phase1 + cached phase2 結果 (上次背景跑完的).
        本 turn 不等 LLM. 新反思 (新 hash) 觸發背景 enqueue, 下次同段反思 turn 才套上去.
        """
        if not modifiers:
            return modifiers
        reflection = self._read_reflection()
        if not reflection:
            return modifiers

        suppress = self._phase1_check(reflection)

        # V3-O.13 #R3: phase2 LLM 走背景 — 用 cached 結果 (同段反思上次跑完的),
        # 沒 cache 就 enqueue 背景跑等下次套, 本 turn 不等 (避免 40-50s 阻塞).
        enqueue_phase2_now = False
        if self.use_llm_fallback:
            curr_key = _hash_reflection(reflection)
            with self._lock:
                if self._phase2_cache_reflection_key == curr_key:
                    # 同段反思 + 已有 cache → 套用上次背景結果
                    suppress |= self._phase2_cache_suppress
                elif curr_key not in self._phase2_inflight_keys:
                    # 新反思 (或反思更新) → 背景 enqueue, 本 turn 還用不到結果
                    self._phase2_inflight_keys.add(curr_key)
                    enqueue_phase2_now = True
            if enqueue_phase2_now:
                _ensure_reflection_llm_worker_started()
                _REFLECTION_LLM_QUEUE.put((self, reflection, list(modifiers)))

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
