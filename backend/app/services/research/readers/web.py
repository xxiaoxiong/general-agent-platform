"""网页/文本 Source Reader：真实读取正文、校验 HTTP 状态与内容类型，防 SSRF。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

import httpx

from app.core.config import settings

from .base import SourceDocument

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_STRIP_BLOCKS = re.compile(
    r"<(script|style|head|nav|footer|header|noscript)[^>]*>.*?</\1>", re.S | re.I,
)
_MAX_BYTES = 5 * 1024 * 1024
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
}


def _html_to_text(html: str) -> str:
    html = _STRIP_BLOCKS.sub(" ", html)
    html = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf_to_text(payload: bytes) -> str:
    try:
        import pypdfium2 as pdfium  # type: ignore

        document = pdfium.PdfDocument(payload)
        parts = []
        for index in range(min(len(document), 200)):
            page = document[index]
            textpage = page.get_textpage()
            parts.append(textpage.get_text_range())
        return "\n".join(parts).strip()
    except Exception:
        return ""


def page_text(text: str, *, max_chars: int, offset: int = 0) -> tuple[str, int, int, bool]:
    """长正文分块：返回 (chunk, start, next_offset, has_more)。"""
    start = max(0, int(offset or 0))
    chunk = str(text or "")[start:start + max_chars]
    next_offset = start + len(chunk)
    return chunk, start, next_offset, next_offset < len(str(text or ""))


def _tool_policy():
    """延迟导入 ToolPolicy：避免 research.readers 与 agent_runtime.tools 循环导入。"""
    from app.services.agent_runtime.tools.policy import ToolPolicy, ToolPolicyError

    return ToolPolicy, ToolPolicyError


async def read_url(
    url: str, *, max_chars: int = 12000, offset: int = 0,
) -> SourceDocument:
    """读取网页/PDF/纯文本；支持 offset 分块读取长内容，失败时返回结构化错误。"""
    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return SourceDocument(
            url=target, final_url=target, error_code="unsupported_type",
            error_detail="URL 必须以 http:// 或 https:// 开头",
        )
    ToolPolicy, ToolPolicyError = _tool_policy()
    try:
        ToolPolicy(allowed_tools=set()).check_url(target)
    except ToolPolicyError as exc:
        return SourceDocument(
            url=target, final_url=target, error_code="blocked", error_detail=str(exc),
        )
    try:
        async with httpx.AsyncClient(
            timeout=25,
            verify=(settings.AGENT_CA_BUNDLE or settings.AGENT_TLS_VERIFY),
            proxy=(settings.AGENT_EGRESS_PROXY or None),
            follow_redirects=True,
        ) as client:
            response = await client.get(target, headers=_HEADERS)
    except httpx.TimeoutException as exc:
        return SourceDocument(
            url=target, final_url=target, error_code="timeout", error_detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 网络错误明确上报
        return SourceDocument(
            url=target, final_url=target, error_code="http_error", error_detail=str(exc),
        )

    final_url = str(response.url)
    try:
        # 跟随重定向后再次校验最终目标，防止恶意重定向到内网/回环
        ToolPolicy(allowed_tools=set()).check_url(final_url)
    except ToolPolicyError as exc:
        return SourceDocument(
            url=target, final_url=final_url, error_code="blocked",
            error_detail=f"重定向目标被拒绝: {exc}",
        )
    content_type = response.headers.get("content-type", "").casefold()
    status = response.status_code
    if status < 200 or status >= 300:
        return SourceDocument(
            url=target, final_url=final_url, http_status=status,
            content_type=content_type, error_code="http_error",
            error_detail=f"HTTP {status}",
        )
    payload = response.content
    if len(payload) > _MAX_BYTES:
        return SourceDocument(
            url=target, final_url=final_url, http_status=status,
            content_type=content_type, error_code="unsupported_type",
            error_detail=f"响应超过 {_MAX_BYTES // (1024 * 1024)}MB 限制",
        )

    title = ""
    if "application/pdf" in content_type or final_url.casefold().endswith(".pdf"):
        text = _pdf_to_text(payload)
    elif "html" in content_type or "xml" in content_type or not content_type:
        raw = response.text
        match = _TITLE.search(raw)
        title = _html_to_text(match.group(1)) if match else ""
        text = _html_to_text(raw)
    else:
        text = response.text

    text = text.strip()
    if len(text) < 50:
        return SourceDocument(
            url=target, final_url=final_url, title=title, http_status=status,
            content_type=content_type, error_code="empty_content",
            error_detail="未提取到可用正文（页面可能由脚本渲染或拒绝访问）",
        )
    start = max(0, int(offset or 0))
    chunk, start, next_offset, has_more = page_text(
        text, max_chars=max_chars, offset=start,
    )
    if not chunk:
        return SourceDocument(
            url=target, final_url=final_url, title=title[:500], http_status=status,
            content_type=content_type, total_chars=len(text), offset=start,
            error_code="empty_content",
            error_detail=f"offset {start} 已超出正文长度 {len(text)}",
        )
    return SourceDocument(
        url=target,
        final_url=final_url,
        title=title[:500],
        text=chunk,
        full_text=text,
        content_type=content_type,
        http_status=status,
        retrieved_at=datetime.utcnow().isoformat(),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        offset=start,
        next_offset=next_offset,
        has_more=has_more,
        total_chars=len(text),
        metadata={
            "domain": urlsplit(final_url).hostname or "",
            "total_chars": len(text),
            "offset": start,
            "next_offset": next_offset,
            "has_more": has_more,
        },
    )


class WebSourceReader:
    async def read(self, url: str, *, max_chars: int = 12000, offset: int = 0) -> SourceDocument:
        return await read_url(url, max_chars=max_chars, offset=offset)
