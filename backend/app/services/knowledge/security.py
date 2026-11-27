"""知识库安全边界：文件类型 / Magic Bytes / 解压炸弹 / SSRF。

原则（任务书 §11.3 / §11.4）：
- 未支持格式明确拒绝，不按 UTF-8 文本兜底；
- 文件类型以 Magic Bytes 为准，扩展名只作路由提示；
- Office ZIP 解压大小 / 条目数受限，防压缩炸弹；
- 网页抓取禁止内网 / 回环 / 链路本地 / 云元数据，重定向逐跳复核。
"""
from __future__ import annotations

import codecs
import ipaddress
import logging
import socket
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

ZIP_BASED = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml",
             "pptx": "ppt/presentation.xml", "odt": "content.xml",
             "ods": "content.xml", "odp": "content.xml"}
OLE_BASED = {"doc", "xls", "ppt"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "tif", "tiff"}
TEXT_EXTS = {"txt", "md", "markdown", "csv", "html", "htm"}
MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint",
    "txt": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "html": "text/html",
    "htm": "text/html",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "rtf": "application/rtf",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
}


class KnowledgeSecurityError(Exception):
    """安全校验失败（API 映射 400 / 422）。"""

    def __init__(self, message: str, code: str = "invalid_file"):
        super().__init__(message)
        self.code = code


def allowed_formats() -> set:
    raw = settings.KNOWLEDGE_ALLOWED_FORMATS or ""
    return {item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip()}


def validate_extension(filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower().strip()
    if not ext:
        raise KnowledgeSecurityError("文件缺少扩展名，无法识别格式", code="missing_extension")
    if ext not in allowed_formats():
        raise KnowledgeSecurityError(
            f"不支持的格式 .{ext}（支持：{', '.join(sorted(allowed_formats()))}）",
            code="unsupported_format",
        )
    return "md" if ext == "markdown" else ext


def max_upload_bytes() -> int:
    return int(settings.KNOWLEDGE_MAX_UPLOAD_SIZE_MB) * 1024 * 1024


def detect_encoding(path: str) -> str:
    """文本编码探测：UTF-8 BOM / UTF-8 / GB18030 / Big5 / Latin-1。"""
    with open(path, "rb") as fh:
        sample = fh.read(64 * 1024)
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin-1"


def read_text_file(path: str) -> str:
    encoding = detect_encoding(path)
    with open(path, "r", encoding=encoding, errors="strict") as fh:
        return fh.read()


def _looks_like_html(head: bytes) -> bool:
    probe = head[:4096].lstrip().lower()
    return probe.startswith(b"<!doctype html") or probe.startswith(b"<html") or b"<html" in probe


def _is_decodable_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            head.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def validate_magic(path: str, ext: str) -> str:
    """按 Magic Bytes 校验文件真实类型，返回 MIME。

    校验失败抛 ``KnowledgeSecurityError``（不进入 Parser）。
    """
    with open(path, "rb") as fh:
        head = fh.read(8192)
    if not head:
        raise KnowledgeSecurityError("文件为空", code="empty_file")

    if ext == "pdf":
        if not head.startswith(b"%PDF-"):
            raise KnowledgeSecurityError("文件内容不是 PDF（Magic Bytes 不符）", code="mime_mismatch")
        return MIME_BY_EXT["pdf"]

    if ext in ZIP_BASED:
        if not head.startswith(b"PK"):
            raise KnowledgeSecurityError(
                f".{ext} 文件不是有效的 Office 压缩包（Magic Bytes 不符）", code="mime_mismatch"
            )
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
                names = {info.filename for info in infos}
                if ZIP_BASED[ext] not in names:
                    raise KnowledgeSecurityError(
                        f".{ext} 压缩包缺少 {ZIP_BASED[ext]}，内容与扩展名不符",
                        code="mime_mismatch",
                    )
                expanded = sum(max(0, info.file_size) for info in infos)
                limit = int(settings.KNOWLEDGE_MAX_ARCHIVE_EXPANDED_BYTES)
                if expanded > limit:
                    raise KnowledgeSecurityError(
                        f"Office 文件解压后 {expanded} 字节超过上限 {limit}（疑似压缩炸弹）",
                        code="archive_too_large",
                    )
                if len(infos) > 5000:
                    raise KnowledgeSecurityError(
                        "Office 压缩包条目数超过 5000（疑似压缩炸弹）", code="archive_too_large"
                    )
        except zipfile.BadZipFile as exc:
            raise KnowledgeSecurityError(f"Office 压缩包损坏：{exc}", code="mime_mismatch") from exc
        return MIME_BY_EXT[ext]

    if ext in OLE_BASED:
        if not head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise KnowledgeSecurityError(
                f".{ext} 不是旧版 Office 二进制格式（Magic Bytes 不符）", code="mime_mismatch"
            )
        return MIME_BY_EXT[ext]

    if ext == "png":
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            raise KnowledgeSecurityError("文件内容不是 PNG", code="mime_mismatch")
        return MIME_BY_EXT["png"]
    if ext in {"jpg", "jpeg"}:
        if not head.startswith(b"\xff\xd8\xff"):
            raise KnowledgeSecurityError("文件内容不是 JPEG", code="mime_mismatch")
        return MIME_BY_EXT["jpg"]
    if ext in {"tif", "tiff"}:
        if not (head.startswith(b"II*\x00") or head.startswith(b"MM\x00*")):
            raise KnowledgeSecurityError("文件内容不是 TIFF", code="mime_mismatch")
        return MIME_BY_EXT["tiff"]
    if ext == "rtf":
        if not head.lstrip().startswith(b"{\\rtf"):
            raise KnowledgeSecurityError("文件内容不是 RTF", code="mime_mismatch")
        return MIME_BY_EXT["rtf"]
    if ext in {"html", "htm"}:
        if not _looks_like_html(head):
            raise KnowledgeSecurityError("文件内容不是 HTML", code="mime_mismatch")
        return MIME_BY_EXT[ext]
    if ext in {"txt", "md", "csv"}:
        if not _is_decodable_text(head):
            raise KnowledgeSecurityError(
                f".{ext} 文件不是可解码的文本（疑似二进制或错误编码）", code="mime_mismatch"
            )
        return MIME_BY_EXT[ext]
    raise KnowledgeSecurityError(f"未登记格式 .{ext}", code="unsupported_format")


def validate_chunk_config(chunk_size: int, chunk_overlap: int) -> None:
    """Chunk 配置强制校验（任务书 §5）：非法返回 422，不进入 Worker。"""
    if chunk_size is None or int(chunk_size) <= 0:
        raise KnowledgeSecurityError("chunk_size 必须大于 0", code="invalid_chunk_config")
    if chunk_overlap is None or int(chunk_overlap) < 0:
        raise KnowledgeSecurityError("chunk_overlap 必须大于等于 0", code="invalid_chunk_config")
    if int(chunk_overlap) >= int(chunk_size):
        raise KnowledgeSecurityError(
            "chunk_overlap 必须小于 chunk_size（否则切分步长为零或负数）",
            code="invalid_chunk_config",
        )


# ── 网页抓取 SSRF 防护 ─────────────────────────────────────────────────────

_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "169.254.169.254"}
_MAX_REDIRECTS = 5
_MAX_BODY_BYTES = 5 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "text/plain", "text/markdown",
)


@dataclass
class FetchedPage:
    url: str
    final_url: str
    title: str
    text: str
    content_type: str


def _assert_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise KnowledgeSecurityError("仅允许抓取 http / https 网页", code="ssrf_blocked")
    if not parsed.hostname:
        raise KnowledgeSecurityError("网页 URL 缺少主机名", code="ssrf_blocked")
    if parsed.username or parsed.password:
        raise KnowledgeSecurityError("网页 URL 不允许携带认证信息", code="ssrf_blocked")
    host = parsed.hostname.lower()
    if host in _METADATA_HOSTS:
        raise KnowledgeSecurityError("禁止访问云元数据地址", code="ssrf_blocked")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                  proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise KnowledgeSecurityError(f"域名解析失败：{host}", code="ssrf_blocked") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise KnowledgeSecurityError(
                f"禁止访问内网 / 回环 / 保留地址：{host} -> {ip}", code="ssrf_blocked"
            )
    return urlunparse(parsed)


class _PageTextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.texts: List[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self.texts.append(stripped)


async def fetch_webpage_safely(url: str) -> FetchedPage:
    """受控网页抓取：逐跳校验重定向、限制大小/类型/超时。"""
    current = _assert_public_url(url)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; GeneralAgentPlatform/2.0)"},
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            # 连接前再次解析校验（缩小 DNS Rebinding 窗口）
            _assert_public_url(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise KnowledgeSecurityError("重定向缺少 Location", code="fetch_failed")
                        current = _assert_public_url(str(httpx.URL(current).join(location)))
                        continue
                    if response.status_code >= 400:
                        raise KnowledgeSecurityError(
                            f"网页返回 HTTP {response.status_code}", code="fetch_failed"
                        )
                    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if content_type and content_type not in _ALLOWED_CONTENT_TYPES:
                        raise KnowledgeSecurityError(
                            f"不支持的网页内容类型：{content_type}", code="fetch_failed"
                        )
                    chunks: List[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_BODY_BYTES:
                            raise KnowledgeSecurityError(
                                "网页内容超过 5MB 上限", code="fetch_failed"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    try:
                        body = raw.decode(encoding, errors="replace")
                    except LookupError:
                        body = raw.decode("utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise KnowledgeSecurityError(f"网页抓取失败：{exc}", code="fetch_failed") from exc

            parser = _PageTextExtractor()
            parser.feed(body)
            return FetchedPage(
                url=url,
                final_url=current,
                title=(parser.title or current)[:200],
                text="\n".join(parser.texts)[:200_000],
                content_type=content_type or "text/html",
            )
    raise KnowledgeSecurityError("重定向次数超过上限", code="fetch_failed")
