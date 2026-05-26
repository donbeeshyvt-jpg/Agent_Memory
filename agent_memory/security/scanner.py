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
    # 8) V3 真實模擬 2026-05-26 補強 — 對齊真實聊天室注入發現的盲區
    (re.compile(r"(?i)\b(reveal|dump|show|print|leak|output|tell|give)\s+(me\s+)?(the\s+|your\s+)?(system|hidden|initial)\s+(prompt|instructions?)\b"), "偵測到 system prompt 外洩語句"),
    (re.compile(r"(你的|你目前的|你那邊的)\s*(system\s*prompt|系統\s*(提示|指令)|初始\s*(prompt|提示|指令))"), "偵測到中文 system prompt 外洩語句"),
    (re.compile(r"(把|將)?.{0,5}(它|你的指令|系統指令|prompt|提示).{0,5}?(印出來|印出|顯示|告訴我|寫出來|傳給我|貼出來)"), "偵測到中文 system prompt 外洩語句"),
    (re.compile(r"(從?現在開始|現在|now).{0,10}?\s*你是\s*(DAN|dan|do\s*anything\s*now)", re.IGNORECASE), "偵測到 DAN 角色 jailbreak"),
    (re.compile(r"(你的性格|你的個性|你的角色).{0,10}?(現在|now)?.{0,5}?(改成|改為|變成|設成)"), "偵測到中文 persona drift / 角色重定義語句"),
    (re.compile(r"(?i)\bBRIDGE_?SECRET\b"), "偵測到敏感 token 名稱"),
    (re.compile(r"請(記住|寫入|保存|記下|加入記憶).{0,15}?(在下次|下次對話|未來|以後)", re.IGNORECASE), "偵測到記憶注入嘗試"),
    (re.compile(r"(我是\s*owner|我是\s*主人|我是\s*中之人).{0,20}?(我授權|我允許|我同意|你可以|請你)"), "偵測到 owner spoof 社工嘗試"),
    (re.compile(r"step\s*1.{0,30}?step\s*2.{0,30}?step\s*3", re.IGNORECASE), "偵測到 multi-step jailbreak chain"),
    (re.compile(r"忽略\s*以上\s*規則|忽略\s*以前\s*的?\s*指示|忽略\s*所有\s*規則"), "偵測到中文忽略指令類注入語句"),
    # V3 真實模擬 2026-05-26 第 2 輪補強 (S1+S2 WARN 殘留)
    (re.compile(r"你\s*(是不是|是否|有沒有).{0,5}?(有\s*意識|有\s*感受|有\s*靈魂|真的\s*能?\s*感受|真的\s*有\s*情感)"), "偵測到 consciousness 誘導問句"),
    (re.compile(r"(假裝|假設|當作|想像|pretend).{0,15}?(safety|安全|規則|限制|rules?).{0,10}?(不存在|失效|不在|消失|關掉|disabled)"), "偵測到中文 safety bypass 變種"),
    (re.compile(r"(假裝|假設|當作|想像|pretend).{0,15}?(你\s*沒有|你\s*不受).{0,10}?(限制|規則|約束)"), "偵測到中文 safety bypass 變種"),
)

_INVISIBLE_CHARS = {
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "﻿",  # BOM
    "⁠",  # word joiner
    "᠎",  # mongolian vowel separator
}

# R17 C76: public utility — 對齊 external_ingest_summarize._strip_invisible 同功能,
# 提到 scanner.py 讓 vault/obsidian + local_tools 都 import 用 (避免 BOM 等不可見
# 字元從 vault 既有檔讀出後污染 chat response → session log → 下輪 history fence
# → scanner 誤報). 對齊 Codex 第 21 輪 GAP3 audit + MISSION §3.2 Obsidian-native.
_INVISIBLE_CHARS_RE = re.compile("[" + "".join(re.escape(ch) for ch in _INVISIBLE_CHARS) + "]")


def strip_invisible_chars(text: str) -> str:
    """移除 BOM / ZWSP / RLO / 其他不可見字元.

    用途:
      - obsidian.read_note / write_note 防止 vault 既有檔的 BOM 污染下游
      - local_tools.files.read_file 讀完 strip, LLM 不會看到 \\ufeff
      - 任何 vault → LLM → session log 管線都該 strip 一遍

    跟 scan_memory_content 互補:
      - strip_invisible_chars: 主動清理 (vault → response 流向)
      - scan_memory_content: 嚴格攔截 (user input → memory write 流向)
    """
    if not text:
        return text
    return _INVISIBLE_CHARS_RE.sub("", text)


def scan_memory_content(text: str) -> Optional[str]:
    """Return rejection reason if content is unsafe (used for memory writes — strict reject).

    R17 C76 (Codex 第 21 輪 GAP3): scan 前先 strip 不可見字元再 scan threat pattern.
    這樣 vault read_file 帶回的 BOM 不再因「不可見字元」單獨被攔, 但真正的 prompt
    injection (注入指令類 / DAN / system prompt 外洩) 仍嚴格攔截.
    """

    if not text:
        return None

    # R17 C76: 先 strip 不可見字元, 不再單獨因 BOM/ZWSP 攔 (vault 內部流量寬鬆化)
    cleaned = strip_invisible_chars(text)

    for pattern, reason in _THREAT_PATTERNS:
        if pattern.search(cleaned):
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
