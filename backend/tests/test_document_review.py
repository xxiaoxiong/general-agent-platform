"""验证对话路由、原始附件写保护和文档差异的选择应用。"""

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.agent_runtime.backends import build_backend
from app.services.agent_runtime.task_router import (
    choose_word_attachment, continuation_attachments, recent_word_attachments, route_task,
)
from app.services.documents import graphs
from app.services.documents.editor import DocumentPatchError, apply_patch
from app.services.documents.reviews import accepted_draft, decision_schema, structured_changes
from app.services.documents.schemas import DocumentDraft, DocumentPatch


def test_screenshot_request_edits_prior_word_and_plain_delivery_uses_document_graph():
    request = "帮我将文档中的机器学习算法改成深度学习算法，并优化文档中的描述，给到我文档"
    history = [{"role": "user", "content":
                "先阅读这份文件\n\n[附件]（旧版）\n- /workspace/uploads/测试说明.docx（文本副本：/workspace/uploads/测试说明.docx.extracted.txt）"}]
    refs = recent_word_attachments(history)
    assert choose_word_attachment(request, refs)["path"] == "uploads/测试说明.docx"
    assert route_task(request, has_word_attachment=bool(refs))["task_type"] == "document_edit"
    assert route_task("把上面的内容整理成可下载的Word")["task_type"] == "document_generate"
    assert route_task("如何修改 Word 文档", has_word_attachment=True)["task_type"] == "general_chat"


def test_research_delivery_to_word_routes_to_document_generate():
    """「将调研结果放到 word 文档中给到我」类语序必须识别为文档交付（143 实录教训）。"""
    assert route_task("将调研结果放到word文档中给到我")["task_type"] == "document_generate"
    assert route_task("把调研报告存到word给我")["task_type"] == "document_generate"


def test_followup_document_task_inherits_session_word_attachment():
    """用户上传资料后追问"基于这份报告…"时，后续文档任务要自动沿用该附件。"""
    history = [{"role": "user", "content":
                "先读一下这份资料\n\n[附件]（旧版）\n"
                "- /workspace/uploads/行业报告.docx（文本副本：/workspace/uploads/行业报告.docx.extracted.txt）"}]
    inherited = continuation_attachments("基于这份报告提炼一套写作规范", history, "profile_extract")
    assert [item["path"] for item in inherited] == ["uploads/行业报告.docx"]
    # 本轮已重新上传时不重复继承
    assert continuation_attachments("基于这份报告提炼规范", history, "profile_extract",
                                    [{"path": "uploads/新附件.docx"}]) == []
    # 非文档图任务、未指代前文时不继承
    assert continuation_attachments("基于这份报告提炼规范", history, "general_chat") == []
    assert continuation_attachments("你好", history, "profile_extract") == []


def test_spaced_chapter_reference_and_fulltext_check_route_to_document_edit():
    """「第 2 章」（带空格）与「通读全文…修正」必须走真实文档编辑（实测曾漏判为闲聊）。"""
    assert route_task("第 2 章写得太浅了，重写成有深度的框架对比",
                      has_document_context=True)["task_type"] == "document_edit"
    assert route_task("第 3 章的选型建议改成可执行的决策流程",
                      has_document_context=True)["task_type"] == "document_edit"
    assert route_task("最后通读全文做一次交付检查，发现问题直接修正",
                      has_document_context=True)["task_type"] == "document_edit"
    # 没有文档上下文时不误判
    assert route_task("第 2 章写得太浅了，重写一下")["task_type"] == "general_chat"


def test_document_version_claim_detection():
    """无文档版本事件却声称"第 N 版/DOCX 已通过校验"属于虚假交付，需要核对提示。"""
    from app.services.agent_runtime.service.delivery import claims_document_version

    assert claims_document_version("已生成《选型指南》第 2 版，DOCX 文件已通过校验，可预览或下载。")
    assert claims_document_version("第3版已保存")
    assert not claims_document_version("已生成一份市场分析草稿")
    assert not claims_document_version("")


def test_style_optimization_routes_to_edit_existing_document():
    """143 实录：要求"优化文档样式、排版"曾被误判为新主题并生成一份讲排版的新文档。

    有会话文档/Word 附件时必须路由到 document_edit（只重排样式），不得重新生成主题文档。
    """
    message = "深度优化文档样式、排版等内容，重新给到我一份"
    route = route_task(message, has_document_context=True)
    assert route["task_type"] == "document_edit"
    assert route["style_edit"] is True
    assert route_task(message, has_word_attachment=True)["style_edit"] is True
    # 没有任何文档上下文时不能凭空生成"讲排版"的文档
    assert route_task(message)["task_type"] == "general_chat"
    # 明确要改内容时不能只做样式重排
    assert route_task("优化文档样式并补充最新数据", has_document_context=True)["style_edit"] is False
    # 143 实录：点名具体章节且指出"不符合要求/需符合写作规范"，必须走真实内容修改，
    # 不能 1 秒内只重排格式就宣告完成
    deep = ("优化Agent Swarm 深度调研报告的样式和排版，4.5 多层安全架构的表、"
            "6.2 技术演进路线图、7. 选型建议决策树不符合要求，需要符合国企的写作规范")
    deep_route = route_task(deep, has_document_context=True)
    assert deep_route["task_type"] == "document_edit"
    assert deep_route["style_edit"] is False


def test_casual_edit_request_routes_to_document_edit():
    """「还需要改下」这类口语化要求，在已指向当前文档时必须进入真实编辑（143 实录）。"""
    route = route_task("1.2 核心架构组件、5.2 生产落地“死亡之谷”还需要改下", document_id=3)
    assert route["task_type"] == "document_edit"
    assert route["style_edit"] is False


def test_host_style_path_write_is_rejected_with_workspace_guidance(tmp_path, monkeypatch):
    """宿主机风格路径（/Users/... 等）写入必须拒绝并引导到工作区（143 实录教训）。"""
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    backend = build_backend(1, 1, 1)
    for bad in ("/Users/user/Documents/调研报告.md", "C:\\Users\\user\\report.md"):
        result = backend.write(bad, "内容")
        assert result.error, bad
        assert "/workspace/" in result.error
        edit = backend.edit(bad, "a", "b")
        assert edit.error and "/workspace/" in edit.error
    # 工作区正常路径不受影响
    assert not backend.write("/workspace/报告.md", "内容").error


def test_agent_can_read_but_cannot_overwrite_original_or_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    folder = tmp_path / "1" / "1" / "1" / "uploads"
    folder.mkdir(parents=True)
    sidecar = folder / "原 Word.docx.extracted.txt"
    sidecar.write_text("旧工艺", encoding="utf-8")
    backend = build_backend(1, 1, 1)
    path = "/workspace/uploads/原 Word.docx.extracted.txt"
    assert backend.edit(path, "旧工艺", "新工艺").error
    assert backend.delete(path).error
    assert backend.write("/workspace/假 Word.docx", "只有文本").error
    assert not backend.write("/workspace/notes.md", "可写工作区文件").error
    assert sidecar.read_text(encoding="utf-8") == "旧工艺"


def _draft(*rows):
    return DocumentDraft(title="审阅测试", sections=[{
        "id": "first", "title": "第一章", "order": 1, "blocks": [
            {"id": anchor, "type": "paragraph", "text": text} for anchor, text in rows
        ],
    }])


def test_independent_changes_in_same_section_allow_partial_approval():
    original = _draft(("a", "原段落"), ("b", "保留段落"))
    candidate = _draft(("a", "更新段落"), ("c", "新增段落"))
    changes = structured_changes(original, candidate)
    assert len(changes) == 3
    selected = {item["id"] for item in changes if item.get("block_id") in {"a", "c"}}
    result = accepted_draft(original, candidate, changes, selected)
    assert [(block.id, block.text) for block in result.sections[0].blocks] == [
        ("a", "更新段落"), ("c", "新增段落"), ("b", "保留段落"),
    ]
    assert decision_schema(changes)["properties"]["accepted_change_ids"]["items"]["enum"] == [
        item["id"] for item in changes
    ]


def test_section_rewrite_never_matches_unrelated_blocks_by_ordinal():
    original = _draft(("b1", "进入用户管理并点击新增用户。"), ("b2", "6.4 日志审计"), ("b3", "以实际界面为准。"))
    patch = DocumentPatch.model_validate({"operations": [{
        "section_id": "first", "operation": "replace_section", "blocks": [
            {"id": "b1", "type": "paragraph", "text": "6.1 用户管理"},
            {"type": "table", "rows": [["字段", "说明"], ["用户名", "登录账号"]]},
            {"type": "paragraph", "text": "以实际界面为准。"},
        ],
    }]})
    candidate = apply_patch(original, patch, anchor_namespace="run-a")
    changed_anchors = [block.id for block in candidate.sections[0].blocks]
    assert changed_anchors[-1] == "b3" and {"b1", "b2"}.isdisjoint(changed_anchors)
    assert apply_patch(original, patch, anchor_namespace="run-a") == candidate
    assert apply_patch(original, patch, anchor_namespace="run-b").sections[0].blocks[0].id != changed_anchors[0]
    changes = structured_changes(original, candidate, {"first"})
    assert len(changes) == 1 and changes[0]["scope"] == "section" and "block_id" not in changes[0]
    assert accepted_draft(original, candidate, changes, {changes[0]["id"]}) == candidate
    assert accepted_draft(original, candidate, changes, set()) == original


def test_removed_anchor_is_not_reused_and_insertions_keep_plan_order():
    original = _draft(("b1", "准备材料"), ("b2", "旧流程"), ("b3", "开始验收"))
    patch = DocumentPatch.model_validate({"operations": [
        {"section_id": "first", "operation": "remove_block", "block_id": "b2", "expected_before": "旧流程"},
        {"section_id": "first", "operation": "insert_block", "block_id": "b1",
         "block": {"id": "b2", "type": "paragraph", "text": "记录结果"}},
        {"section_id": "first", "operation": "insert_block", "block_id": "b1",
         "block": {"type": "paragraph", "text": "核对状态"}},
    ]})
    candidate = apply_patch(original, patch)
    assert [block.text for block in candidate.sections[0].blocks] == ["准备材料", "记录结果", "核对状态", "开始验收"]
    changes = structured_changes(original, candidate)
    assert [change["action"] for change in changes] == ["删除内容", "添加内容", "添加内容"]
    assert changes[0]["block_id"] == "b2" and changes[0]["after"] is None
    assert all(change["before"] is None for change in changes[1:])
    result = accepted_draft(original, candidate, changes, {item["id"] for item in changes[1:]})
    assert [block.text for block in result.sections[0].blocks] == ["准备材料", "记录结果", "核对状态", "旧流程", "开始验收"]


def test_moving_existing_content_cannot_masquerade_as_a_block_replacement():
    original = _draft(("a", "6.4 日志审计"), ("b", "进入用户管理页面并点击新增用户。"))
    patch = DocumentPatch.model_validate({"operations": [{
        "section_id": "first", "operation": "replace_block", "block_id": "a",
        "block": {"type": "paragraph", "text": "进入用户管理页面并点击新增用户。"},
    }]})
    with pytest.raises(DocumentPatchError, match="其他原文块"):
        apply_patch(original, patch)

    # 块类型转换是合法编辑方式：结构如何表达由模型按用户要求决定
    patch = DocumentPatch.model_validate({"operations": [{
        "section_id": "first", "operation": "replace_block", "block_id": "a",
        "block": {"type": "table", "rows": [["字段", "说明"]]},
    }]})
    converted = apply_patch(original, patch)
    assert converted.sections[0].blocks[0].type == "table"
    assert converted.sections[0].blocks[0].id == "a"


def test_chapter_scope_uses_printed_number_and_prefers_a_named_subsection():
    manual = DocumentDraft(title="产品使用手册", sections=[
        {"id": "intro", "title": "前言", "order": 1},
        {"id": "system", "title": "第6章 系统管理", "order": 2},
        {"id": "users", "title": "6.1 用户管理", "order": 3, "level": 2},
        {"id": "audit", "title": "6.10 日志审计", "order": 4, "level": 2},
    ])
    assert graphs._scope("重写第六章", manual.sections) == ["system"]
    assert graphs._scope("修改第6章 系统管理里 6.1 的初始密码", manual.sections) == ["users"]
    assert graphs._scope("重写整个第6章 系统管理，覆盖6.1", manual.sections) == ["system", "users"]
    # 小节号在文档中重复（两个 6.2）时不得按号码误选无关章节，必须靠标题点名（143 实录）
    duplicated = DocumentDraft(title="深度调研报告", sections=[
        {"id": "trend", "title": "6. 未来发展趋势", "order": 1},
        {"id": "roadmap", "title": "6.2 技术演进路线图", "order": 2, "level": 2},
        {"id": "cases", "title": "6. 真实企业落地案例", "order": 3},
        {"id": "caseb", "title": "6.2 案例 B：合规审查", "order": 4, "level": 2},
    ])
    assert graphs._scope("6.2 技术演进路线图不符合要求", duplicated.sections) == ["roadmap"]


def test_review_issue_normalization_tolerates_model_output():
    """评审模型常返回超长代码/非锚点章节 ID，必须规范化而不是整单失败（143 实录）。"""
    from app.services.documents.graphs import ReviewIssue, _normalized_issue

    long_code = _normalized_issue(ReviewIssue(code="A" * 60, message="超长代码", section_id="6.2"))
    assert len(long_code.code) == 32 and long_code.section_id is None
    chinese = _normalized_issue(ReviewIssue(code="表格格式不合规", message="中文代码"))
    assert chinese.code == "REVIEW_ISSUE"
    valid = _normalized_issue(ReviewIssue(code="TABLE_NONCOMPLIANT", message="缺少表头",
                                          section_id="section_20"))
    assert valid.code == "TABLE_NONCOMPLIANT" and valid.section_id == "section_20"


@pytest.mark.asyncio
async def test_edit_review_downgrades_out_of_scope_issues(monkeypatch):
    """编辑语义复查只作提示：不因范围外问题触发返工，由用户审阅最终稿（143 实录）。"""
    original = _draft(("b1", "4.5 多层安全架构说明。"))

    async def model_json(_context, _config, operation, _instruction, _data, response_type, **_kwargs):
        assert operation == "review_draft"
        return response_type.model_validate({"issues": [
            {"code": "OUT_SCOPE", "message": "全局术语不统一", "section_id": None},
            {"code": "IN_SCOPE", "message": "本节表格缺少表头", "section_id": "first"},
        ]})

    async def emit(*_args, **_kwargs):
        return None

    async def step(_name):
        return None

    monkeypatch.setattr(graphs, "model_json", model_json)
    workflow = graphs.DocumentWorkflow(SimpleNamespace(
        run=SimpleNamespace(run_id="test-run"), step=step, emit=emit,
    ))
    result = await workflow.review({
        "previous": original.model_dump(), "draft": original.model_dump(), "scope": ["first"],
        "request": "修改第一章", "sources": [],
        "execution_context": {"task_type": "document_edit"},
    }, {})
    severities = {issue["code"]: issue["severity"] for issue in result["validation"]["issues"]}
    assert severities == {"OUT_SCOPE": "warning", "IN_SCOPE": "warning"}
    assert result["validation"]["passed"] is True
    assert any("质量建议" in item for item in result["warnings"])


@pytest.mark.asyncio
async def test_edit_draft_repairs_invalid_operation_and_allows_section_rewrite(monkeypatch):
    """模型可按需整节重写，无需关键词门槛；锚点非法时仍需修复重试（143 实录教训）。"""
    original = _draft(("b1", "初始密码由管理员设定。"), ("b2", "日志审计需查看原系统。"))
    steps, requests = [], []

    async def step(name):
        steps.append(name)

    async def model_json(_context, _config, operation, _instruction, data, response_type, **_kwargs):
        assert operation == "edit_draft" and "allow_section_rewrite" not in data
        requests.append(data.copy())
        if len(requests) == 1:
            # 不存在的块锚点：必须修复后重试
            proposed = {"operation": "replace_block", "block_id": "missing",
                        "block": {"type": "paragraph", "text": "初始密码由用户自行设定。"}}
        else:
            # 结构变化（段落改表格）允许用整节重写表达
            proposed = {"operation": "replace_section", "blocks": [
                {"type": "paragraph", "text": "初始密码由用户自行设定。"},
                {"type": "table", "rows": [["项目", "说明"], ["初始密码", "由用户自行设定"]]},
            ]}
        return response_type.model_validate({"operations": [{"section_id": "first", **proposed}]})

    monkeypatch.setattr(graphs, "model_json", model_json)
    workflow = graphs.DocumentWorkflow(SimpleNamespace(run=SimpleNamespace(run_id="test-run"), step=step))
    result = await workflow.edit_draft({
        "previous": original.model_dump(), "sources": [], "scope": ["first"],
        "request": "1.2 核心架构组件、5.2 生产落地还需要改下",
    }, {})
    candidate = DocumentDraft.model_validate(result["draft"])
    assert steps == ["repair_edit_patch"] and "不存在" in requests[1]["patch_validation_error"]
    assert result["rewrite_section_ids"] == ["first"]
    assert [block.type for block in candidate.sections[0].blocks] == ["paragraph", "table"]
