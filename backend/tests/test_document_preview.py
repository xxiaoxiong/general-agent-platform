"""Word 版式预览保留内嵌图并隔离外部引用。"""

from io import BytesIO
import shutil
from zipfile import ZipFile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.shared import Inches
from PIL import Image
from pypdf import PdfReader
import pytest

from app.services.documents.office_preview import _prepare_preview, render_docx_pdf


def _document(path) -> None:
    doc = Document()
    doc.add_heading("Word layout check", 0)
    image = BytesIO()
    Image.new("RGB", (160, 100), "#36a7fa").save(image, format="PNG")
    doc.add_picture(image, width=Inches(2))
    doc.add_table(2, 2).cell(0, 0).text = "Status"
    doc.part.relate_to("https://example.test/remote.png", RELATIONSHIP_TYPE.IMAGE, is_external=True)
    run = doc.add_paragraph().add_run()
    for instruction in ("INCLUDETEXT \"file:///tmp/private-example.txt\"", " PAGE "):
        field = OxmlElement("w:instrText")
        field.text = instruction
        run._element.append(field)
    doc.save(path)


def test_safe_preview_copy_keeps_internal_package_data(tmp_path):
    source = tmp_path / "original.docx"
    prepared = tmp_path / "preview.docx"
    _document(source)
    original = source.read_bytes()
    _prepare_preview(source, prepared)
    with ZipFile(prepared) as archive:
        relationships = archive.read("word/_rels/document.xml.rels")
        body = archive.read("word/document.xml")
        assert b"https://example.test/remote.png" not in relationships
        assert b"media/image1.png" in relationships
        assert archive.read("word/media/image1.png").startswith(b"\x89PNG")
        assert b"private-example.txt" not in body and b" PAGE " in body
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (shutil.which("soffice") or shutil.which("libreoffice")),
    reason="实际 Word 渲染验证需要容器内 LibreOffice",
)
async def test_actual_word_layout_pdf_keeps_images_and_table(tmp_path):
    source = tmp_path / "original.docx"
    _document(source)
    data = await render_docx_pdf(source)
    assert data.startswith(b"%PDF-")
    pdf = PdfReader(BytesIO(data))
    page = pdf.pages[0]
    assert "Word layout check" in page.extract_text()
    assert "Status" in page.extract_text()
    objects = page["/Resources"]["/XObject"].get_object()
    assert any(obj.get_object().get("/Subtype") == "/Image" for obj in objects.values())
