"""Folder label helpers for English-first + zh-Hant display."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_memory.security.atomic import atomic_write

DIR_INFO_FILENAME = "_DIR_INFO.md"

_CANONICAL_ZH_PURPOSE: dict[str, str] = {
    "00_System": "系統層",
    "00_System/08_Runtime_Profiles": "人格與路由設定",
    "00_System/08_Runtime_Profiles/personas": "人格定義檔",
    "00_System/08_Runtime_Profiles/routes": "人格路由檔",
    "00_System/08_Runtime_Profiles/proposals": "人格提案檔",
    "00_System/09_Index": "人類可讀索引視圖",
    "00_System/Skills": "技能程序記憶",
    "00_System/Skills/_Persona": "人格私有技能區",
    "10_Permanent": "永久知識層",
    "10_Permanent/Profiles": "使用者與角色檔",
    "10_Permanent/Facts": "事實記憶",
    "10_Permanent/Concepts": "概念記憶",
    "10_Permanent/Manual_Inputs": "使用者投餵記憶區",
    "10_Permanent/Mid_Term": "中期可變記憶區（升格過渡層）",
    "11_AI_Mirror": "AI 鏡像層",
    "11_AI_Mirror/90_to_80": "日誌降噪鏡像",
    "11_AI_Mirror/external_ingest": "外部資料鏡像",
    "11_AI_Mirror/external_ingest/notion_queue": "Notion 發佈佇列（待技能消費）",
    "11_AI_Mirror/external_ingest/web_research": "搜網鏡像資料",
    "11_AI_Mirror/template_normalized": "模板正規化輸出",
    "11_AI_Mirror/internalised_candidates": "內化候選區",
    "11_AI_Mirror/ingestion_logs": "匯入台帳區",
    "11_AI_Mirror/ingestion_logs/daily_flush": "每日短期記憶",
    "20_Literature": "文獻原始層（唯讀）",
    "30_Programming": "程式沙盒",
    "40_Gaming": "遊戲沙盒",
    "50_Media": "媒體沙盒",
    "60_Other_Domains": "其他領域沙盒",
    "70_Active_Plans": "活躍任務層",
    "70_Active_Plans/Task_Board": "協作任務板",
    "70_Active_Plans/Session_Logs": "會話記錄層",
    "80_Fleeting": "暫存靈感層（唯讀來源）",
    "90_Daily_Journal": "日誌原始層（唯讀）",
    "99_Archive": "封存層",
    "99_Archive/auto_archived": "自動封存區（curator 180d 無命中移入）",
}


# V3 C3: companion vault 專屬 zh purpose labels (對齊 V3_夥伴大腦_新規劃 §5)
_COMPANION_ZH_PURPOSE: dict[str, str] = {
    "00_System_Core": "系統核心層（夥伴）",
    "00_System_Core/personalities": "多 personality 模式（daily/stream/intimate）",
    "10_Working_Memory": "短期工作記憶",
    "10_Working_Memory/11_Session_Logs": "直播會話記錄",
    "10_Working_Memory/12_Active_Tasks": "活躍任務",
    "20_Audience_Graph": "觀眾記憶圖譜",
    "20_Audience_Graph/21_VIP_Viewers": "VIP 觀眾檔案",
    "20_Audience_Graph/22_Casual_Viewers": "一般觀眾檔案",
    "20_Audience_Graph/23_Inside_Jokes": "直播間專屬迷因",
    "20_Audience_Graph/24_Banned": "黑名單",
    "30_Emotional_State": "情緒記憶與特質演化",
    "30_Emotional_State/31_Core_Affect_Logs": "VAD + 七情天平 時序紀錄",
    "30_Emotional_State/32_Appraisal_Events": "重大情緒事件",
    "30_Emotional_State/33_Trait_Evolution": "慢速性格變化",
    "30_Emotional_State/34_Mood_Diary": "每日心情總覽",
    "40_Knowledge_Base": "純知識管理 — 日常 / 外部 兩大入口 (V3-G4)",
    "40_Knowledge_Base/41_Daily_Knowledge": "對話中累積的知識歸納 (curator L3 24h 寫, LLM 摘要)",
    "40_Knowledge_Base/42_External_Knowledge": "user/hermes 投餵的文獻知識 (curator L4 7d 寫, LLM 摘要)",
    "40_Knowledge_Base/42_External_Knowledge/_ingest_inbox": "入口: user 拖檔 / hermes 抓資料寫此 (Watcher 偵測)",
    "50_Skills_Tools": "技能進化",
    "50_Skills_Tools/51_Hermes_Learned": "Hermes Learning Loop 自學技能",
    "50_Skills_Tools/52_OpenClaw_MCP": "外部工具橋接",
    "50_Skills_Tools/53_Tool_Audit_Log": "tool 呼叫稽核",
    "60_Preference_Memory": "偏好記憶",
    "60_Preference_Memory/61_Owner_Preferences": "中之人偏好",
    "60_Preference_Memory/62_Viewer_Preferences": "各觀眾偏好聚合",
    "70_Persona_Versions": "人格版本控制",
    "70_Persona_Versions/71_Active": "當前啟用版本",
    "70_Persona_Versions/72_History": "歷史版本",
    "70_Persona_Versions/73_Candidates": "drift guard 候選（待中之人審）",
    "80_Audit_Trace": "Trace / Audit",
    "80_Audit_Trace/81_Decision_Traces": "決策 trace",
    "80_Audit_Trace/82_Memory_Audit": "記憶 audit",
    "80_Audit_Trace/83_Injection_Detected": "注入偵測紀錄",
    "90_Daily_Journal": "每日總覽",
    "99_Templates": "雙向關聯模板",
    "99_Archive": "封存層",
    "99_Archive/auto_archived": "自動封存區（curator 180d 無命中移入）",
}


def companion_dir_info_targets() -> dict[str, str]:
    """V3 C3: companion vault canonical zh purpose labels."""

    return dict(_COMPANION_ZH_PURPOSE)


def normalize_relative(path: str) -> str:
    """Normalize path to vault-style relative path."""

    return path.replace("\\", "/").strip().strip("/")


def canonical_dir_info_targets() -> dict[str, str]:
    """Canonical folders that should have readable zh purpose labels."""

    return dict(_CANONICAL_ZH_PURPOSE)


def infer_zh_purpose(folder_relative: str, *, provided: str | None = None) -> str:
    """Resolve zh-Hant folder purpose from explicit value or canonical mapping."""

    if provided and provided.strip():
        return provided.strip()
    normalized = normalize_relative(folder_relative)
    if normalized in _CANONICAL_ZH_PURPOSE:
        return _CANONICAL_ZH_PURPOSE[normalized]
    top = normalized.split("/", 1)[0] if normalized else ""
    if top in _CANONICAL_ZH_PURPOSE:
        return f"{_CANONICAL_ZH_PURPOSE[top]}子層"
    return "用途待補"


def folder_display_name(folder_relative: str, *, zh_purpose: str | None = None) -> str:
    """Return display label in `EnglishSlug / 繁中用途` format."""

    normalized = normalize_relative(folder_relative)
    base = normalized.split("/")[-1] if normalized else "(root)"
    purpose = infer_zh_purpose(normalized, provided=zh_purpose)
    return f"{base} / {purpose}"


def build_dir_info_markdown(folder_relative: str, *, zh_purpose: str | None = None) -> str:
    """Build markdown explanation file content for one folder."""

    normalized = normalize_relative(folder_relative)
    base = normalized.split("/")[-1] if normalized else "(root)"
    purpose = infer_zh_purpose(normalized, provided=zh_purpose)
    updated = datetime.now(timezone.utc).isoformat()
    return (
        f"# {base} / {purpose}\n\n"
        "- 命名規則：英文前綴供程式搜尋，繁中用途供人類閱讀。\n"
        f"- folder_path: `{normalized}/`\n"
        f"- english_slug: `{base}`\n"
        f"- zh_purpose: `{purpose}`\n"
        f"- updated_at: `{updated}`\n"
    )


def ensure_dir_info_file(
    vault_root: Path,
    folder_relative: str,
    *,
    zh_purpose: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Ensure one `_DIR_INFO.md` exists under the target folder."""

    normalized = normalize_relative(folder_relative)
    if not normalized:
        raise ValueError("folder_relative 不能為根目錄")
    folder_abs = (Path(vault_root).resolve() / normalized).resolve()
    info_abs = folder_abs / DIR_INFO_FILENAME
    if info_abs.exists() and not overwrite:
        return info_abs

    folder_abs.mkdir(parents=True, exist_ok=True)
    content = build_dir_info_markdown(normalized, zh_purpose=zh_purpose)
    atomic_write(info_abs, content)
    return info_abs
