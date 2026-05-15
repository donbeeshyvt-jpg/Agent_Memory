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
}


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
