"""V3-O.10 #40 — Overlay 升格 SOUL.dynamic_sections.

當 overlay delta 穩定（達到 evidence_threshold）後,
將 effective_baseline 直接寫進 SOUL.md 的 dynamic_sections 對應欄位.

觸發: flush_overlay_from_reflection 後若有更新軸, 由 evolution_interval_minutes 計時.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Optional


_PROMOTION_LOCK = threading.Lock()
_LAST_PROMOTION: dict[str, float] = {}


def _parse_soul_frontmatter(text: str) -> tuple[dict, str]:
    """拆解 SOUL.md frontmatter + body."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:]
    # 簡單 key: value parse (不依賴 yaml, 避免循環 import)
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def promote_overlay_to_soul(
    vault_root: Path,
    updated_axes: list[str],
    *,
    effective_baselines: dict[str, float],
    evolution_interval_minutes: int = 5,
) -> list[str]:
    """將 overlay 更新的軸升格寫進 SOUL.md dynamic_sections.

    Returns: 成功寫入的 axis 清單
    """
    if not updated_axes or not effective_baselines:
        return []

    key = str(vault_root)
    now = time.monotonic()
    with _PROMOTION_LOCK:
        last = _LAST_PROMOTION.get(key, 0.0)
        elapsed_min = (now - last) / 60.0
        if elapsed_min < evolution_interval_minutes:
            return []  # 還沒到升格時間
        _LAST_PROMOTION[key] = now

    soul_path = vault_root / "00_System_Core" / "00.06_Companion_SOUL.md"
    if not soul_path.exists():
        return []

    try:
        original = soul_path.read_text(encoding="utf-8")
        fm, body = _parse_soul_frontmatter(original)

        # 確認 dynamic_sections 包含 personality_baseline
        locked = fm.get("locked_sections", "")
        # schema v11+ 才允許升格
        schema_ver = int(fm.get("schema_version", 10))
        if schema_ver < 11:
            return []

        written: list[str] = []
        new_body = body

        for axis in updated_axes:
            if axis not in effective_baselines:
                continue
            new_val = round(effective_baselines[axis], 4)

            # 找並替換對應的 baseline_* 行
            yaml_key = axis if axis.startswith("baseline_") else f"baseline_{axis}"
            pattern = re.compile(rf"^(\s*-\s*{re.escape(yaml_key)}:\s*)[\d.]+", re.MULTILINE)
            match = pattern.search(new_body)
            if match:
                new_body = pattern.sub(rf"\g<1>{new_val}", new_body)
                written.append(axis)
            else:
                # key 不存在 → 在「## 我的初始性格」段後加
                insert_pattern = re.compile(r"(## 我的初始性格.*?\n)", re.DOTALL)
                if insert_pattern.search(new_body):
                    new_body = insert_pattern.sub(
                        rf"\1- {yaml_key}: {new_val}\n", new_body
                    )
                    written.append(axis)

        if written:
            # 重建文件（保留原始 frontmatter 不動，只改 body）
            end = original.find("\n---\n", 4)
            fm_block = original[: end + 5] if end > 0 else ""
            soul_path.write_text(fm_block + new_body, encoding="utf-8")

        return written
    except Exception:
        return []
