"""真实文件、结构规范与局部修改的边界回归。"""

from zipfile import ZIP_DEFLATED, ZIP_LZMA, ZipFile

from docx import Document
from docx.oxml.ns import qn
from openpyxl import Workbook
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pptx import Presentation
import pytest

from app.services.documents.editor import DocumentPatchError, apply_patch, diff_drafts
from app.services.documents.parser import DocumentParseError, parse_conversation_draft, parse_document, parse_docx, validate_reference_file
from app.services.documents.renderer import render_docx, safe_docx_filename
from app.services.documents.schemas import (
    DocumentDraft, DocumentPatch, DraftSection, InputField, ListBlock, ParagraphBlock, ProfileDefinition,
    ProfileFormatting, ProfileSection, SourceReference, TableBlock,
)
from app.services.documents.validator import valid_field, validate_docx, validate_draft


@pytest.fixture
def profile():
    return ProfileDefinition(
        name="项目总结规范", document_type="project_summary",
        required_inputs=[InputField(id="finish_date", label="项目完成日期", type="date")],
        sections=[
            ProfileSection(id="overview", title="项目概况", order=1, required_fields=["finish_date"]),
            ProfileSection(id="results", title="成果与交付", order=2),
            ProfileSection(id="followup", title="后续计划", order=3, required=False),
        ],
        formatting=ProfileFormatting(header="项目交付资料", footer="项目办公室"),
    )


@pytest.fixture
def draft():
    return DocumentDraft(
        title="项目阶段总结", subtitle="交付与验收", abstract="迁移已完成，系统运行稳定。",
        field_values={"finish_date": "2026-11-15"},
        sources=[SourceReference(id="report", label="项目验收记录", kind="attachment")],
        sections=[
            DraftSection(id="overview", title="项目概况", order=1, blocks=[
                ParagraphBlock(text="项目于 2026年11月15日完成。", source_ids=["report"]),
            ]),
            DraftSection(id="results", title="成果与交付", order=2, blocks=[
                ListBlock(items=["完成数据迁移", "开展业务培训"], ordered=True, source_ids=["report"]),
                ListBlock(items=["交付运行手册"], ordered=True),
                TableBlock(rows=[["交付项", "状态"], ["切换演练", "完成"]], source_ids=["report"]),
            ]),
            DraftSection(id="followup", title="后续计划", order=3, blocks=[
                ParagraphBlock(text="下周开展一次复盘。"),
            ]),
        ],
    )


def test_profile_validates_hierarchy_choices_and_inferred_fields():
    with pytest.raises(ValidationError, match="选项"):
        InputField(id="team", label="团队", type="single_choice")
    with pytest.raises(ValidationError, match="不能跳级"):
        ProfileDefinition(name="总结规范", document_type="summary", sections=[
            ProfileSection(id="level2", title="小节", order=1, level=2),
        ])
    with pytest.raises(ValidationError, match="顺序不能重复"):
        ProfileDefinition(name="总结规范", document_type="summary", sections=[
            ProfileSection(id="sections1", title="第一节", order=1),
            ProfileSection(id="sections2", title="第二节", order=1),
        ])
    with pytest.raises(ValidationError, match="列数"):
        TableBlock(rows=[["a", "b"], ["c"]])
    spec = ProfileDefinition(name="项目规范", document_type="summary", sections=[
        ProfileSection(id="project", title="项目", order=1, required_fields=["project_name"]),
    ])
    assert spec.input_fields()[0].id == "project_name"
    choices = InputField(id="owners", label="负责人", type="multiple_choice", options=["甲", "乙"])
    assert valid_field(choices, ["甲", "乙"])
    assert not valid_field(choices, [{"name": "甲"}])
    amount = InputField(id="total", label="金额", type="number")
    assert valid_field(amount, 3.5)
    assert not valid_field(amount, float("nan"))


def test_conversation_draft_keeps_phm_chapters_lists_and_tables_in_word(tmp_path):
    from docx import Document

    parsed = parse_conversation_draft("""以下是之前编写的稿件。
# PHM 软件用户手册

使用本手册了解系统的维护流程。
## 健康数据管理
1. 选择设备
2. 查看趋势

### 报警等级
|等级|处理方式|
|:---|---:|
|高|检查设备|
""")
    output = tmp_path / "phm.docx"
    render_docx(DocumentDraft(title=parsed.title, sections=parsed.sections), output)
    word = Document(output)
    assert [section.title for section in parsed.sections] == ["正文", "健康数据管理", "报警等级"]
    assert [section.level for section in parsed.sections] == [1, 1, 2]
    assert word.paragraphs[0].text == "PHM 软件用户手册"
    assert any(paragraph.text == "选择设备" for paragraph in word.paragraphs)
    assert word.tables[0].cell(1, 1).text == "检查设备"
    assert not any("以下是之前" in paragraph.text for paragraph in word.paragraphs)


@pytest.mark.asyncio
async def test_section_model_repairs_truncation_and_missing_block_fields():
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from app.services.documents.graphs import SectionContent
    from app.services.documents.workflow_support import model_json

    prompts, steps = [], []
    replies = iter([
        AIMessage(content='{"blocks":[', response_metadata={"finish_reason": "length"}),
        AIMessage(content='{"blocks":[{"type":"table"}]}'),
        AIMessage(content='<think>提炼要点</think>\n本段内容如下\n```json\n{"blocks":["完成",{"text":"复核"}]}\n```'),
    ])

    async def invoke(messages, config):
        prompts.append(messages[0].content)
        return next(replies)

    async def step(operation):
        steps.append(operation)

    context = SimpleNamespace(model=SimpleNamespace(ainvoke=invoke), step=step)
    section = await model_json(context, {}, "write_section", "编写章节", {}, SectionContent,
                               compact_retry="压缩返回篇幅", stage="报警")
    assert [block.text for block in section.blocks] == ["完成", "复核"]
    assert steps == ["write_section_retry", "write_section_retry"]
    assert "压缩返回篇幅" in prompts[1] and "blocks.0.table.rows missing" in prompts[2]


def test_guarded_parser_retains_source_docx_structure(tmp_path):
    path = tmp_path / "样例.docx"
    word = Document()
    word.core_properties.author = "业务团队"
    word.add_paragraph("组织改造项目", "Title")
    word.add_heading("项目背景", 1)
    word.add_paragraph("汇报范围与撰写说明。")
    word.add_heading("实施范围", 2)
    word.add_paragraph("业务范围", "List Bullet")
    word.add_paragraph("数据范围", "List Bullet")
    word.add_table(rows=1, cols=2).rows[0].cells[0].text = "接口"
    word.add_heading("例外", 3)
    word.add_paragraph("以已核验记录为准。")
    word.save(path)
    result = parse_docx(path)
    assert result.title == "组织改造项目"
    assert result.metadata["author"] == "业务团队"
    assert [(section.title, section.level) for section in result.sections] == [
        ("项目背景", 1), ("实施范围", 2), ("例外", 3),
    ]
    assert result.sections[1].blocks[0].items == ["业务范围", "数据范围"]
    assert result.sections[1].blocks[1].rows == [["接口", ""]]
    assert "## 实施范围" in result.as_text()


def test_guard_rejects_bad_office_mime_paths_macros_and_bombs(tmp_path):
    path = tmp_path / "样例.docx"
    Document().save(path)
    with pytest.raises(DocumentParseError, match="类型"):
        validate_reference_file(path, "application/pdf")
    with ZipFile(path, "w", compression=ZIP_LZMA) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", "<document/>")
    with pytest.raises(DocumentParseError, match="压缩方式"):
        validate_reference_file(path)
    for name, data, message in [
        ("../outside", b"x", "非法路径"),
        ("word/vbaProject.bin", b"x", "无宏"),
        ("word/expanded.xml", b"x" * (3 * 1024**2), "压缩比"),
        ("word/unsafe.xml", b"<!DOCTYPE ignored><document/>", "XML 文档类型"),
    ]:
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr(name, data)
        with pytest.raises(DocumentParseError, match=message):
            validate_reference_file(path)


def test_parser_handles_pdf_pptx_and_xlsx_without_fabricating_scanned_text(tmp_path):
    blank = tmp_path / "扫描件.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    writer.write(blank)
    result = parse_document(blank)
    assert not result.sections
    assert "扫描件" in result.warnings[0]
    source = tmp_path / "检查记录.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(300, 200)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 150 Td (Approved for delivery) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(source)
    assert parse_document(source).sections[0].blocks[0].text == "Approved for delivery"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "阶段汇报"
    slide.placeholders[1].text = "验收结论"
    slides = tmp_path / "阶段汇报.pptx"
    presentation.save(slides)
    result = parse_document(slides)
    assert result.sections[0].title == "阶段汇报"
    assert result.sections[0].blocks[0].text == "验收结论"
    workbook = Workbook()
    workbook.active.title = "验收明细"
    workbook.active.append(["项目", "数量", "复核"])
    workbook.active.append(["迁移", 20, "=1+1"])
    sheet = tmp_path / "验收明细.xlsx"
    workbook.save(sheet)
    result = parse_document(sheet)
    assert result.sections[0].title == "验收明细"
    assert result.sections[0].blocks[0].rows[1] == ["迁移", "20", ""]


def test_renderer_reopens_a4_and_independent_lists_tables(draft, profile, tmp_path):
    target = tmp_path / "v1.docx"
    render_docx(draft, target, profile)
    report = validate_docx(target, draft, profile)
    assert report.passed and not report.issues
    assert "docx_blocks" in report.checks
    word = Document(target)
    section = word.sections[0]
    assert section.page_width.cm == pytest.approx(21, abs=0.01)
    assert section.page_height.cm == pytest.approx(29.7, abs=0.01)
    assert section.header.paragraphs[0].text == "项目交付资料"
    assert section.footer.paragraphs[0]._p.find(qn("w:fldSimple")).get(qn("w:instr")) == "PAGE"
    numbers = [
        paragraph._p.pPr.numPr.numId.val for paragraph in word.paragraphs
        if paragraph.style.name == "List Number"
    ]
    assert numbers[0] == numbers[1] and numbers[1] != numbers[2]
    assert safe_docx_filename("CON.docx") == "_CON.docx"
    assert "/" not in safe_docx_filename("../项目.docx")
    initial = target.read_bytes()
    with pytest.raises(FileExistsError):
        render_docx(draft, target)
    assert initial == target.read_bytes()


def test_validator_rejects_missing_required_content_and_survives_external_corruption(draft, profile, tmp_path):
    modified = draft.model_copy(update={"field_values": {}, "sections": [draft.sections[0]]})
    report = validate_draft(modified, profile)
    assert not report.passed
    assert {"required_section", "required_input"}.issubset({issue.code for issue in report.issues})
    marked = draft.model_copy(update={"field_values": {}, "missing_fields": ["finish_date"]})
    target = render_docx(marked, tmp_path / "mark.docx", profile)
    report = validate_docx(target, marked, profile)
    assert report.passed
    assert [issue.code for issue in report.issues] == ["input_marked_missing"]
    assert parse_docx(target).missing_fields == ["finish_date"]
    word = Document(target)
    word.tables[0].cell(1, 1).text = "已篡改"
    word.save(target)
    report = validate_docx(target, marked, profile)
    assert not report.passed
    assert any(issue.code == "docx_content" and issue.section_id == "results" for issue in report.issues)
    target.write_bytes(b"corrupt")
    assert not validate_docx(target, marked, profile).passed


def test_patch_changes_only_confirmed_section_with_stable_anchors_and_diff(draft):
    patch = DocumentPatch.model_validate({
        "operations": [{
            "operation": "replace_block", "section_id": "results", "block_id": "b1",
            "expected_before": "完成数据迁移\n开展业务培训", "source_ids": ["report"],
            "block": {"type": "list", "ordered": True, "items": ["完成迁移与质量抽检", "培训已覆盖业务团队"]},
        }],
        "summary": "更新交付成果",
    })
    result = apply_patch(draft, patch, allowed_section_ids={"results"})
    assert result.sections[0] == draft.sections[0]
    assert result.sections[2] == draft.sections[2]
    assert result.sections[1].blocks[0].id == draft.sections[1].blocks[0].id
    assert draft.sections[1].blocks[0].items[0] == "完成数据迁移"
    difference = diff_drafts(draft, result)["sections"]
    assert [item["section_id"] for item in difference] == ["results"]
    assert difference[0]["blocks"][0]["after"]["source_ids"] == ["report"]
    with pytest.raises(DocumentPatchError, match="超出"):
        apply_patch(draft, patch, allowed_section_ids={"followup"})
    with pytest.raises(DocumentPatchError, match="已变化"):
        apply_patch(result, patch, allowed_section_ids={"results"})
