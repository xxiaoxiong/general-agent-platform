"""在临时目录将 Word 转换成带原始排版和图片的 PDF。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import posixpath
import shutil
import signal
from tempfile import TemporaryDirectory
from urllib.parse import unquote
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from lxml import etree

from .errors import DocumentError
from .parser import DocumentParseError, validate_reference_file

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SAFE_FIELDS = {"PAGE", "NUMPAGES", "SECTION", "SECTIONPAGES", "PAGEREF", "REF", "TOC", "SEQ"}
_MAX_PREVIEW_BYTES = 24 * 1024**2
_PREVIEW_TIMEOUT_SECONDS = 45
_gate = asyncio.Semaphore(1)
_SECURE_PROFILE = """<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Writer/Content/Update">
  <prop oor:name="Link" oor:op="fuse"><value>0</value></prop>
 </item>
</oor:items>
"""


def _guard_preview_xml(name: str, content: bytes, members: set[str]) -> bytes:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(content, parser)
    changed = False
    if name.lower().endswith(".rels"):
        part_directory = posixpath.dirname(posixpath.dirname(name))
        for relationship in list(root):
            target = unquote(relationship.get("Target", ""))
            internal_part = posixpath.normpath(posixpath.join(part_directory, target)).lstrip("/")
            relation = relationship.get("Type", "").rsplit("/", 1)[-1].lower()
            if (
                relationship.get("TargetMode", "").lower() == "external"
                or target.startswith("//") or "\\" in target or ":" in target or internal_part not in members
                or relation in {"oleobject", "package", "attachedtemplate", "afchunk"}
            ):
                root.remove(relationship)
                changed = True
    else:
        for field in root.iter(f"{_WORD_NS}instrText", f"{_WORD_NS}fldSimple"):
            is_simple = field.tag.endswith("fldSimple")
            instruction = field.get(f"{_WORD_NS}instr") if is_simple else field.text
            tokens = (instruction or "").strip().split()
            if tokens and tokens[0].upper() not in _SAFE_FIELDS:
                if is_simple:
                    field.set(f"{_WORD_NS}instr", "")
                else:
                    field.text = ""
                changed = True
    return etree.tostring(root, encoding="utf-8") if changed else content


def _prepare_preview(path: Path, target: Path) -> None:
    try:
        validate_reference_file(path)
        with ZipFile(path) as source, ZipFile(target, "w", ZIP_DEFLATED) as draft:
            members = set(source.namelist())
            for member in source.infolist():
                name = member.filename.lower()
                if name.startswith("word/embeddings/"):
                    continue
                payload = source.read(member)
                if name.endswith(".rels") or (name.startswith("word/") and name.endswith(".xml")):
                    payload = _guard_preview_xml(member.filename, payload, members)
                draft.writestr(member.filename, payload)
    except (DocumentParseError, etree.XMLSyntaxError, BadZipFile, OSError) as exc:
        raise DocumentError(422, "无法准备 Word 版式预览，请尝试下载原件") from exc


async def render_docx_pdf(path: Path) -> bytes:
    """转换产生短期私有副本，保留原件字节及版本记录。"""
    if path.suffix.lower() != ".docx":
        raise DocumentError(422, "版式预览支持 DOCX 文件")
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise DocumentError(503, "此部署暂未启用 Word 版式预览")
    async with _gate:
        with TemporaryDirectory(prefix="word-preview-") as temporary:
            directory = Path(temporary)
            source = directory / "document.docx"
            await asyncio.to_thread(_prepare_preview, path, source)
            profile = directory / "profile"
            (profile / "user").mkdir(parents=True)
            (profile / "user" / "registrymodifications.xcu").write_text(_SECURE_PROFILE, encoding="utf-8")
            # 单独进程组确保超时或请求取消时回收 soffice 启动的子进程。
            use_process_group = os.name == "posix"
            limiter = shutil.which("prlimit") if use_process_group else None
            limits = [limiter, "--as=1610612736", f"--fsize={_MAX_PREVIEW_BYTES}", "--"] if limiter else []
            process = await asyncio.create_subprocess_exec(
                *limits, executable, f"-env:UserInstallation={profile.as_uri()}",
                "--headless", "--norestore", "--nodefault", "--nofirststartwizard",
                "--convert-to", "pdf:writer_pdf_Export", "--outdir", temporary, str(source),
                cwd=temporary, start_new_session=use_process_group,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
                | {"HOME": temporary, "TMPDIR": temporary},
            )
            try:
                result = await asyncio.wait_for(process.wait(), timeout=_PREVIEW_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.CancelledError) as exc:
                try:
                    if use_process_group:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise DocumentError(504, "Word 版式预览耗时过长，请尝试下载原件") from exc
            output = directory / "document.pdf"
            if result or not output.is_file():
                raise DocumentError(422, "无法渲染此 Word 文档，请尝试下载原件")
            if not 0 < output.stat().st_size <= _MAX_PREVIEW_BYTES:
                raise DocumentError(422, "Word 版式预览大小超过上限，请下载原件")
            data = await asyncio.to_thread(output.read_bytes)
            if not data.startswith(b"%PDF-"):
                raise DocumentError(422, "Word 版式预览结果无效，请下载原件")
            return data
