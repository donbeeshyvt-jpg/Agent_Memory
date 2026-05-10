"""Simple web research pipeline for external knowledge ingestion."""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from agent_memory.security.atomic import atomic_write
from agent_memory.security.locks import file_lock
from agent_memory.types import Frontmatter, MemoryNote, MemorySource, MemoryType
from agent_memory.vault import ObsidianVaultAdapter

WEB_RESEARCH_BASE_DIR = "11_AI_Mirror/external_ingest/web_research"
WEB_RESEARCH_LOG_PATH = "11_AI_Mirror/ingestion_logs/web_research.md"

_SPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip > 0:
            return
        text = _SPACE_RE.sub(" ", data).strip()
        if text:
            self._parts.append(text)

    def as_text(self) -> str:
        return "\n".join(self._parts).strip()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _slugify(text: str, *, fallback: str) -> str:
    cleaned = _SLUG_RE.sub("-", (text or "").strip()).strip("-").lower()
    return cleaned or fallback


def _decode_ddg_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return urllib.parse.unquote(query["uddg"][0])
    return url


def _http_get(url: str, *, timeout_s: float = 20.0) -> tuple[str, str]:
    req = urllib.request.Request(
        url=url,
        headers={
            "User-Agent": "Mozilla/5.0 (AgentMemoryCore/0.1; +https://example.local)",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:  # noqa: S310
        raw = resp.read()
        content_type = str(resp.headers.get("Content-Type", ""))
    charset = "utf-8"
    match = re.search(r"charset=([a-zA-Z0-9._-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1).strip().lower()
    try:
        text = raw.decode(charset, errors="ignore")
    except LookupError:
        text = raw.decode("utf-8", errors="ignore")
    return text, content_type


def search_web(query: str, *, max_results: int = 5, timeout_s: float = 20.0) -> list[dict[str, str]]:
    q = query.strip()
    if not q:
        raise ValueError("query 不可為空")
    target = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
    body, _ = _http_get(target, timeout_s=timeout_s)
    anchor_re = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_re = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.IGNORECASE | re.DOTALL)
    results: list[dict[str, str]] = []
    snippet_iter = snippet_re.finditer(body)
    snippet_positions = [(m.start(), m.group("snippet")) for m in snippet_iter]
    for match in anchor_re.finditer(body):
        href = html.unescape(match.group("href"))
        title_html = match.group("title")
        title = _SPACE_RE.sub(" ", html.unescape(re.sub(r"<[^>]+>", " ", title_html))).strip()
        if not href or not title:
            continue
        url = _decode_ddg_url(href)
        snippet = ""
        for pos, value in snippet_positions:
            if pos >= match.end():
                snippet = _SPACE_RE.sub(" ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()
                break
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max(1, int(max_results)):
            break
    return results


def fetch_url_text(url: str, *, timeout_s: float = 20.0, max_chars: int = 12000) -> dict[str, str]:
    text, content_type = _http_get(url, timeout_s=timeout_s)
    lowered = content_type.lower()
    if "html" in lowered or "<html" in text.lower():
        parser = _VisibleTextParser()
        parser.feed(text)
        content = parser.as_text()
    else:
        content = text
    compact = _SPACE_RE.sub(" ", content).strip()
    if len(compact) > max_chars:
        compact = compact[:max_chars] + " ..."
    return {"url": url, "content_type": content_type, "text": compact}


def _append_web_research_log(vault_root: Path, payload: dict[str, Any]) -> str:
    root = Path(vault_root).expanduser().resolve()
    target = (root / WEB_RESEARCH_LOG_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, "# web_research_log\n\n")
    block = (
        f"## {payload.get('timestamp', _now_iso())}\n\n"
        f"- query: {payload.get('query', '')}\n"
        f"- note_path: `{payload.get('note_path', '')}`\n"
        f"- operator: `{payload.get('operator', '')}`\n"
        f"- sources: {payload.get('sources', 0)}\n\n"
    )
    with file_lock(target, timeout=5.0):
        existing = target.read_text(encoding="utf-8")
        atomic_write(target, existing + block)
    return WEB_RESEARCH_LOG_PATH


def _build_research_note(
    *,
    query: str,
    operator: str,
    hits: list[dict[str, str]],
    fetched: list[dict[str, str]],
) -> str:
    lines = [
        f"# Web Research: {query}",
        "",
        f"- query: `{query}`",
        f"- operator: `{operator}`",
        f"- captured_at: `{_now_iso()}`",
        f"- source_count: `{len(hits)}`",
        "",
        "## Search Hits",
        "",
    ]
    for idx, hit in enumerate(hits, start=1):
        lines.append(f"{idx}. [{hit.get('title', 'untitled')}]({hit.get('url', '')})")
        snippet = str(hit.get("snippet", "")).strip()
        if snippet:
            lines.append(f"   - snippet: {snippet}")
    lines.append("")
    lines.append("## Source Excerpts")
    lines.append("")
    for idx, item in enumerate(fetched, start=1):
        lines.append(f"### Source {idx}: {item.get('url', '')}")
        lines.append("")
        lines.append(f"- content_type: `{item.get('content_type', '')}`")
        lines.append("")
        lines.append(item.get("text", ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_web_research(
    vault_root: Path,
    *,
    query: str,
    operator: str = "researcher",
    max_results: int = 5,
    fetch_top: int = 3,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()

    hits = search_web(query, max_results=max_results, timeout_s=timeout_s)
    if not hits:
        raise ValueError("搜尋結果為空，請嘗試更精準關鍵字")
    fetched: list[dict[str, str]] = []
    for hit in hits[: max(1, int(fetch_top))]:
        try:
            fetched.append(fetch_url_text(str(hit.get("url", "")), timeout_s=timeout_s))
        except Exception as exc:  # noqa: BLE001
            fetched.append(
                {
                    "url": str(hit.get("url", "")),
                    "content_type": "error",
                    "text": f"[fetch_error] {exc}",
                }
            )
    date_part = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(query, fallback="research")
    note_name = f"{datetime.now().strftime('%H%M%S')}-{slug}"
    path = f"{WEB_RESEARCH_BASE_DIR}/{date_part}/{note_name}.md"
    body = _build_research_note(query=query, operator=operator, hits=hits, fetched=fetched)
    note = MemoryNote(
        path=path,
        frontmatter=Frontmatter(
            type=MemoryType.LONG_TERM,
            source=MemorySource.MIRROR,
            agent=_slugify(operator, fallback="researcher"),
            tags=["web_research", "external_ingest"],
            extras={
                "query": query,
                "source_count": len(hits),
                "fetched_count": len(fetched),
            },
        ),
        body=body,
    )
    adapter.write_note(note)
    _append_web_research_log(
        root,
        {
            "timestamp": _now_iso(),
            "query": query,
            "note_path": path,
            "operator": _slugify(operator, fallback="researcher"),
            "sources": len(hits),
        },
    )
    return {
        "query": query,
        "note_path": path,
        "hits": hits,
        "fetched_count": len(fetched),
    }


def ingest_web_url(
    vault_root: Path,
    *,
    url: str,
    title: str = "",
    operator: str = "researcher",
    timeout_s: float = 20.0,
) -> dict[str, str]:
    target = url.strip()
    if not target:
        raise ValueError("url 不可為空")
    root = Path(vault_root).expanduser().resolve()
    adapter = ObsidianVaultAdapter(root)
    adapter.ensure_skeleton()
    fetched = fetch_url_text(target, timeout_s=timeout_s)
    parsed = urllib.parse.urlparse(target)
    base_title = title.strip() or parsed.netloc or "web"
    slug = _slugify(base_title, fallback="web")
    date_part = datetime.now().strftime("%Y-%m-%d")
    name = f"{datetime.now().strftime('%H%M%S')}-{slug}"
    path = f"{WEB_RESEARCH_BASE_DIR}/{date_part}/{name}.md"
    body = (
        f"# Web Ingest: {base_title}\n\n"
        f"- url: {target}\n"
        f"- operator: `{_slugify(operator, fallback='researcher')}`\n"
        f"- captured_at: `{_now_iso()}`\n\n"
        "## Excerpt\n\n"
        f"{fetched.get('text', '')}\n"
    )
    note = MemoryNote(
        path=path,
        frontmatter=Frontmatter(
            type=MemoryType.LONG_TERM,
            source=MemorySource.MIRROR,
            agent=_slugify(operator, fallback="researcher"),
            tags=["web_ingest", "external_ingest"],
            extras={"url": target, "title": base_title},
        ),
        body=body,
    )
    adapter.write_note(note)
    _append_web_research_log(
        root,
        {
            "timestamp": _now_iso(),
            "query": f"url:{target}",
            "note_path": path,
            "operator": _slugify(operator, fallback="researcher"),
            "sources": 1,
        },
    )
    return {"note_path": path, "url": target}
