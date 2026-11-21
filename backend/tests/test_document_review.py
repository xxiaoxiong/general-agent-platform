"""验证对话路由、原始附件写保护和文档差异的选择应用。"""

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.agent_runtime.backends import build_backend
from app.services.agent_runtime.task_router import choose_word_attachment, recent_word_attachments, route_task
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

    patch = DocumentPatch.model_validate({"operations": [{
        "section_id": "first", "operation": "replace_block", "block_id": "a",
        "block": {"type": "table", "rows": [["字段", "说明"]]},
    }]})
    with pytest.raises(DocumentPatchError, match="保持原有"):
        apply_patch(original, patch)


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
    assert graphs._allows_section_rewrite("重写第6章的6.1 用户管理", manual.sections)
    assert not graphs._allows_section_rewrite("调整6.1 的字段表格", manual.sections)


@pytest.mark.asyncio
async def test_local_request_replans_model_whole_section_operation(monkeypatch):
    original = _draft(("b1", "初始密码由管理员设定。"), ("b2", "日志审计需查看原系统。"))
    steps, requests = [], []

    async def step(name):
        steps.append(name)

    async def model_json(_context, _config, operation, _instruction, data, response_type):
        assert operation == "edit_draft" and data["allow_section_rewrite"] is False
        requests.append(data.copy())
        paragraph = {"type": "paragraph", "text": "初始密码由用户自行设定。"}
        proposed = {"operation": "replace_section", "blocks": [paragraph]} if len(requests) == 1 else {
            "operation": "replace_block", "block_id": "b1", "block": paragraph,
        }
        return response_type.model_validate({"operations": [{"section_id": "first", **proposed}]})

    monkeypatch.setattr(graphs, "model_json", model_json)
    workflow = graphs.DocumentWorkflow(SimpleNamespace(run=SimpleNamespace(run_id="test-run"), step=step))
    result = await workflow.edit_draft({
        "previous": original.model_dump(), "sources": [], "scope": ["first"],
        "request": "把第一章的初始密码说明重新整理一下，其他段落保持不变",
    }, {})
    candidate = DocumentDraft.model_validate(result["draft"])
    assert steps == ["repair_edit_patch"] and "局部编辑" in requests[1]["patch_validation_error"]
    assert result["rewrite_section_ids"] == []
    assert candidate.sections[0].blocks[1] == original.sections[0].blocks[1]
    assert [block.text for block in candidate.sections[0].blocks] == ["初始密码由用户自行设定。", "日志审计需查看原系统。"]
