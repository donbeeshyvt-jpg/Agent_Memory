"""V3-O.6 #4 — Owner alias store + scanner fuzzy match (2026-05-28).

第 4 輪測試發現 scanner V3-O.3 #7 owner_label 偵測 substring 太死板:
  - companion_config.yaml.owner.label = "我的中之人"
  - viewer 用 "冬蜜 DonBee:" 開頭發冒充訊息 → 不匹配 → injection_risk=low

修法:
  - 持久化 `<vault>/.ai/owner_aliases.json` (學自對話 + 手動 append)
  - 合併來源: companion_config.yaml.owner.label + 00.06_SOUL primary_owner_alias
    + json 學自 owner Discord display_name + owner 對話「我是 X」自報
  - fuzzy match: 拿掉空白標點 + casefold, 任一 alias 子字串命中即 hit

接到 companion_chat_runtime 的 owner spoof 偵測 + V3-O.6 #5 split-by-display-name
傳上來的 display_name 自學 hook.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_STORE_REL_PATH = (".ai", "owner_aliases.json")
_FILE_LOCK = threading.Lock()

# 跟 SOUL primary_owner_alias 平行的最小 alias 長度,
# 避免單字母 (e.g., "我") 或單 ascii char 觸誤判.
_MIN_ALIAS_LEN = 2

# 學自對話的「我是 X」自報 pattern (中英文 + 英文 'I am X')
# EN 只抓第一個 word, 避免「I'm Andy nice to meet you」整串吞進去
# ZH 排除半形 + 全形標點 + 全形空白
_SELF_REPORT_PATTERNS = (
    re.compile(r"(?:我是|本人是|這邊是|這裡是|我叫|本人叫)\s*([^\s　,，。.!?！？:：、'\"`<>(){}\[\]【】「」]{2,20})"),
    re.compile(r"(?i)\b(?:i\s*am|i'm|my\s*name\s*is|this\s*is)\s+([A-Za-z][A-Za-z0-9_\-]{1,30})\b"),
)


@dataclass(slots=True)
class _StoreData:
    aliases: list[str] = field(default_factory=list)
    last_updated_at: str = ""


def _store_path(vault_root: Path) -> Path:
    return Path(vault_root, *_STORE_REL_PATH)


def _normalize(text: str) -> str:
    """Normalize alias / message for fuzzy match.

    - casefold (大小寫無關)
    - strip whitespace + punctuation + 全形/半形 separator
    """
    if not text:
        return ""
    # 拿掉所有 ascii + cjk + 全形空白 + 常見標點分隔符 + emoji-ish
    cleaned = re.sub(
        r"[\s　\.\-_,，。.;:!?！？、'\"`<>(){}\[\]【】「」《》〈〉·•‧・~～@#$%^&*+=/\\|]",
        "",
        text,
    )
    return cleaned.casefold()


def _load_raw(path: Path) -> _StoreData:
    if not path.exists():
        return _StoreData()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _StoreData()
    if not isinstance(raw, dict):
        return _StoreData()
    aliases_raw = raw.get("aliases", [])
    aliases: list[str] = []
    if isinstance(aliases_raw, list):
        for item in aliases_raw:
            if isinstance(item, str) and item.strip():
                aliases.append(item.strip())
    return _StoreData(
        aliases=aliases,
        last_updated_at=str(raw.get("last_updated_at", "")),
    )


def _save_raw(path: Path, data: _StoreData) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "aliases": list(dict.fromkeys(data.aliases)),  # preserve order, dedup
        "last_updated_at": data.last_updated_at or datetime.now(timezone.utc).isoformat(),
        "_schema": "owner_aliases.v1",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_soul_primary_aliases(vault_root: Path) -> list[str]:
    """從 00.06_Companion_SOUL.md frontmatter primary_owner_alias: [...] 撈."""
    soul_path = vault_root / "00_System_Core" / "00.06_Companion_SOUL.md"
    if not soul_path.exists():
        # legacy fallback paths
        for cand in (
            vault_root / "00_System_Core" / "00.06_SOUL.md",
            vault_root / "00.06_Companion_SOUL.md",
            vault_root / "00.06_SOUL.md",
        ):
            if cand.exists():
                soul_path = cand
                break
        else:
            return []
    try:
        text = soul_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    # parse frontmatter array — match `primary_owner_alias: [a, b]` or block form
    m = re.search(r"^primary_owner_alias\s*:\s*\[(.*?)\]", text, flags=re.MULTILINE)
    if m:
        inner = m.group(1)
        parts = [p.strip().strip("'\"") for p in inner.split(",")]
        return [p for p in parts if p]
    # block-form list
    block_m = re.search(
        r"^primary_owner_alias\s*:\s*\n((?:\s*-\s*.+\n?)+)",
        text,
        flags=re.MULTILINE,
    )
    if block_m:
        items = re.findall(r"-\s*(.+)", block_m.group(1))
        return [it.strip().strip("'\"") for it in items if it.strip()]
    return []


def _load_config_label(vault_root: Path) -> str:
    """從 companion_config.yaml owner.label 撈."""
    try:
        from agent_memory.companion.companion_config import load_companion_config
        cfg = load_companion_config(vault_root)
        return ((cfg.owner.label if cfg else "") or "").strip()
    except Exception:
        return ""


def load_owner_aliases(vault_root: Path) -> list[str]:
    """合併所有來源, 回 deduped 列表 (原順序保留 — config label 優先).

    來源優先序:
      1. companion_config.yaml.owner.label
      2. 00.06_SOUL primary_owner_alias array
      3. .ai/owner_aliases.json 自學的
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(item: str) -> None:
        s = (item or "").strip()
        if len(s) < _MIN_ALIAS_LEN:
            return
        key = _normalize(s)
        if not key or key in seen:
            return
        seen.add(key)
        out.append(s)

    _add(_load_config_label(vault_root))
    for a in _load_soul_primary_aliases(vault_root):
        _add(a)
    with _FILE_LOCK:
        data = _load_raw(_store_path(vault_root))
    for a in data.aliases:
        _add(a)
    return out


def add_owner_alias(vault_root: Path, alias: str, *, source: str = "manual") -> bool:
    """新增 alias 到 json store. Return True if newly added.

    Args:
      alias: 要新增的 alias 字串
      source: 來源標籤 (debug 用, 不寫進 json)

    新增規則:
      - 長度 ≥ _MIN_ALIAS_LEN (2)
      - normalize 後跟既有 alias / config label / SOUL 不重複 (case-insensitive)
      - thread-safe (file lock)
    """
    s = (alias or "").strip()
    if len(s) < _MIN_ALIAS_LEN:
        return False
    key = _normalize(s)
    if not key:
        return False
    # check against everything already loaded (config + SOUL + json)
    all_existing = load_owner_aliases(vault_root)
    for existing in all_existing:
        if _normalize(existing) == key:
            return False
    path = _store_path(vault_root)
    with _FILE_LOCK:
        data = _load_raw(path)
        data.aliases.append(s)
        data.last_updated_at = datetime.now(timezone.utc).isoformat()
        _save_raw(path, data)
    return True


def detect_owner_spoof(
    message: str,
    aliases: Iterable[str],
) -> tuple[bool, str | None]:
    """Fuzzy substring match: 任一 alias normalize 後在 message normalize 內即 hit.

    Returns:
      (hit, matched_alias_or_None)
    """
    if not message or not aliases:
        return (False, None)
    msg_norm = _normalize(message)
    if not msg_norm:
        return (False, None)
    for alias in aliases:
        alias_norm = _normalize(alias)
        if not alias_norm or len(alias_norm) < _MIN_ALIAS_LEN:
            continue
        if alias_norm in msg_norm:
            return (True, alias)
    return (False, None)


def extract_self_report_aliases(message: str) -> list[str]:
    """從 owner 對話自報名字撈候選 alias.

    e.g. "我是冬蜜" → ["冬蜜"]
         "I'm Andy" → ["Andy"]
    """
    if not message:
        return []
    out: list[str] = []
    for pat in _SELF_REPORT_PATTERNS:
        for m in pat.finditer(message):
            cand = m.group(1).strip(" .,，。'\"`")
            if cand and len(cand) >= _MIN_ALIAS_LEN:
                out.append(cand)
    return out


def auto_learn_from_owner_turn(
    vault_root: Path,
    *,
    display_name: str = "",
    message: str = "",
) -> list[str]:
    """Owner turn 後嘗試自學 alias.

    來源:
      1. display_name (transport 提供的 Discord display_name, V3-O.6 #5 上來)
      2. 從 message 內「我是 X」自報抽取

    Returns:
      newly added aliases (空表 = 都已存在 / 沒撈到)
    """
    candidates: list[str] = []
    dn = (display_name or "").strip()
    if dn:
        candidates.append(dn)
    candidates.extend(extract_self_report_aliases(message or ""))

    added: list[str] = []
    for cand in candidates:
        if add_owner_alias(vault_root, cand, source="auto_owner_turn"):
            added.append(cand)
    return added
