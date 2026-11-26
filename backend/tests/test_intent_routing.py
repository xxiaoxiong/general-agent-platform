"""意图识别：模型分诊 + 规则兜底 + 会话文档不被新任务绑架。"""
from __future__ import annotations

import json

from app.services.agent_runtime.document_turn import document_turn
from app.services.agent_runtime.intent import (
    apply_intent, build_intent_prompt, parse_intent,
)
from app.services.agent_runtime.task_router import (
    detect_deliverable_format, route_task, user_grants_autonomy,
)


def test_detect_deliverable_format():
    assert detect_deliverable_format("放到md文档中给到我下载") == "md"
    assert detect_deliverable_format("导出为 Markdown 文件") == "md"
    assert detect_deliverable_format("给我一份 word 文档") == "docx"
    assert detect_deliverable_format("生成 docx 报告") == "docx"
    assert detect_deliverable_format("整理成 csv") == "csv"
    assert detect_deliverable_format("导出一份 pdf") == "pdf"
    assert detect_deliverable_format("把 word 转成 md") == "md"
    assert detect_deliverable_format("帮我分析一下这个方案") is None


def test_markdown_delivery_does_not_enter_word_pipeline():
    """143/178 实录：用户要 md 却生成 DOCX —— 非 Word 格式交给通用能力交付。"""
    route = route_task("放到md文档中给到我下载", has_document_context=True)
    assert route["task_type"] == "general_chat"
    assert route["deliverable_format"] == "md"

    word = route_task("将调研的结果放到word文档中给到我下载", has_document_context=True)
    assert word["task_type"] == "document_generate"
    assert word["deliverable_format"] == "docx"

    # 会话里有文档 + 普通解释诉求：不能被当成文档任务
    assert route_task("帮我用三句话解释向量数据库", has_document_context=True)["task_type"] == "general_chat"


def test_text_attachment_edit_stays_general_chat():
    attachments = [{"path": "uploads/notes.md", "name": "notes.md"}]
    route = route_task("修改这个附件里的第三段", attachment_refs=attachments)
    assert route["task_type"] == "general_chat"


def test_parse_intent_accepts_valid_json_and_rejects_invalid():
    valid = parse_intent(json.dumps({
        "intent": "chat", "deliverable_format": "md", "target": "none", "reason": "普通问答",
    }))
    assert valid == {
        "intent": "chat", "deliverable_format": "md", "target": "none", "reason": "普通问答",
    }
    fenced = parse_intent('```json\n{"intent":"create_document","deliverable_format":"docx"}\n```')
    assert fenced and fenced["intent"] == "create_document"
    assert parse_intent("不是 JSON") is None
    assert parse_intent('{"intent":"unknown"}') is None
    # 非法格式回落 none，不影响 intent
    relaxed = parse_intent('{"intent":"chat","deliverable_format":"exe"}')
    assert relaxed and relaxed["deliverable_format"] == "none"


def test_build_intent_prompt_carries_conversation_context():
    prompt = build_intent_prompt(
        "放到md文档中给到我下载",
        history=[
            {"role": "user", "content": "生成一份调研报告"},
            {"role": "assistant", "content": "# 报告\n\n正文", "document_run_id": "run-1"},
        ],
        artifacts=[{"document_id": 7, "title": "调研报告", "format": "docx", "version_no": 2}],
        preview_document={"document_id": 7, "title": "调研报告", "version_no": 2},
        attachments=[{"name": "notes.md", "path": "uploads/notes.md"}],
    )
    assert "本轮用户消息" in prompt and "放到md文档中" in prompt
    assert "该轮产生了文档产物" in prompt
    assert "《调研报告》" in prompt and "document_id=7" in prompt
    assert "notes.md" in prompt


def test_apply_intent_chat_releases_stale_document_binding():
    """会话里有文档、用户只是提问：模型判 chat 后不得再绑定文档/文档分析。"""
    rules = route_task(
        "帮我解释一下这个方案的核心取舍", document_id=7, has_document_context=True,
    )
    assert rules["task_type"] == "document_analyze"
    route = apply_intent(
        {"intent": "chat", "deliverable_format": "none", "target": "none", "reason": "普通问答"},
        rules, has_existing_document=True, has_attachments=False,
    )
    assert route["task_type"] == "general_chat"
    assert route["document_id"] is None
    assert "document_context" not in route
    assert route["intent_source"] == "llm"


def test_apply_intent_markdown_delivery_uses_general_capability():
    rules = route_task("把上面的调研结果整理成 md 文件给我下载", has_document_context=True)
    route = apply_intent(
        {"intent": "create_document", "deliverable_format": "md", "target": "latest_draft",
         "reason": "交付 md 文件"},
        rules, has_existing_document=True, has_attachments=False,
    )
    assert route["task_type"] == "general_chat"
    assert route["deliverable"] == {"format": "md"}
    assert route["document_id"] is None


def test_apply_intent_word_delivery_keeps_document_graph():
    rules = route_task("把上面的调研结果整理成 Word 给我", has_document_context=True)
    route = apply_intent(
        {"intent": "create_document", "deliverable_format": "docx", "target": "latest_draft",
         "reason": "交付 Word"},
        rules, has_existing_document=True, has_attachments=False,
    )
    assert route["task_type"] == "document_generate"
    assert "deliverable" not in route


def test_apply_intent_edit_without_target_degrades_to_chat():
    rules = route_task("第 2 章改得再具体些")
    route = apply_intent(
        {"intent": "edit_document", "deliverable_format": "none", "target": "none",
         "reason": "修改文档"},
        rules, has_existing_document=False, has_attachments=False,
    )
    assert route["task_type"] == "general_chat"


def test_apply_intent_edit_with_preview_target():
    rules = route_task("第 2 章改得再具体些")
    route = apply_intent(
        {"intent": "edit_document", "deliverable_format": "none", "target": "preview",
         "reason": "修改预览文档"},
        rules, has_existing_document=True, has_attachments=False,
    )
    assert route["task_type"] == "document_edit"


def test_user_grants_autonomy_detection():
    """用户明确说"不用再问、缺的自行假设"时必须识别为自主授权。"""
    for text in (
        "不需要联网，也不需要再向我确认额外信息，缺少的信息采用通用假设并在文中注明",
        "缺的资料你自己决定，不用问我",
        "资料不足的部分按合理假设补全",
        "no need to ask me, just assume",
    ):
        assert user_grants_autonomy(text), text
    for text in (
        "生成一份考勤报告",
        "先确认一下需求再开始",
        "还需要我提供什么资料？",
    ):
        assert not user_grants_autonomy(text), text


def test_document_turn_marks_autonomous_requests():
    """授权的文档任务要把 autonomous 写进上下文，后续不得再弹资料表单。"""
    message = "写一份《AI Agent 评测入门》Word 文档给我，不需要再向我确认额外信息，缺少的用通用假设"
    route = document_turn(message, [], route_task(message))[0]
    assert route["task_type"] == "document_generate"
    assert route["document_context"]["autonomous"] is True

    normal = "写一份《AI Agent 评测入门》Word 文档给我"
    route = document_turn(normal, [], route_task(normal))[0]
    assert route["document_context"]["autonomous"] is False


def test_document_turn_does_not_upgrade_when_model_says_chat():
    """模型已判定本轮为对话时，历史文档草稿不得把消息升级成文档生成。"""
    report = "# 调研报告\n\n## 一、背景\n\n" + "背景说明。" * 80
    history = [
        {"role": "user", "content": "帮我调研 agent 框架"},
        {"role": "assistant", "content": report, "document_run_id": "run-1"},
    ]
    message = "把上面的内容整理成几条要点"
    rules = route_task(message)
    route, refs, run = document_turn(message, history, rules, allow_upgrade=False)
    assert route["task_type"] == "general_chat"
    assert refs == [] and run is None
    # Word 交付仍允许历史草稿直接成稿
    word = "把上面的内容整理成 Word 文档"
    route = document_turn(word, history, route_task(word), allow_upgrade=True)[0]
    assert route["task_type"] == "document_generate"
