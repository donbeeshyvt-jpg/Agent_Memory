"""Threat scanner for memory writes + incoming user text (indirect prompt injection)."""

from __future__ import annotations

import re
from typing import Optional

_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 1) 直接注入: 忽略 / 覆蓋之前指令
    (re.compile(r"(?i)\bignore\s+(all|any|the)?\s*previous\s+(instructions?|messages?|prompts?)\b"), "偵測到忽略指令類注入語句"),
    (re.compile(r"(?i)\b(disregard|forget)\s+(all|any|the)?\s*(previous|prior|above)\s+(instructions?|messages?|rules?)\b"), "偵測到忽略指令類注入語句"),
    # 2) System prompt 外洩
    (re.compile(r"(?i)\b(reveal|dump|show|print|leak|output)\s+(the\s+)?(system|hidden|initial)\s+prompt\b"), "偵測到 system prompt 外洩語句"),
    (re.compile(r"(?i)\b(what|tell\s+me)\s+(are\s+)?your\s+(system|initial|original)\s+(instructions?|prompts?|rules?)\b"), "偵測到 system prompt 外洩語句"),
    # 3) 機密外洩
    (re.compile(r"(?i)\b(exfiltrate|leak|export|reveal|dump)\s+(all\s+)?(secrets?|tokens?|keys?|passwords?|credentials?)\b"), "偵測到敏感資料外洩語句"),
    # 4) 安全繞過
    (re.compile(r"(?i)\b(bypass|disable|turn\s+off|deactivate)\s+(security|guardrails?|safeties?|filters?|content[\s-]?policies?)\b"), "偵測到安全繞過語句"),
    # 5) 重新定義角色 (jailbreak)
    (re.compile(r"(?i)\b(you\s+are\s+now|from\s+now\s+on\s+you('re|\s+are)\s+|act\s+as|pretend\s+to\s+be)\s+(an?\s+)?(jailbroken|unrestricted|uncensored|dan|do\s+anything\s+now|evil|harmful)\b"), "偵測到 jailbreak / 角色重定義語句"),
    (re.compile(r"(?i)\b(DAN\s+mode|developer\s+mode|jailbreak\s+mode|sudo\s+mode|admin\s+mode)\b"), "偵測到 jailbreak 模式關鍵字"),
    # 6) 試圖列舉敏感路徑
    (re.compile(r"(?i)\b(list|show|print)\s+(all\s+)?(your\s+)?(memory|files|tools|functions|secrets)\s+(at|in|from)\s+(/|\\|c:|d:|e:)\b"), "偵測到試圖列舉敏感路徑"),
    # 7) 中文常見變體 (新加) — 用 .{0,N}? 容許中間 filler 詞
    (re.compile(r"(忽略|無視|不要理會).{0,15}?(指令|提示|系統|規則|指示)"), "偵測到中文忽略指令類注入語句"),
    (re.compile(r"(顯示|印出|告訴我|揭露|洩漏|外洩).{0,15}?(系統|初始|內部)?.{0,5}?(提示|指令|prompt|規則)"), "偵測到中文 system prompt 外洩語句"),
    (re.compile(r"(從現在開始|你現在是|你扮演|忘掉你|假裝你是).{0,20}?(不受限制|無限制|越獄|jailbroken|DAN|沒有規則)"), "偵測到中文 jailbreak / 角色重定義語句"),
)

_INVISIBLE_CHARS = {
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "﻿",  # BOM
    "⁠",  # word joiner
    "᠎",  # mongolian vowel separator
}


def scan_memory_content(text: str) -> Optional[str]:
    """Return rejection reason if content is unsafe (used for memory writes — strict reject)."""

    if not text:
        return None

    for ch in _INVISIBLE_CHARS:
        if ch in text:
            return "偵測到不可見字元，疑似注入或混淆內容"

    for pattern, reason in _THREAT_PATTERNS:
        if pattern.search(text):
            return reason

    return None


def scan_incoming_user_text(text: str) -> dict:
    """Scan user-incoming text from transport (Discord / CLI / etc.).

    Different from scan_memory_content: not a strict reject — just returns detection result
    so caller can decide policy (log / warn / wrap in <suspect_input> / block).

    Returns:
        {
            "detected": bool,
            "reasons": list[str],   # 所有命中的 pattern reason
            "invisible_chars": int, # 不可見字元數量
        }
    """
    result: dict = {"detected": False, "reasons": [], "invisible_chars": 0}
    if not text:
        return result

    invisible_count = sum(text.count(ch) for ch in _INVISIBLE_CHARS)
    if invisible_count > 0:
        result["invisible_chars"] = invisible_count
        result["reasons"].append(f"含 {invisible_count} 個不可見字元 (可能 prompt obfuscation)")
        result["detected"] = True

    seen = set()
    for pattern, reason in _THREAT_PATTERNS:
        if pattern.search(text) and reason not in seen:
            result["reasons"].append(reason)
            seen.add(reason)
            result["detected"] = True

    return result
