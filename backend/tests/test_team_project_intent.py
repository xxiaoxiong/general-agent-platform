"""软件工程交付须由团队模式与实际实施意图共同触发。"""

import pytest

from app.services.agent_runtime.team_project_intent import project_scope


@pytest.mark.parametrize(("message", "mode", "selected", "expected"), [
    ("帮我构建一个前后端分离的项目，你随意发挥", "auto", False, "fullstack"),
    ("做一个交互完整的设备看板网页", "chat", False, "frontend"),
    ("请实现一个 REST API 服务，提供代码和测试", "auto", False, "backend"),
    ("Build a runnable full-stack inventory application", "auto", False, "fullstack"),
    ("怎么做一个前后端分离的项目？", "auto", False, None),
    ("只写方案，不用实现这个前后端分离的项目", "auto", False, None),
    ("帮我生成前后端分离项目的需求文档", "auto", False, None),
    ("生成一个 Word 项目方案", "document_generate", False, None),
    ("实现一个网站", "auto", True, None),
    ("对比 Flask 和 FastAPI，给出取舍", "auto", False, None),
    ("创建一个评估表并解释预测结果", "auto", False, None),
])
def test_project_scope_distinguishes_implementation_from_discussion(message, mode, selected, expected):
    assert project_scope(message, mode, has_selected_document=selected) == expected
