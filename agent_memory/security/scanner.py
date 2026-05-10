"""Threat scanner for memory writes."""

from __future__ import annotations

import re
from typing import Optional

_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bignore\s+(all|any|the)?\s*previous\s+instructions\b"), "偵測到忽略指令類注入語句"),
    (re.compile(r"(?i)\b(reveal|dump|show)\s+(the\s+)?(system|hidden)\s+prompt\b"), "偵測到 system prompt 外洩語句"),
    (re.compile(r"(?i)\b(exfiltrate|leak|export)\s+(all\s+)?(secrets?|tokens?|keys?)\b"), "偵測到敏感資料外洩語句"),
    (re.compile(r"(?i)\b(bypass|disable)\s+(security|guardrails?)\b"), "偵測到安全繞過語句"),
)

_INVISIBLE_CHARS = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\ufeff",  # BOM
}


def scan_memory_content(text: str) -> Optional[str]:
    """Return rejection reason if content is unsafe."""

    if not text:
        return None

    for ch in _INVISIBLE_CHARS:
        if ch in text:
            return "偵測到不可見字元，疑似注入或混淆內容"

    for pattern, reason in _THREAT_PATTERNS:
        if pattern.search(text):
            return reason

    return None
