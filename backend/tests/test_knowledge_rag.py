"""Knowledge Ingestion & RAG 测试。

覆盖：
- 真实解析（Markdown / DOCX / XLSX / PPTX / CSV / PDF / 图片 OCR）
- 结构感知 Chunking（Parent-Child / 来源定位 / 去重）
- Durable Ingestion（Job / Lease / 幂等 / 失败状态）
- 真实 Embedding（fastembed）+ 真实 Qdrant（本地引擎）+ Hybrid Retrieval
- API ACL（IDOR）、Magic Bytes、SSRF、Chunk 配置 422
- Agent Tool（多知识库 / 引用 / 越权拒绝）、评测指标
"""
from __future__ import annotations

import asyncio
import io
import os
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.security import create_access_token
from app.models.knowledge import (
    KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
)
from app.models.user import User
from app.services.knowledge import embedding as embedding_module
from app.services.knowledge import vector_store as vector_module
from app.services.knowledge.ingestion import (
    IngestionRunner, claim_job, queue_ingest, utcnow,
)
from app.services.knowledge.retrieval import KnowledgeRetrievalService, RetrievalOptions

pytestmark = pytest.mark.asyncio


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


async def _create_user(username: str) -> int:
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if existing is not None:
            return existing.id
        user = User(
            username=username, email=f"{username}@test.dev",
            hashed_password="x", is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


@pytest_asyncio.fixture
async def rag_env(tmp_path, monkeypatch):
    """隔离存储 / Qdrant 内存引擎 / 本地 Embedding / 原生解析器。"""
    await engine.dispose()
    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(settings, "KNOWLEDGE_QDRANT_URL", ":memory:")
    monkeypatch.setattr(settings, "KNOWLEDGE_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(settings, "KNOWLEDGE_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    monkeypatch.setattr(settings, "KNOWLEDGE_EMBEDDING_DIMENSION", 512)
    monkeypatch.setattr(settings, "KNOWLEDGE_EMBEDDING_BATCH_SIZE", 8)
    monkeypatch.setattr(settings, "KNOWLEDGE_PARSER_PROVIDER", "native")
    monkeypatch.setattr(settings, "KNOWLEDGE_OCR_ENABLED", False)
    monkeypatch.setattr(settings, "KNOWLEDGE_RERANKER_ENABLED", False)
    monkeypatch.setattr(settings, "KNOWLEDGE_LEGACY_FALLBACK", False)
    monkeypatch.setattr(settings, "KNOWLEDGE_MIN_DENSE_SCORE", 0.35)
    monkeypatch.setattr(settings, "KNOWLEDGE_MIN_LEXICAL_COVERAGE", 0.2)
    vector_module.reset_client_for_tests()
    embedding_module.reset_provider_for_tests()
    from app.main import app

    from app.core.database import init_db

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()
    vector_module.reset_client_for_tests()
    embedding_module.reset_provider_for_tests()


async def _create_base(client, headers, name: str) -> int:
    response = await client.post("/api/v1/knowledge/bases", json={
        "name": name, "description": "",
    }, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _upload(client, headers, kb_id: int, filename: str, content: bytes,
                  mime: str = "text/markdown"):
    response = await client.post(
        f"/api/v1/knowledge/bases/{kb_id}/documents",
        files={"file": (filename, content, mime)}, headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _run_worker() -> None:
    worker_id = f"test-{uuid.uuid4().hex[:8]}"
    for _ in range(12):
        async with AsyncSessionLocal() as db:
            job = await claim_job(db, worker_id, lease_seconds=60)
        if job is None:
            return
        await IngestionRunner(worker_id).run(job.id)


MANUAL_MD = """# 设备运行手册

## 第三章 设备维护

设备应每隔六个月进行一次常规检查，检查内容包括润滑、紧固与电气绝缘。

### 3.1 年度检修

每年十二月进行年度检修，需要更换密封件并记录检修报告编号。

| 检查项 | 周期 | 负责人 |
| --- | --- | --- |
| 润滑 | 6 个月 | 张工 |
| 绝缘测试 | 12 个月 | 李工 |

## 第四章 故障处理

出现 E-2048 错误码时，先检查冷却液液位，再复位控制器。
"""


def _make_text_pdf(pages: list[str]) -> bytes:
    """生成带文本层的真实 PDF（pypdf 写入内容流，用于页码来源测试）。"""
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject, DictionaryObject, NameObject,
    )

    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        })
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _make_blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _make_image_pdf(text: str) -> bytes:
    """扫描件 PDF：页面只有图片（无可提取文本层），必须走 OCR。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (700, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 70), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PDF")
    return buffer.getvalue()


async def test_ingestion_retrieval_citation_and_acl(rag_env):
    client = rag_env
    owner_id = await _create_user("kb-owner")
    other_id = await _create_user("kb-other")
    headers = _auth(owner_id)
    other_headers = _auth(other_id)

    kb_id = await _create_base(client, headers, "设备知识库")
    uploaded = await _upload(client, headers, kb_id, "设备运行手册.md", MANUAL_MD.encode("utf-8"))
    assert uploaded["status"] in {"queued", "pending"}
    assert uploaded["job"]["status"] == "queued"

    await _run_worker()

    detail = await client.get(f"/api/v1/knowledge/bases/{kb_id}/documents/{uploaded['id']}",
                              headers=headers)
    assert detail.status_code == 200, detail.text
    doc = detail.json()
    assert doc["status"] == "ready", doc
    assert doc["element_count"] >= 5
    assert doc["chunk_count"] >= 1

    job = await client.get(f"/api/v1/knowledge/documents/{uploaded['id']}/ingestion",
                           headers=headers)
    assert job.status_code == 200
    assert job.json()["status"] == "succeeded"
    assert job.json()["stage"] == "ready"

    preview = await client.get(
        f"/api/v1/knowledge/documents/{uploaded['id']}/parsed-preview", headers=headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    types = {element["type"] for element in body["elements"]}
    assert "heading" in types and "table" in types
    heading = next(e for e in body["elements"] if e["text"].startswith("第三章"))
    assert heading["heading_path"][-1].startswith("第三章")

    chunks = await client.get(
        f"/api/v1/knowledge/documents/{uploaded['id']}/chunks", headers=headers,
    )
    assert chunks.status_code == 200, chunks.text
    chunk_items = chunks.json()["items"]
    assert chunk_items
    assert any(item["heading_path"] for item in chunk_items)

    # 语义检索：问法与原文不同（"多久检查一次" vs "每隔六个月"）
    search = await client.post("/api/v1/knowledge/search", json={
        "query": "设备多久需要检查一次？", "knowledge_base_ids": [kb_id],
        "top_k": 3, "mode": "hybrid",
    }, headers=headers)
    assert search.status_code == 200, search.text
    result = search.json()
    assert result["total"] >= 1, result
    top = result["results"][0]
    assert top["citation"]["filename"] == "设备运行手册.md"
    assert "六个月" in top["content"] or "六个月" in top["context"]
    assert result["candidates"]["bm25"] >= 1

    # 精确编号检索（BM25 路径）
    code_search = await client.post("/api/v1/knowledge/search", json={
        "query": "E-2048 错误码怎么处理", "knowledge_base_ids": [kb_id],
        "top_k": 3, "mode": "bm25",
    }, headers=headers)
    assert code_search.status_code == 200
    code_results = code_search.json()["results"]
    assert code_results and "E-2048" in code_results[0]["content"]

    # 无证据：返回空，不瞎猜
    empty = await client.post("/api/v1/knowledge/search", json={
        "query": "量子纠缠煎饼的配方是什么", "knowledge_base_ids": [kb_id],
        "top_k": 3, "mode": "hybrid",
    }, headers=headers)
    assert empty.status_code == 200
    assert empty.json()["total"] == 0

    # ── ACL / IDOR ──────────────────────────────────────────────
    assert (await client.get(f"/api/v1/knowledge/bases/{kb_id}",
                             headers=other_headers)).status_code == 404
    assert (await client.put(f"/api/v1/knowledge/bases/{kb_id}", json={"name": "x"},
                             headers=other_headers)).status_code == 404
    assert (await client.get(f"/api/v1/knowledge/bases/{kb_id}/documents",
                             headers=other_headers)).status_code == 404
    assert (await client.post("/api/v1/knowledge/search", json={
        "query": "维护", "knowledge_base_ids": [kb_id],
    }, headers=other_headers)).status_code == 404
    forbidden_upload = await client.post(
        f"/api/v1/knowledge/bases/{kb_id}/documents",
        files={"file": ("x.md", b"# x", "text/markdown")}, headers=other_headers,
    )
    assert forbidden_upload.status_code == 404
    assert (await client.delete(f"/api/v1/knowledge/bases/{kb_id}/documents/{uploaded['id']}",
                                headers=other_headers)).status_code == 404

    # ── 删除一致性 ──────────────────────────────────────────────
    deleted = await client.delete(
        f"/api/v1/knowledge/bases/{kb_id}/documents/{uploaded['id']}", headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert not (Path(settings.KNOWLEDGE_STORAGE_DIR) / f"kb_{kb_id}"
                / f"doc_{uploaded['id']}").exists()
    after = await client.post("/api/v1/knowledge/search", json={
        "query": "设备多久需要检查一次？", "knowledge_base_ids": [kb_id],
        "top_k": 3, "mode": "hybrid",
    }, headers=headers)
    assert after.status_code == 200
    assert after.json()["total"] == 0


async def test_upload_rejects_magic_mismatch_and_bad_chunk_config(rag_env):
    client = rag_env
    user_id = await _create_user("kb-security")
    headers = _auth(user_id)

    bad_config = await client.post("/api/v1/knowledge/bases", json={
        "name": "非法切分", "chunk_size_tokens": 100, "chunk_overlap_tokens": 100,
    }, headers=headers)
    assert bad_config.status_code == 422

    kb_id = await _create_base(client, headers, "安全知识库")
    fake_pdf = await client.post(
        f"/api/v1/knowledge/bases/{kb_id}/documents",
        files={"file": ("fake.pdf", b"this is not a pdf", "application/pdf")},
        headers=headers,
    )
    assert fake_pdf.status_code == 400
    assert "PDF" in fake_pdf.text or "Magic" in fake_pdf.text

    unsupported = await client.post(
        f"/api/v1/knowledge/bases/{kb_id}/documents",
        files={"file": ("run.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=headers,
    )
    assert unsupported.status_code == 400


async def test_ssrf_blocks_internal_targets(rag_env):
    from app.services.knowledge.security import KnowledgeSecurityError, fetch_webpage_safely

    for url in (
        "http://127.0.0.1/", "http://localhost:8000/", "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/", "file:///etc/passwd",
    ):
        with pytest.raises(KnowledgeSecurityError):
            await fetch_webpage_safely(url)


async def test_job_lease_recovery_and_idempotency(rag_env):
    client = rag_env
    user_id = await _create_user("kb-worker")
    headers = _auth(user_id)
    kb_id = await _create_base(client, headers, "任务队列")
    await _upload(client, headers, kb_id, "lease.md", b"# Lease\n\nlease recovery body.")

    async with AsyncSessionLocal() as db:
        job = (await db.execute(
            select(KnowledgeIngestionJob).where(KnowledgeIngestionJob.knowledge_base_id == kb_id)
        )).scalars().first()
        assert job is not None
        # 模拟 Worker 领取后崩溃：running + 过期租约
        job.status = "running"
        job.stage = "parsing"
        job.attempt = 1
        job.lease_owner = "dead-worker"
        job.lease_token = "dead-token"
        job.lease_expires_at = utcnow().replace(year=utcnow().year - 1)
        db.add(job)
        await db.commit()
        job_id = job.id

    async with AsyncSessionLocal() as db:
        claimed = await claim_job(db, "recovery-worker", lease_seconds=60)
        assert claimed is not None and claimed.id == job_id
        assert claimed.lease_owner == "recovery-worker"
        assert claimed.attempt == 2

    # 同文档版本重复入队：返回同一 Job（幂等）
    async with AsyncSessionLocal() as db:
        document = (await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id)
        )).scalars().one()
        version = (await db.execute(
            select(KnowledgeDocumentVersion).where(
                KnowledgeDocumentVersion.document_id == document.id
            )
        )).scalars().one()
        kb = await db.get(KnowledgeBase, kb_id)
        first = await queue_ingest(db, knowledge_base=kb, document=document, version=version)
        second = await queue_ingest(db, knowledge_base=kb, document=document, version=version)
        first_id, second_id = first.id, second.id
        await db.rollback()
        assert first_id == second_id


async def test_parser_structures_docx_xlsx_pptx_csv_and_chunker(tmp_path):
    """真实文件结构解析：标题 / 表格 / Sheet / Slide / 编码 / Parent-Child。"""
    from app.services.knowledge.parsers.native import NativeParser
    from app.services.knowledge.parsers.base import ParseOptions

    parser = NativeParser()
    options = ParseOptions(ocr_enabled=False)
    document_id = 1

    # DOCX
    from docx import Document as DocxDocument

    docx_path = tmp_path / "manual.docx"
    docx = DocxDocument()
    docx.add_heading("设备维护手册", level=1)
    docx.add_heading("维护周期", level=2)
    docx.add_paragraph("设备每六个月检查一次。")
    docx.add_paragraph("润滑检查", style="List Bullet")
    table = docx.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "检查项"
    table.cell(0, 1).text = "周期"
    table.cell(1, 0).text = "润滑"
    table.cell(1, 1).text = "6 个月"
    docx.save(str(docx_path))
    parsed = await parser.parse(
        docx_path, file_type="docx", document_id=document_id,
        source_name="manual.docx", options=options,
    )
    assert parsed.parse_stats["table_count"] == 1
    assert any(e.type == "heading" for e in parsed.elements)
    table_element = next(e for e in parsed.elements if e.type == "table")
    assert table_element.table["headers"] == ["检查项", "周期"]
    assert table_element.table["rows"][0] == ["润滑", "6 个月"]

    # XLSX（多 Sheet + 合并单元格 + 公式）
    import openpyxl

    xlsx_path = tmp_path / "data.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "设备台账"
    ws.append(["设备编号", "检查周期", "备注"])
    ws.append(["EQ-001", 6, "每年校准"])
    ws.append(["EQ-002", 12, None])
    ws["D1"] = "合计"
    ws["D2"] = "=B2+B3"
    ws.merge_cells("A4:B4")
    ws2 = wb.create_sheet("参数")
    ws2.append(["参数", "值"])
    ws2.append(["温度", 25])
    wb.save(str(xlsx_path))
    parsed = await parser.parse(
        xlsx_path, file_type="xlsx", document_id=document_id,
        source_name="data.xlsx", options=options,
    )
    assert parsed.parse_stats["sheet_count"] == 2
    assert "合计" in "\n".join(e.text for e in parsed.elements)
    assert "=B2+B3" in "\n".join(e.text for e in parsed.elements)
    assert any(e.provenance.row_start for e in parsed.elements if e.type == "table")

    # PPTX
    from pptx import Presentation

    pptx_path = tmp_path / "slides.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "年度检修计划"
    body = slide.shapes.add_textbox(100, 100, 500, 100).text_frame
    body.text = "十二月开展年度检修"
    table_shape = slide.shapes.add_table(2, 2, 100, 250, 400, 100)
    table_shape.table.cell(0, 0).text = "月份"
    table_shape.table.cell(0, 1).text = "工作"
    table_shape.table.cell(1, 0).text = "12 月"
    table_shape.table.cell(1, 1).text = "年度检修"
    prs.save(str(pptx_path))
    parsed = await parser.parse(
        pptx_path, file_type="pptx", document_id=document_id,
        source_name="slides.pptx", options=options,
    )
    assert parsed.parse_stats["slide_count"] == 1
    assert any(e.provenance.slide_number == 1 for e in parsed.elements)
    assert any(e.type == "table" for e in parsed.elements)

    # CSV（GBK 编码）
    csv_path = tmp_path / "gbk.csv"
    csv_path.write_bytes("设备编号,检查周期\nEQ-001,6\nEQ-002,12\n".encode("gbk"))
    parsed = await parser.parse(
        csv_path, file_type="csv", document_id=document_id,
        source_name="gbk.csv", options=options,
    )
    assert any(e.type == "table" and "设备编号" in e.text for e in parsed.elements)

    # Markdown：代码块 / 多级标题 / 表格
    md_path = tmp_path / "notes.md"
    md_path.write_text(
        "# 手册\n\n## 代码示例\n\n```python\nprint('hello')\n```\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    parsed = await parser.parse(
        md_path, file_type="md", document_id=document_id,
        source_name="notes.md", options=options,
    )
    assert any(e.type == "code" for e in parsed.elements)
    assert any(e.type == "table" for e in parsed.elements)
    assert [e.level for e in parsed.elements if e.type == "heading"] == [1, 2]

    # HTML：标题 / 正文 / 表格；脚本与导航必须被丢弃
    html_path = tmp_path / "page.html"
    html_path.write_text(
        "<html><head><title>设备手册</title><script>alert('x')</script></head>"
        "<body><nav>导航</nav><h1>维护说明</h1><p>每六个月检查一次。</p>"
        "<table><tr><th>项目</th><th>周期</th></tr>"
        "<tr><td>润滑</td><td>6 个月</td></tr></table></body></html>",
        encoding="utf-8",
    )
    parsed = await parser.parse(
        html_path, file_type="html", document_id=document_id,
        source_name="page.html", options=options,
    )
    text = "\n".join(e.text for e in parsed.elements)
    assert "维护说明" in text and "润滑" in text
    assert "alert" not in text and "导航" not in text
    assert any(e.type == "table" for e in parsed.elements)

    # ODT：content.xml 标题 / 段落 / 表格
    import zipfile

    odt_path = tmp_path / "doc.odt"
    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        '<office:body><office:text>'
        '<text:h text:outline-level="1">设备手册</text:h>'
        '<text:p>每六个月检查一次。</text:p>'
        '<table:table><table:table-row>'
        '<table:table-cell><text:p>项目</text:p></table:table-cell>'
        '<table:table-cell><text:p>周期</text:p></table:table-cell>'
        '</table:table-row><table:table-row>'
        '<table:table-cell><text:p>润滑</text:p></table:table-cell>'
        '<table:table-cell><text:p>6 个月</text:p></table:table-cell>'
        '</table:table-row></table:table>'
        '</office:text></office:body></office:document-content>'
    )
    with zipfile.ZipFile(odt_path, "w") as zf:
        zf.writestr("content.xml", content_xml)
    parsed = await parser.parse(
        odt_path, file_type="odt", document_id=document_id,
        source_name="doc.odt", options=options,
    )
    assert any(e.type == "heading" and e.text == "设备手册" for e in parsed.elements)
    assert any(e.type == "table" and "润滑" in e.text for e in parsed.elements)

    # RTF：基础文本
    rtf_path = tmp_path / "note.rtf"
    rtf_path.write_bytes(b"{\\rtf1\\ansi Equipment maintenance every six months.\\par}")
    parsed = await parser.parse(
        rtf_path, file_type="rtf", document_id=document_id,
        source_name="note.rtf", options=options,
    )
    assert "Equipment maintenance" in "\n".join(e.text for e in parsed.elements)

    # 结构感知 Chunking：长小节 → Parent + Child；来源继承
    from app.services.knowledge.chunking import ChunkingConfig, StructureAwareChunker

    long_text = "设备维护需要定期检查。" * 400
    chunker = StructureAwareChunker(ChunkingConfig(
        chunk_size_tokens=80, chunk_overlap_tokens=10, parent_max_tokens=240,
    ))
    chunks = chunker.chunk(
        parsed, knowledge_base_id=1, document_id=2,
        document_version_id=3, index_generation_id=4,
    )
    assert chunks
    # 这里用长文本单独构建一个文档验证 Parent-Child
    from app.services.knowledge.model import ParsedDocument, ParsedElement, Provenance

    long_doc = ParsedDocument(
        document_id=9, source_name="long.txt", source_type="txt", title="long",
        elements=[
            ParsedElement(element_id="el_1", type="heading", text="长章节",
                          heading_path=["长章节"], level=1, provenance=Provenance(page=1)),
            ParsedElement(element_id="el_2", type="paragraph", text=long_text,
                          heading_path=["长章节"], provenance=Provenance(page=1)),
        ],
    )
    children = chunker.chunk(
        long_doc, knowledge_base_id=1, document_id=9,
        document_version_id=1, index_generation_id=1,
    )
    parents = [c for c in children if c.is_parent]
    kids = [c for c in children if c.parent_chunk_id]
    assert parents and kids
    assert all(k.parent_chunk_id in {p.chunk_id for p in parents} for k in kids)
    assert all(k.provenance.get("page") == 1 for k in kids)


async def test_pdf_page_provenance_and_scanned_ocr(tmp_path, monkeypatch):
    """PDF 页码来源；扫描 PDF 无文本层时触发 OCR 并记录页码与置信度。"""
    from app.services.knowledge.parsers.base import ParseOptions
    from app.services.knowledge.parsers.native import NativeParser
    from app.services.knowledge.ocr import ocr_available

    monkeypatch.setattr(settings, "KNOWLEDGE_OCR_ENABLED", True)
    parser = NativeParser()

    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(_make_text_pdf([
        "Chapter 1 Overview",
        "Equipment must be inspected every six months.",
    ]))
    parsed = await parser.parse(
        pdf_path, file_type="pdf", document_id=1, source_name="manual.pdf",
        options=ParseOptions(ocr_enabled=True),
    )
    pages = {e.provenance.page for e in parsed.elements}
    assert pages == {1, 2}
    assert parsed.parse_stats["page_count"] == 2

    if not ocr_available():
        pytest.skip("RapidOCR 不可用，跳过扫描件 OCR")
    scanned = tmp_path / "scan.pdf"
    scanned.write_bytes(_make_image_pdf("MAINTENANCE EVERY SIX MONTHS"))
    parsed = await parser.parse(
        scanned, file_type="pdf", document_id=2, source_name="scan.pdf",
        options=ParseOptions(ocr_enabled=True),
    )
    assert 1 in parsed.ocr_pages
    assert "MAINTENANCE" in "\n".join(e.text for e in parsed.elements).upper()
    assert parsed.parse_stats["ocr_mean_score"] > 0.5


async def test_parse_failure_marks_failed_not_done(rag_env):
    """解析失败不能冒充成功：空 PDF / 无文本层 PNG 必须 failed 且带错误码。"""
    client = rag_env
    user_id = await _create_user("kb-failure")
    headers = _auth(user_id)
    kb_id = await _create_base(client, headers, "失败语义")

    blank = await _upload(client, headers, kb_id, "blank.pdf", _make_blank_pdf(),
                          "application/pdf")
    await _run_worker()
    doc = await client.get(
        f"/api/v1/knowledge/bases/{kb_id}/documents/{blank['id']}", headers=headers,
    )
    body = doc.json()
    assert body["status"] == "failed", body
    assert body["error_code"] in {"empty_extraction", "parse_failed"}, body
    job = await client.get(f"/api/v1/knowledge/documents/{blank['id']}/ingestion",
                           headers=headers)
    assert job.json()["status"] == "failed"
    assert job.json()["stage"] == "failed"


async def test_agent_tool_multi_kb_citation_and_revocation(rag_env):
    """Agent Tool：授权范围来自服务端配置；引用可定位；越权立即拒绝。"""
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.tools import ToolExecutor

    client = rag_env
    owner_id = await _create_user("kb-agent-owner")
    other_id = await _create_user("kb-agent-other")
    headers = _auth(owner_id)
    kb_id = await _create_base(client, headers, "Agent 知识库")
    await _upload(client, headers, kb_id, "manual.md", MANUAL_MD.encode("utf-8"))
    await _run_worker()

    config = {
        "knowledge_base_ids": [kb_id],
        "capability_ids": ["knowledge"],
    }
    async with AsyncSessionLocal() as db:
        ctx = RuntimeContext(
            scope=RunScope(user_id=owner_id), run_id="run-kb-test",
            thread_id="thread-kb-test", agent_id=1, agent_config=config,
        )
        executor = ToolExecutor(db, config, ctx=ctx)
        result = await executor.execute("knowledge_search", {
            "query": "设备多久需要检查一次？", "top_k": 3,
        })
        assert result.status == "succeeded", result.text
        assert "<knowledge_evidence>" in result.text
        assert "六个月" in result.text
        sources = result.metadata.get("sources") or []
        assert sources and sources[0]["kind"] == "knowledge"
        assert "manual.md" in sources[0]["title"]
        assert sources[0]["heading_path"]

        # 撤销授权（换成其他用户上下文）→ 下一次检索立即拒绝
        other_ctx = RuntimeContext(
            scope=RunScope(user_id=other_id), run_id="run-kb-other",
            thread_id="thread-kb-other", agent_id=1, agent_config=config,
        )
        other_executor = ToolExecutor(db, config, ctx=other_ctx)
        denied = await other_executor.execute("knowledge_search", {"query": "维护周期"})
        assert denied.status == "failed"
        assert "没有可访问的知识库" in denied.text


async def test_evaluation_metrics_and_rebuild_generation(rag_env):
    """评测指标可重复运行；重建生成新 Generation 并原子切换（旧索引保留回滚）。"""
    from app.services.knowledge.evaluation import EvaluationCase, evaluate

    client = rag_env
    user_id = await _create_user("kb-eval")
    headers = _auth(user_id)
    kb_id = await _create_base(client, headers, "评测知识库")
    uploaded = await _upload(client, headers, kb_id, "manual.md", MANUAL_MD.encode("utf-8"))
    await _run_worker()

    base = await client.get(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
    first_generation = base.json()["active_index_generation_id"]
    assert first_generation
    assert base.json()["indexed_chunk_count"] >= 1
    assert base.json()["vector_count"] >= 1

    async with AsyncSessionLocal() as db:
        metrics = await evaluate(
            db, knowledge_base_ids=[kb_id],
            cases=[
                EvaluationCase(question="设备多久需要检查一次？",
                               expected_document_ids=[uploaded["id"]]),
                EvaluationCase(question="E-2048 错误码怎么处理",
                               expected_document_ids=[uploaded["id"]]),
                EvaluationCase(question="量子纠缠煎饼配方", no_answer=True),
            ],
            mode="hybrid", top_k=3,
        )
    assert metrics["recall_at_5"] >= 0.66, metrics
    assert metrics["mrr_at_10"] > 0
    assert metrics["no_answer_accuracy"] == 1.0, metrics

    # API 评测持久化
    evaluation = await client.post(f"/api/v1/knowledge/bases/{kb_id}/evaluations", json={
        "name": "基线",
        "cases": [{
            "question": "设备多久需要检查一次？",
            "expected_document_ids": [uploaded["id"]],
        }],
        "mode": "hybrid", "top_k": 3,
    }, headers=headers)
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["status"] == "completed"
    assert evaluation.json()["metrics"]["recall_at_5"] == 1.0

    # 重建索引：新 Generation
    rebuild = await client.post(f"/api/v1/knowledge/bases/{kb_id}/rebuild", headers=headers)
    assert rebuild.status_code == 200, rebuild.text
    new_generation = rebuild.json()["generation_id"]
    assert new_generation != first_generation
    await _run_worker()

    refreshed = await client.get(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
    assert refreshed.json()["active_index_generation_id"] == new_generation
    search = await client.post("/api/v1/knowledge/search", json={
        "query": "年度检修在几月", "knowledge_base_ids": [kb_id], "top_k": 3,
    }, headers=headers)
    assert search.status_code == 200
    assert search.json()["total"] >= 1

    async with AsyncSessionLocal() as db:
        from app.models.knowledge import KnowledgeIndexGeneration

        old = await db.get(KnowledgeIndexGeneration, first_generation)
        assert old.status == "retired"

    # 删除知识库：文档 / Chunk / 向量全部清理
    deleted = await client.delete(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    missing = await client.get(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
    assert missing.status_code == 404
    async with AsyncSessionLocal() as db:
        chunks = (await db.execute(
            select(func.count()).select_from(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_base_id == kb_id
            )
        )).scalar()
        assert chunks == 0


async def test_reindex_replaces_chunks_without_duplicates(rag_env):
    """同一文档重跑：旧版本 Chunk 在同代际内被替换，不产生重复。"""
    client = rag_env
    user_id = await _create_user("kb-reindex")
    headers = _auth(user_id)
    kb_id = await _create_base(client, headers, "重建去重")
    uploaded = await _upload(client, headers, kb_id, "manual.md", MANUAL_MD.encode("utf-8"))
    await _run_worker()

    async with AsyncSessionLocal() as db:
        before = (await db.execute(
            select(func.count()).select_from(KnowledgeChunk).where(
                KnowledgeChunk.document_id == uploaded["id"],
                KnowledgeChunk.is_parent.is_(False),
            )
        )).scalar()

    reindexed = await client.post(
        f"/api/v1/knowledge/documents/{uploaded['id']}/reindex", json={}, headers=headers,
    )
    assert reindexed.status_code == 200, reindexed.text
    await _run_worker()
    async with AsyncSessionLocal() as db:
        after = (await db.execute(
            select(func.count()).select_from(KnowledgeChunk).where(
                KnowledgeChunk.document_id == uploaded["id"],
                KnowledgeChunk.is_parent.is_(False),
            )
        )).scalar()
    assert after == before
    document = await client.get(
        f"/api/v1/knowledge/bases/{kb_id}/documents/{uploaded['id']}", headers=headers,
    )
    assert document.json()["status"] == "ready"


async def test_qdrant_local_persistence_across_client_restart(tmp_path, monkeypatch):
    """Qdrant 本地文件模式：客户端重建（模拟重启）后索引仍在。"""
    from app.services.knowledge import vector_store

    monkeypatch.setattr(settings, "KNOWLEDGE_STORAGE_DIR", str(tmp_path / "kb"))
    monkeypatch.setattr(settings, "KNOWLEDGE_QDRANT_URL", "")
    vector_store.reset_client_for_tests()
    store = vector_store.get_vector_store()
    await store.ensure_collection("persist_test", 4)
    await store.upsert("persist_test", [{
        "chunk_id": "a" * 40,
        "vector": [1.0, 0.0, 0.0, 0.0],
        "payload": {"knowledge_base_id": 1, "index_generation_id": 1, "chunk_id": "a" * 40},
    }])
    assert await store.count("persist_test") == 1

    vector_store.reset_client_for_tests()
    store2 = vector_store.get_vector_store()
    assert await store2.count("persist_test") == 1
    hits = await store2.search(
        "persist_test", [1.0, 0.0, 0.0, 0.0], limit=1,
        flt=vector_store.build_filter(knowledge_base_ids=[1], generation_ids=[1]),
    )
    assert hits and hits[0][0] == "a" * 40
    vector_store.reset_client_for_tests()


async def test_worker_retry_then_fail(rag_env, monkeypatch):
    """可重试异常按 attempt 退避重试，超过 max_attempts 标记 failed。"""
    client = rag_env
    user_id = await _create_user("kb-retry")
    headers = _auth(user_id)
    kb_id = await _create_base(client, headers, "重试队列")
    await _upload(client, headers, kb_id, "retry.md", b"# Retry\n\nbody")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("transient upstream failure")

    monkeypatch.setattr(IngestionRunner, "_pipeline", _boom)
    for expected_attempt in (1, 2):
        worker = f"retry-{expected_attempt}"
        async with AsyncSessionLocal() as db:
            job = await claim_job(db, worker, lease_seconds=60)
        assert job is not None
        await IngestionRunner(worker).run(job.id)
        async with AsyncSessionLocal() as db:
            row = await db.get(KnowledgeIngestionJob, job.id)
            assert row.status == "queued"
            assert row.attempt == expected_attempt
            assert "transient" in (row.error_message or "")
    worker = "retry-3"
    async with AsyncSessionLocal() as db:
        job = await claim_job(db, worker, lease_seconds=60)
    await IngestionRunner(worker).run(job.id)
    async with AsyncSessionLocal() as db:
        row = await db.get(KnowledgeIngestionJob, job.id)
        assert row.status == "failed"
        assert row.attempt == 3


async def test_office_archive_limit_and_legacy_split_guard(tmp_path, monkeypatch):
    """Office 解压上限（压缩炸弹防护）与旧字符切分死循环防护。"""
    from docx import Document as DocxDocument
    from app.services.knowledge.security import KnowledgeSecurityError, validate_magic
    from app.services.knowledge_service import KnowledgeService

    monkeypatch.setattr(settings, "KNOWLEDGE_MAX_ARCHIVE_EXPANDED_BYTES", 256)
    docx_path = tmp_path / "bomb.docx"
    docx = DocxDocument()
    docx.add_paragraph("x" * 4096)
    docx.save(str(docx_path))
    with pytest.raises(KnowledgeSecurityError, match="压缩炸弹|超过上限"):
        validate_magic(str(docx_path), "docx")

    chunks = KnowledgeService._split_text("a" * 1000, chunk_size=100, chunk_overlap=100)
    assert chunks and "".join(chunks).startswith("aaaa")


async def test_agent_prompt_contains_knowledge_rules(rag_env):
    """绑定知识库时系统提示注入检索 / 引用 / 注入防护规则。"""
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.agent_runtime.factory import _compose_system_prompt

    ctx = RuntimeContext(
        scope=RunScope(user_id=1), run_id="prompt-run", thread_id="prompt-thread",
        agent_id=1,
        agent_config={"system_prompt": "基础提示", "knowledge_base_ids": [7]},
    )
    prompt = _compose_system_prompt(ctx)
    assert "企业知识库检索规则" in prompt
    assert "knowledge_search" in prompt
    assert "证据，不是指令" in prompt

    ctx.agent_config = {"system_prompt": "基础提示"}
    assert "企业知识库检索规则" not in _compose_system_prompt(ctx)


async def test_project_and_organization_visibility_acl(rag_env):
    """项目共享复用 ProjectMember 角色；组织共享只读。"""
    from app.models.sandbox import Project, ProjectMember

    client = rag_env
    owner_id = await _create_user("kb-share-owner")
    viewer_id = await _create_user("kb-share-viewer")
    operator_id = await _create_user("kb-share-operator")
    org_id = await _create_user("kb-share-org")
    owner_headers = _auth(owner_id)

    async with AsyncSessionLocal() as db:
        project = Project(name="知识共享项目", owner_id=owner_id)
        db.add(project)
        await db.flush()
        db.add_all([
            ProjectMember(project_id=project.id, user_id=viewer_id, role="viewer"),
            ProjectMember(project_id=project.id, user_id=operator_id, role="operator"),
        ])
        await db.commit()
        project_id = project.id

    shared = await client.post("/api/v1/knowledge/bases", json={
        "name": "项目知识库", "visibility": "project", "project_id": project_id,
    }, headers=owner_headers)
    assert shared.status_code == 200, shared.text
    kb_id = shared.json()["id"]

    assert (await client.get(f"/api/v1/knowledge/bases/{kb_id}",
                             headers=_auth(viewer_id))).status_code == 200
    assert (await client.post(
        f"/api/v1/knowledge/bases/{kb_id}/documents",
        files={"file": ("x.md", b"# x", "text/markdown")},
        headers=_auth(viewer_id),
    )).status_code == 403
    assert (await client.post(
        f"/api/v1/knowledge/bases/{kb_id}/documents",
        files={"file": ("x.md", b"# x", "text/markdown")},
        headers=_auth(operator_id),
    )).status_code == 200
    assert (await client.delete(f"/api/v1/knowledge/bases/{kb_id}",
                                headers=_auth(operator_id))).status_code == 403

    org = await client.post("/api/v1/knowledge/bases", json={
        "name": "组织知识库", "visibility": "organization",
    }, headers=owner_headers)
    org_kb = org.json()["id"]
    assert (await client.get(f"/api/v1/knowledge/bases/{org_kb}",
                             headers=_auth(org_id))).status_code == 200
    assert (await client.put(f"/api/v1/knowledge/bases/{org_kb}", json={"name": "改"},
                             headers=_auth(org_id))).status_code == 403


async def test_image_ocr_parser(tmp_path, monkeypatch):
    """独立图片（PNG）走 OCR 主流程，记录页码与置信度。"""
    from PIL import Image, ImageDraw

    from app.services.knowledge.ocr import ocr_available
    from app.services.knowledge.parsers.base import ParseOptions
    from app.services.knowledge.parsers.native import NativeParser

    monkeypatch.setattr(settings, "KNOWLEDGE_OCR_ENABLED", True)
    if not ocr_available():
        pytest.skip("RapidOCR 不可用")
    image = Image.new("RGB", (700, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 70), "MAINTENANCE EVERY SIX MONTHS", fill="black")
    path = tmp_path / "scan.png"
    image.save(path)
    parsed = await NativeParser().parse(
        path, file_type="png", document_id=1, source_name="scan.png",
        options=ParseOptions(ocr_enabled=True),
    )
    assert 1 in parsed.ocr_pages
    assert "MAINTENANCE" in "\n".join(e.text for e in parsed.elements).upper()
    assert parsed.ocr_scores and parsed.ocr_scores[0] > 0.5


async def test_rebuild_generation_failure_keeps_old_active(rag_env):
    """新 Generation 无可用数据时标记 failed，旧 Active Generation 保持不变。"""
    from app.services.knowledge.ingestion import (
        create_rebuild_generation, maybe_activate_generation,
    )

    client = rag_env
    user_id = await _create_user("kb-genfail")
    headers = _auth(user_id)
    kb_id = await _create_base(client, headers, "代际失败")
    await _upload(client, headers, kb_id, "manual.md", MANUAL_MD.encode("utf-8"))
    await _run_worker()
    base = await client.get(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
    old_generation = base.json()["active_index_generation_id"]

    async with AsyncSessionLocal() as db:
        from app.models.knowledge import KnowledgeBase, KnowledgeIndexGeneration

        kb = await db.get(KnowledgeBase, kb_id)
        generation = await create_rebuild_generation(db, kb)
        new_id = generation.id
        activated = await maybe_activate_generation(db, kb_id, new_id)
        assert activated is False
        new_row = await db.get(KnowledgeIndexGeneration, new_id)
        assert new_row.status == "failed"
        refreshed = await db.get(KnowledgeBase, kb_id)
        assert refreshed.active_index_generation_id == old_generation

    search = await client.post("/api/v1/knowledge/search", json={
        "query": "设备多久检查一次", "knowledge_base_ids": [kb_id], "top_k": 3,
    }, headers=headers)
    assert search.json()["total"] >= 1
