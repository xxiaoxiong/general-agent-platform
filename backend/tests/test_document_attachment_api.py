"""会话附件入口与文档智能解析器的集成回归。"""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

from docx import Document
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from pptx import Presentation
from pypdf import PdfWriter
import pytest

from app.api.deps import get_current_user
from app.api.v2 import files as file_api
from app.core.database import get_db
from app.services.knowledge_service import KnowledgeService


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def application(tmp_path, monkeypatch):
    async def owned_session(db, session_id, user):
        if session_id != 1:
            raise HTTPException(404, "会话不存在")
        return SimpleNamespace(id=session_id, agent_id=2)

    monkeypatch.setattr(file_api, "_get_owned_session", owned_session)
    monkeypatch.setattr(file_api, "workspace_root", lambda *_: str(tmp_path))
    app = FastAPI()
    app.include_router(file_api.router, prefix="/api/v2")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=3)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(rollback=AsyncMock())
    return app


def _word() -> bytes:
    output = BytesIO()
    document = Document()
    document.add_heading("项目报告", 0)
    document.add_heading("实施进展", 1)
    document.add_paragraph("完成文档处理验证。")
    document.save(output)
    return output.getvalue()


def _upload_payload(extension: str) -> tuple[str, bytes, str]:
    stream = BytesIO()
    if extension == "docx":
        return "参考.docx", _word(), DOCX_MIME
    if extension == "pdf":
        pdf = PdfWriter()
        pdf.add_blank_page(400, 600)
        pdf.write(stream)
        return "参考.pdf", stream.getvalue(), "application/pdf"
    if extension == "pptx":
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[0])
        slide.shapes.title.text = "项目简报"
        deck.save(stream)
        return (
            "参考.pptx", stream.getvalue(),
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    workbook = Workbook()
    workbook.active.append(["指标", "数值"])
    workbook.active.append(["进度", 90])
    workbook.create_sheet("其他数据").append(["第二页"])
    workbook.save(stream)
    return (
        "参考.xlsx", stream.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension, kind, expected",
    [("docx", "office", "完成文档处理验证。"), ("pdf", "pdf", "扫描件"),
     ("pptx", "office", "项目简报"), ("xlsx", "table", "进度")],
)
async def test_upload_retains_attachment_summary_and_guarded_preview(application, extension, kind, expected):
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        url = "/api/v2/conversations/1"
        filename, content, mime = _upload_payload(extension)
        uploaded = await client.post(f"{url}/files", files={"file": (filename, content, mime)})
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json() == {
            "name": filename, "path": f"uploads/{filename}",
            "text_path": f"uploads/{filename}.extracted.txt", "size": len(content),
        }
        original = await client.get(f"{url}/files/{filename}")
        assert original.content == content
        extracted = await client.get(f"{url}/file-preview", params={"path": uploaded.json()["text_path"]})
        assert expected in extracted.json()["text"]
        preview = await client.get(f"{url}/file-preview", params={"path": uploaded.json()["path"]})
        result = preview.json()
        assert result["kind"] == kind
        if kind == "office":
            assert expected in result["text"]
        if kind == "table":
            assert result["rows"] == [["指标", "数值"], ["进度", "90"]]
        assert (await client.delete(f"{url}/files/{filename}")).status_code == 200
        assert (await client.get(f"{url}/files/{filename}.extracted.txt")).status_code == 404


@pytest.mark.asyncio
async def test_rejects_invalid_office_upload_and_revalidates_legacy_preview(application, tmp_path, monkeypatch):
    archive = BytesIO()
    with ZipFile(archive, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", "<document/>")
        package.writestr("word/bad.xml", "<!DOCTYPE invalid><document/>")
    legacy = AsyncMock()
    monkeypatch.setattr(KnowledgeService, "extract_text", legacy)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        url = "/api/v2/conversations/1"
        for payload, mime, reason in (
            (archive.getvalue(), DOCX_MIME, "XML 文档类型"),
            (_word(), "text/plain", "类型与文件扩展名不符"),
            (b"this is not Word", DOCX_MIME, "Office 压缩包"),
        ):
            invalid = await client.post(f"{url}/files", files={"file": ("draft.docx", payload, mime)})
            assert invalid.status_code == 422
            assert reason in invalid.json()["detail"]
            assert list((tmp_path / "uploads").iterdir()) == []
        malformed = tmp_path / "uploads" / "older.docx"
        malformed.write_bytes(archive.getvalue())
        preview = await client.get(f"{url}/file-preview", params={"path": "uploads/older.docx"})
        assert preview.status_code == 422
        assert "XML 文档类型" in preview.json()["detail"]
        assert (await client.get("/api/v2/conversations/999/file-preview", params={
            "path": "uploads/older.docx",
        })).status_code == 404
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_files_legacy_extraction_and_sidecar_collision(application, monkeypatch):
    legacy = AsyncMock(return_value="音频识别结果。")
    monkeypatch.setattr(KnowledgeService, "extract_text", legacy)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        url = "/api/v2/conversations/1"
        user_text = await client.post(
            f"{url}/files", files={"file": ("same.docx.extracted.txt", b"user notes", "text/plain")},
        )
        assert user_text.json()["text_path"] is None
        word = await client.post(f"{url}/files", files={"file": ("same.docx", _word(), DOCX_MIME)})
        assert word.json()["name"] == "same-1.docx"
        assert (await client.get(f"{url}/files/same.docx.extracted.txt")).content == b"user notes"
        audio = await client.post(f"{url}/files", files={"file": ("voice.wav", b"wave data", "audio/wav")})
        assert audio.json()["text_path"] == "uploads/voice.wav.extracted.txt"
        assert legacy.await_args.args[-1] == "wav"


@pytest.mark.asyncio
async def test_word_layout_preview_validates_conversation_and_returns_pdf(application, monkeypatch):
    render = AsyncMock(return_value=b"%PDF-preview")
    monkeypatch.setattr(file_api, "render_docx_pdf", render)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        url = "/api/v2/conversations/1"
        await client.post(f"{url}/files", files={"file": ("draft.docx", _word(), DOCX_MIME)})
        params = {"path": "uploads/draft.docx"}
        layout_metadata = (await client.get(f"{url}/file-preview", params={**params, "layout_only": True})).json()
        assert layout_metadata["kind"] == "office" and "text" not in layout_metadata
        foreign = await client.get("/api/v2/conversations/2/file-preview-pdf", params=params)
        escaped = await client.get(f"{url}/file-preview-pdf", params={"path": "uploads/../draft.docx"})
        assert (foreign.status_code, escaped.status_code) == (404, 400)
        render.assert_not_awaited()
        result = await client.get(f"{url}/file-preview-pdf", params=params)
        assert (result.status_code, result.headers["content-type"]) == (200, "application/pdf")
        assert result.headers["cache-control"] == "private, no-store"
        assert result.content == b"%PDF-preview"
        assert render.await_args.args[0].name == "draft.docx"
