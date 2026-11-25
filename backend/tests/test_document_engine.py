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
    DocumentDraft, DocumentPatch, DraftSection, InputField, ListBlock, ParagraphBlock, PatchOperation,
    ProfileDefinition, ProfileFormatting, ProfileSection, SourceReference, TableBlock,
)
from app.services.documents.restyle import apply_style_plan, style_plan_from_request
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


def test_patch_operation_tolerates_common_model_shapes():
    """模型常把 replace_section 写成 blocks+block_id、块级操作放进 blocks：按等价语义纠正（143 实录）。"""
    section = PatchOperation.model_validate({
        "operation": "replace_section", "section_id": "first", "block_id": "b1",
        "blocks": [{"type": "paragraph", "text": "整节新正文"}],
    })
    assert section.block is None and section.block_id is None and section.blocks
    block = PatchOperation.model_validate({
        "operation": "replace_block", "section_id": "first", "block_id": "b1",
        "blocks": [{"type": "paragraph", "text": "块级替换"}],
    })
    assert block.block is not None and block.blocks is None and block.block_id == "b1"


def test_content_blocks_tolerate_common_model_shapes():
    """模型常输出类名式 type、对象单元格/列表项、{cells: [...]} 行：按等价语义归一。

    write_section 曾因 `type: "ParagraphBlock"` / `TableBlock` 反复校验失败并耗光返工轮次。
    """
    from app.services.documents.schemas import DraftSection

    section = DraftSection.model_validate({
        "id": "s1", "title": "测试章节", "order": 1,
        "blocks": [
            {"type": "ParagraphBlock", "text": "段落正文"},
            {"type": "ListBlock", "items": [{"text": "第一项"}, "第二项", {"content": "第三项"}]},
            {"type": "TableBlock", "rows": [
                {"cells": [{"text": "列A"}, "列B"]},
                ["值1", {"value": "值2"}],
            ]},
        ],
    })
    assert [block.type for block in section.blocks] == ["paragraph", "list", "table"]
    assert list(section.blocks[1].items) == ["第一项", "第二项", "第三项"]
    assert section.blocks[2].rows == [["列A", "列B"], ["值1", "值2"]]


def test_content_blocks_drop_empty_rows_and_oversized_anchors():
    """空表格行/空列表项/空段落与超长块锚点不应让整轮生成失败（模型常见输出）。"""
    from app.services.documents.schemas import DraftSection

    section = DraftSection.model_validate({
        "id": "s2", "title": "风险", "order": 1,
        "blocks": [
            {"type": "paragraph", "text": "   "},
            {"type": "table", "id": "table_" + "x" * 60, "rows": [[], [""], ["风险", "应对"]]},
            {"type": "list", "items": ["", "  ", "有效条目"]},
        ],
    })
    assert [block.type for block in section.blocks] == ["table", "list"]
    assert section.blocks[0].rows == [["风险", "应对"]]
    assert section.blocks[0].id != "table_" + "x" * 60
    assert list(section.blocks[1].items) == ["有效条目"]


def test_profile_drops_section_ids_mistaken_for_input_fields():
    """章节 id 被误填为 required_fields 时只作为可选推导字段，不得变成索要资料。"""
    from app.services.documents.schemas import ProfileDefinition

    profile = ProfileDefinition.model_validate({
        "document_type": "report", "name": "技术调研报告规范",
        "sections": [
            {"id": "S1_architecture", "title": "架构", "order": 1,
             "required_fields": ["S1_architecture", "report_date"]},
            {"id": "S2_frameworks", "title": "框架", "order": 2, "required_fields": ["S2_frameworks"]},
        ],
        "required_inputs": [{"id": "report_date", "label": "报告日期", "type": "date", "required": False}],
    })
    fields = {field.id: field for field in profile.input_fields()}
    assert set(fields) == {"report_date", "S1_architecture", "S2_frameworks"}
    assert fields["S1_architecture"].required is False
    assert fields["S2_frameworks"].required is False
    # 已声明字段被必填章节引用时升级为必填（既有语义），章节 id 不会被升级
    assert fields["report_date"].required is True


def test_extracted_profile_hoists_nested_confidence_and_orders_sections():
    """模型常把 confidence 写进 definition、章节序号重复：归一后不应整轮失败。"""
    from app.services.documents.graphs import ExtractedProfile

    profile = ExtractedProfile.model_validate({
        "definition": {
            "document_type": "report", "name": "技术调研报告规范", "confidence": 0.82,
            "sections": [
                {"id": "a", "title": "甲", "order": 1},
                {"id": "b", "title": "乙", "order": 1},
                {"id": "c", "title": "丙", "order": 3},
            ],
        },
    })
    assert profile.confidence == 0.82
    assert [section.order for section in profile.definition.sections] == [1, 2, 3]
    assert "confidence" not in profile.definition.model_dump()


def test_content_blocks_drop_foreign_fields_and_pad_table_rows():
    """列表/表格块带段落专属字段、表格行宽不一致：按等价语义归一而非失败。"""
    from app.services.documents.schemas import DraftSection

    section = DraftSection.model_validate({
        "id": "s3", "title": "选型", "order": 1,
        "blocks": [
            {"type": "list", "items": ["甲", "乙"], "style": "bullet"},
            {"type": "table", "title": "决策矩阵", "rows": [["维度", "权重", "说明"], ["成本"], ["生态", "高"]]},
            {"type": "paragraph", "text": "正文", "style": "unknown"},
        ],
    })
    assert [block.type for block in section.blocks] == ["list", "table", "paragraph"]
    assert section.blocks[0].ordered is False
    assert section.blocks[1].rows == [["维度", "权重", "说明"], ["成本", "", ""], ["生态", "高", ""]]
    assert section.blocks[2].style == "normal"


def test_content_blocks_infer_unknown_types_and_unwrap_sections():
    """模型自造 type=section/text 等：按承载内容推断块类型或展开内部块。"""
    from app.services.documents.schemas import DraftSection

    section = DraftSection.model_validate({
        "id": "s4", "title": "章节包装", "order": 1,
        "blocks": [
            {"type": "section", "title": "小节", "blocks": [
                {"type": "paragraph", "text": "内部段落"},
                {"type": "text", "text": "自造类型段落"},
            ]},
            {"type": "unknown", "items": ["甲", "乙"]},
        ],
    })
    assert [block.type for block in section.blocks] == ["paragraph", "paragraph", "list"]
    assert section.blocks[0].text == "内部段落"
    assert section.blocks[1].text == "自造类型段落"
    assert list(section.blocks[2].items) == ["甲", "乙"]


@pytest.mark.asyncio
async def test_rework_rewrites_only_failed_sections():
    """单节校验失败只重写该节：曾因一节截断把整篇 16 节全部重跑，生成耗时翻倍。"""
    from types import SimpleNamespace

    from app.services.documents import graphs
    from app.services.documents.schemas import ProfileDefinition

    emitted = []

    async def emit(event, data):
        emitted.append((event, data))

    context = SimpleNamespace(
        run=SimpleNamespace(run_id="run-rework", user_id=1, agent_id=1, session_id=1),
        emit=emit,
    )
    workflow = graphs.DocumentWorkflow(context)
    profile = ProfileDefinition.model_validate({
        "document_type": "report", "name": "重写范围规范",
        "sections": [
            {"id": "s1", "title": "甲", "order": 1},
            {"id": "s2", "title": "乙", "order": 2},
            {"id": "s3", "title": "丙", "order": 3},
        ],
    })
    sections = [
        {"id": "s1", "title": "甲", "order": 1, "level": 1, "blocks": []},
        {"id": "s2", "title": "乙", "order": 2, "level": 1, "blocks": []},
        {"id": "s3", "title": "丙", "order": 3, "level": 1, "blocks": []},
    ]
    state = {
        "execution_context": {"task_type": "document_generate"},
        "profile": profile.model_dump(mode="json"),
        "sections": sections,
        "rework": 0,
        "validation": {"issues": [
            {"code": "DOC_TRUNCATED_OUTPUT", "message": "截断", "severity": "error", "section_id": "s2"},
            {"code": "source_requirement", "message": "提示", "severity": "warning", "section_id": "s1"},
        ]},
    }
    result = await workflow.rework(state, {})
    assert [item["id"] for item in result["sections"]] == ["s1", "s3"]
    assert result["cursor"] == 2 and result["rework"] == 1
    assert emitted and emitted[0][0] == "document.rework.scoped"
    assert emitted[0][1]["section_ids"] == ["s2"]

    # 编辑任务的返工回到补丁重写，不重置章节
    assert await workflow.rework({**state, "execution_context": {"task_type": "document_edit"}}, {}) == {"rework": 1}


def test_profile_normalizes_chinese_identifiers_and_field_references():
    """模型用中文写标识时归一到合法锚点，章节引用同步改名（曾因 string_pattern 整轮失败）。"""
    from app.services.documents.schemas import ProfileDefinition

    profile = ProfileDefinition.model_validate({
        "document_type": "技术调研报告",
        "name": "技术调研报告规范",
        "sections": [
            {"id": "技术架构", "title": "技术架构", "order": 1, "required_fields": ["报告日期", "S1"]},
            {"id": "技术架构", "title": "重复标识", "order": 2},
        ],
        "required_inputs": [{"id": "报告日期", "label": "报告日期", "type": "date"}],
    })
    assert profile.document_type == "document"
    assert [section.id for section in profile.sections] == ["s1", "s2"]
    assert profile.sections[0].required_fields == ["field1", "S1"]
    assert [field.id for field in profile.required_inputs] == ["field1"]


def test_generation_facts_normalizes_chinese_extra_field_ids():
    from app.services.documents.graphs import GenerationFacts

    facts = GenerationFacts.model_validate({
        "title": "选型指南", "field_values": {},
        "additional_required_inputs": [{"id": "市场规模", "label": "市场规模", "type": "text"}],
    })
    assert facts.additional_required_inputs[0].id == "extra1"


def test_generation_facts_normalizes_structured_field_values():
    """字段值写成对象/布尔/混合列表时收敛为可渲染文本，避免 identify_missing 整轮失败。"""
    from app.services.documents.graphs import GenerationFacts

    facts = GenerationFacts.model_validate({
        "title": "选型指南",
        "field_values": {
            "summary": {"a": "甲", "b": 2},
            "approved": True,
            "items": ["x", 1, {"y": "z"}],
        },
        "additional_required_inputs": [],
    })
    assert facts.field_values["summary"] == "甲；2"
    assert facts.field_values["approved"] == "是"
    assert facts.field_values["items"] == ["x", "1"]


def test_draft_excerpt_marks_truncation_at_section_boundary():
    """审阅摘录按章节截取并标注未摘录不等于缺失（曾因硬切半句被判 DOC_TRUNCATED）。"""
    from app.services.documents.schemas import DraftSection
    from app.services.documents.workflow_support import draft_excerpt

    sections = [
        DraftSection.model_validate({
            "id": f"s{index}", "title": f"第 {index} 章", "order": index,
            "blocks": [{"type": "paragraph", "text": "完整段落内容。" * 30}],
        })
        for index in range(1, 6)
    ]
    excerpt = draft_excerpt(sections, limit=400)
    assert "摘录到此结束" in excerpt
    assert "全文共 5 节" in excerpt and "不代表正文缺失" in excerpt
    assert not excerpt.rstrip().endswith("完整段落内容")


def test_edit_guard_detects_major_content_loss():
    """术语统一等局部改动若把章节正文删掉大半，必须被拦截（实测 2415→1242 字）。"""
    from app.services.documents.graphs import _content_loss_sections
    from app.services.documents.schemas import DocumentDraft

    before = DocumentDraft.model_validate({
        "title": "报告",
        "sections": [{
            "id": "chapter2", "title": "框架对比", "order": 1,
            "blocks": [{"type": "paragraph", "text": "原有内容。" * 100}],
        }],
    })
    after = DocumentDraft.model_validate({
        "title": "报告",
        "sections": [{
            "id": "chapter2", "title": "框架对比", "order": 1,
            "blocks": [{"type": "paragraph", "text": "只剩一句。"}],
        }],
    })
    assert _content_loss_sections(before, after, {"chapter2"}, "统一全文术语并修正矛盾")
    assert _content_loss_sections(before, after, {"chapter2"}, "重写第 2 章为对比表") == []
    assert _content_loss_sections(before, after, {"other"}, "统一全文术语") == []


def test_undeclared_section_fields_do_not_block_validation():
    """章节误填的字段名（如章节标题）不得在最终校验里变成必填项（实测 3 个字段卡死生成）。"""
    from app.services.documents.schemas import DocumentDraft, ProfileDefinition
    from app.services.documents.validator import validate_draft

    profile = ProfileDefinition.model_validate({
        "document_type": "report", "name": "规范",
        "sections": [
            {"id": "s1", "title": "甲", "order": 1, "required_fields": ["Agent_Swarm"]},
        ],
        "required_inputs": [],
    })
    draft = DocumentDraft.model_validate({
        "title": "报告",
        "sections": [{
            "id": "s1", "title": "甲", "order": 1, "level": 1,
            "blocks": [{"type": "paragraph", "text": "正文内容"}],
        }],
    })
    report = validate_draft(draft, profile)
    assert report.passed, [issue.model_dump() for issue in report.issues]


def test_style_plan_inherits_across_content_versions():
    """内容编辑会整文件重渲染，必须继承历史样式方案（曾导致 v5/v6 排版丢失）。"""
    from app.services.documents.graphs import _style_plan_of

    assert _style_plan_of({"style_restyle": {"plan": {"body_font": "宋体"}}}) == {"body_font": "宋体"}
    assert _style_plan_of({"inherited_style": {"plan": {"body_font": "仿宋"}}}) == {"body_font": "仿宋"}
    assert _style_plan_of({"base_version": 1}) is None
    assert _style_plan_of(None) is None


def test_style_restyle_validation_payload_matches_schema():
    """样式重排写入的 validation 必须符合 ValidationReport（extra=forbid，曾因多带 version 秒失败）。"""
    from app.services.documents.schemas import ValidationReport

    report = ValidationReport.model_validate({"passed": True, "issues": [], "checks": ["style_restyle"]})
    assert report.passed and report.checks == ["style_restyle"]


def test_conversation_draft_strips_internal_markdown_links():
    """subagent: 等内部链接不能以 Markdown 语法残留在 Word 正文（143 实录）。"""
    parsed = parse_conversation_draft("""# 调研报告

## 来源
本报告基于 [researcher 子代理检索](subagent:researcher) 与 [官网](https://example.com/a)。
""")
    text = "\n".join(
        block.text for section in parsed.sections for block in section.blocks if hasattr(block, "text")
    )
    assert "researcher 子代理检索" in text
    assert "subagent:" not in text and "](https://" not in text
    assert "https://example.com/a" in text


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


@pytest.mark.asyncio
async def test_truncated_but_valid_json_retries_with_compact_prompt():
    """命中输出上限时 JSON 可能仍可解析，但正文已截断：必须压缩重试一次。"""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from app.services.documents.graphs import SectionContent
    from app.services.documents.workflow_support import model_json

    prompts = []
    replies = iter([
        AIMessage(content='{"blocks":[{"type":"paragraph","text":"其核心组件包"}]}',
                  response_metadata={"finish_reason": "length"}),
        AIMessage(content='{"blocks":[{"type":"paragraph","text":"完整内容。"}]}'),
    ])

    async def invoke(messages, config):
        prompts.append(messages[0].content)
        return next(replies)

    async def step(_operation):
        return None

    context = SimpleNamespace(model=SimpleNamespace(ainvoke=invoke), step=step)
    section = await model_json(context, {}, "write_section", "编写章节", {}, SectionContent,
                               compact_retry="压缩返回篇幅")
    assert [block.text for block in section.blocks] == ["完整内容。"]
    assert len(prompts) == 2 and "压缩返回篇幅" in prompts[1]


@pytest.mark.asyncio
async def test_model_json_reports_progress_during_slow_calls(monkeypatch):
    """长模型调用期间必须周期性上报进度，避免前端长时间没有任何事件。"""
    import asyncio
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from app.services.documents import workflow_support
    from app.services.documents.graphs import SectionContent
    from app.services.documents.workflow_support import model_json

    events = []

    async def emit(event, data):
        events.append((event, data))

    async def invoke(_messages, config):
        await asyncio.sleep(0.05)
        return AIMessage(content='{"blocks":[{"type":"paragraph","text":"完整内容。"}]}')

    async def step(_operation):
        return None

    monkeypatch.setattr(workflow_support, "_PROGRESS_INTERVAL", 0.01)
    context = SimpleNamespace(model=SimpleNamespace(ainvoke=invoke), step=step, emit=emit)
    section = await model_json(context, {}, "write_section", "编写章节", {}, SectionContent,
                               progress_label="正在撰写《架构》")
    assert [block.text for block in section.blocks] == ["完整内容。"]
    assert events and {event for event, _ in events} == {"document.model.progress"}
    assert events[0][1]["label"] == "正在撰写《架构》"
    assert events[0][1]["elapsed_s"] >= 0


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
    with pytest.raises(DocumentPatchError, match="不一致"):
        apply_patch(result, patch, allowed_section_ids={"results"})


def test_patch_expected_before_tolerates_copy_variance(draft):
    """模型抄写原文常有空白、标点差异；块 ID 已定位时不应整单失败。"""
    patch = DocumentPatch.model_validate({
        "operations": [{
            "operation": "replace_block", "section_id": "results", "block_id": "b1",
            "expected_before": "完成数据迁移，\n\n开展业务培训。",
            "block": {"type": "list", "ordered": True, "items": ["完成迁移与质量抽检", "培训已覆盖业务团队"]},
        }],
        "summary": "更新交付成果",
    })
    result = apply_patch(draft, patch, allowed_section_ids={"results"})
    assert result.sections[1].blocks[0].items == ["完成迁移与质量抽检", "培训已覆盖业务团队"]


def test_restyle_docx_changes_formatting_only(tmp_path):
    """样式/排版优化只改格式：文字、章节与表格内容必须原样保留（143 实录教训）。"""
    from docx.shared import Pt

    source = tmp_path / "source.docx"
    word = Document()
    word.add_heading("Agent Swarm 深度调研报告", level=0)
    word.add_heading("1. 技术架构", level=1)
    word.add_paragraph("多智能体编排正在成为主流。" * 12)
    word.add_heading("1.1 核心组件", level=2)
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "框架"
    table.cell(0, 1).text = "特点"
    table.cell(1, 0).text = "LangGraph"
    table.cell(1, 1).text = "图编排"
    word.save(source)
    before = [paragraph.text for paragraph in Document(source).paragraphs]

    target = tmp_path / "restyled.docx"
    plan = style_plan_from_request("正文用仿宋，1.5倍行距，首行缩进2字符")
    summary = apply_style_plan(source, target, plan)
    assert summary["headings"] == 3 and summary["tables"] == 1
    # 交付说明必须列出实际改动（避免"1s 已完成"式空泛结论）
    assert "仿宋" in summary["description"] and "1.5 倍行距" in summary["description"]

    out = Document(target)
    assert [paragraph.text for paragraph in out.paragraphs] == before
    body = next(item for item in out.paragraphs if item.text.startswith("多智能体"))
    assert body.paragraph_format.line_spacing == 1.5
    assert body.runs[0].font.size == Pt(12)
    assert body.runs[0].font.name == "仿宋"
    heading = next(item for item in out.paragraphs if item.text.startswith("1. 技术架构"))
    assert heading.runs[0].font.bold is True
    assert heading.runs[0].font.size == Pt(16)
    shading = out.tables[0].rows[0].cells[0]._tc.get_or_add_tcPr().find(qn("w:shd"))
    assert shading is not None and shading.get(qn("w:fill")) == "D9D9D9"
