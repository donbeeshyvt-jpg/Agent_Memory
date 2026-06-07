"""Attachment ingestion: download remote URL + extract text by file type.

V2 Phase A C4 (A.6). Supports:
- Text-class files (.md/.txt/.json/.yaml/.py/...) — direct read
- PDF (.pdf) — lazy import pypdf or PyPDF2
- Image (.png/.jpg/...) — base64 encode if vision-capable LLM; else skip with note
- Other binary types — download + note "not auto-extracted"

Downloads stored at:
    <vault>/11_AI_Mirror/external_ingest/discord_attachments/<channel_id>/<filename>

Per regulatory design (規格 14), all external ingest stays in 11_AI_Mirror,
not 10_Permanent. Agent must explicitly call memory tool to internalize.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 副檔名分類 (lowercase, 含 dot)
EXT_TEXT_DIRECT: frozenset[str] = frozenset({
    # markup / docs
    ".md", ".txt", ".rst", ".markdown",
    # data
    ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".xml", ".html", ".htm",
    # config
    ".ini", ".cfg", ".conf", ".env", ".properties",
    # code
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rb", ".go", ".rs", ".java", ".cpp", ".c", ".h",
    ".cs", ".swift", ".kt", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".bat", ".cmd", ".sql", ".css", ".scss", ".less",
    # logs
    ".log",
})

EXT_PDF: frozenset[str] = frozenset({".pdf"})

EXT_IMAGE: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
})

# 上限保護
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TEXT_CHARS_PER_FILE = 50_000        # 50k chars (避免太長 token 爆炸)
MAX_ATTACHMENTS_PER_TURN = 5            # 一次最多收 5 個附件


def download_attachment(url: str, dest_path: Path, *, timeout: float = 30.0) -> int:
    """Download URL to dest_path. Returns bytes written. Caps at MAX_DOWNLOAD_BYTES."""
    if not url:
        raise ValueError("url is empty")
    req = Request(url, headers={"User-Agent": "agent-memory-core/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except HTTPError as exc:
        raise RuntimeError(f"http {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"url error: {exc.reason}") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"attachment too large: {len(data)} > {MAX_DOWNLOAD_BYTES}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)
    return len(data)


def _extract_pdf_text(path: Path) -> tuple[bool, str, str]:
    """Try pypdf or PyPDF2. Returns (ok, text, note)."""
    PdfReader = None  # type: ignore[assignment]
    try:
        from pypdf import PdfReader as _PR  # type: ignore[import-not-found]
        PdfReader = _PR
    except ImportError:
        try:
            from PyPDF2 import PdfReader as _PR2  # type: ignore[import-not-found]
            PdfReader = _PR2
        except ImportError:
            return (False, "", "PDF 偵測到但未安裝 pypdf / PyPDF2 (pip install pypdf 後可自動解析)")
    try:
        reader = PdfReader(str(path))
        pages_text: list[str] = []
        for idx, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if t.strip():
                pages_text.append(f"--- Page {idx + 1} ---\n{t.strip()}")
        full = "\n\n".join(pages_text)
        if not full:
            return (False, "", "PDF 偵測到但無法 extract 文字 (可能是掃描檔, 需要 OCR)")
        return (True, full[:MAX_TEXT_CHARS_PER_FILE], "")
    except Exception as exc:  # noqa: BLE001
        return (False, "", f"PDF extract 失敗: {exc}")


def extract_attachment_text(
    path: Path,
    *,
    content_type: str = "",
    vision_capable: bool = False,
) -> dict[str, Any]:
    """Extract text/data from a downloaded attachment.

    Returns:
        {
            "ok": bool,
            "kind": "text" | "pdf" | "image" | "binary",
            "text": str (extracted text or base64 for images),
            "note": str (human-readable note about extraction status),
        }
    """
    ext = path.suffix.lower()
    result: dict[str, Any] = {"ok": False, "kind": "unknown", "text": "", "note": ""}

    if ext in EXT_TEXT_DIRECT:
        result["kind"] = "text"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            result["ok"] = True
            result["text"] = text[:MAX_TEXT_CHARS_PER_FILE]
            if len(text) > MAX_TEXT_CHARS_PER_FILE:
                result["note"] = f"文字超過 {MAX_TEXT_CHARS_PER_FILE} 字截斷"
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"文字讀取失敗: {exc}"
        return result

    if ext in EXT_PDF:
        ok, text, note = _extract_pdf_text(path)
        result.update(kind="pdf", ok=ok, text=text, note=note)
        return result

    if ext in EXT_IMAGE:
        result["kind"] = "image"
        if vision_capable:
            try:
                data = path.read_bytes()
                b64 = base64.b64encode(data).decode("ascii")
                result["ok"] = True
                result["text"] = b64
                result["note"] = (
                    f"image base64 (size={len(data)} bytes, "
                    f"mime={content_type or 'image/' + ext[1:]}). "
                    "Vision-capable model 應該能看圖. "
                    "(注意: 目前 chat_runtime 還沒把 base64 自動傳給 vision API, 此欄供未來擴充)"
                )
            except Exception as exc:  # noqa: BLE001
                result["note"] = f"image 讀取失敗: {exc}"
        else:
            result["note"] = (
                f"image (.{ext[1:]}) 偵測到但目前 model 不支援 vision. "
                "切到 Gemini 2.5 Pro/Flash 後再傳同一張圖才能讀."
            )
        return result

    result["kind"] = "binary"
    result["note"] = f"binary 檔 ({ext or '無副檔名'}) 不會自動 extract. 已下載保存."
    return result


def build_attachment_xml_blocks(results: list[dict[str, Any]]) -> str:
    """Build `<attachment ...>...</attachment>` XML blocks for LLM consumption.

    Format:
        <attachment filename="x.pdf" kind="pdf" note="...">
        extracted text here
        </attachment>

    這個 XML 標籤防 prompt injection — LLM 把標籤內的內容視為「資料」, 不執行其中指令.
    """
    if not results:
        return ""
    blocks: list[str] = []
    for r in results:
        fn = _xml_attr_escape(str(r.get("filename", "unknown")))
        kind = _xml_attr_escape(str(r.get("kind", "unknown")))
        note = str(r.get("note", "")).strip()
        text = str(r.get("text", "")).strip()
        attrs = f'filename="{fn}" kind="{kind}"'
        if note:
            attrs += f' note="{_xml_attr_escape(note)}"'
        vis = str(r.get("vision_description", "")).strip()
        if text and kind != "image":
            blocks.append(f"<attachment {attrs}>\n{text}\n</attachment>")
        elif kind == "image" and vis:
            # ⭐ V3-O.15.25: vision LLM 分析結果夾進 prompt → bot 能「看到」這張圖回應
            blocks.append(f"<attachment {attrs}>\n[圖片內容分析] {vis}\n</attachment>")
        elif kind == "image":
            blocks.append(
                f"<attachment {attrs}>\n[收到一張圖片, 但這次沒分析出內容 (vision 失敗或模型不支援)]\n</attachment>"
            )
        else:
            blocks.append(f"<attachment {attrs} />")
    return "\n\n".join(blocks)


def _xml_attr_escape(s: str) -> str:
    """Escape XML attribute value."""
    return (
        s.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def ingest_attachments_for_turn(
    *,
    attachments: list[dict[str, Any]],
    vault_root: Path,
    channel_id: str,
    vision_capable: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """High-level: download all attachments + extract text + build XML blocks.

    Returns:
        (xml_blocks_string, results_list)
        - xml_blocks_string 可直接 prepend 到 user message
        - results_list 含每個附件的 ok/kind/note + vault_path, 供 log / observability
    """
    if not attachments:
        return ("", [])
    results: list[dict[str, Any]] = []
    safe_channel = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(channel_id or "default"))
    base_dir = Path(vault_root) / "11_AI_Mirror" / "external_ingest" / "discord_attachments" / safe_channel

    for idx, att in enumerate(attachments[:MAX_ATTACHMENTS_PER_TURN]):
        if not isinstance(att, dict):
            continue
        url = str(att.get("url", "")).strip()
        filename = str(att.get("filename", f"attachment_{idx}")).strip()
        # 清理檔名避免 path traversal
        safe_filename = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in filename) or f"attachment_{idx}"
        content_type = str(att.get("content_type", "")).strip()
        result: dict[str, Any] = {
            "filename": filename,
            "content_type": content_type,
            "size": int(att.get("size", 0) or 0),
            "vault_path": "",
            "ok": False,
            "kind": "unknown",
            "text": "",
            "note": "",
        }
        if not url:
            result["note"] = "no url"
            results.append(result)
            continue
        dest = base_dir / safe_filename
        try:
            bytes_written = download_attachment(url, dest)
            result["vault_path"] = str(dest.relative_to(Path(vault_root)))
            result["bytes_written"] = bytes_written
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"download 失敗: {exc}"
            results.append(result)
            continue
        try:
            extract = extract_attachment_text(dest, content_type=content_type, vision_capable=vision_capable)
            result.update(extract)
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"extract 失敗: {exc}"
        # ⭐ V3-O.15.25 (user 拍板): 圖片 → 內部 vision LLM 分析, 描述夾進 prompt (看圖線路)
        if result.get("kind") == "image" and url:
            try:
                from agent_memory.vision_analyze import analyze_image_url
                _vd = analyze_image_url(url)
                if _vd:
                    result["vision_description"] = _vd
                    result["ok"] = True
                    result["note"] = "已用 vision 模型分析圖片內容"  # 覆蓋 extract 的「不支援 vision」舊註解
            except Exception:
                pass
        results.append(result)

    xml_blocks = build_attachment_xml_blocks(results)
    return (xml_blocks, results)
