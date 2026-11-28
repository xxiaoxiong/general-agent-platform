"""P4-01/P4-02：Task Contract + Verification（§9 完成条件与验证器）。

运行：
    pytest backend/tests/test_task_verification.py -q
"""
import pytest
import pytest_asyncio

from app.services.assistant.task_contract import (
    TASK_BUSINESS,
    TASK_CODE,
    TASK_DB,
    TASK_DOC,
    TASK_OTHER,
    TASK_QA,
    TASK_RESEARCH,
    build_task_contract,
    infer_task_type,
)
from app.services.assistant.verification import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PASSED,
    ToolEvidence,
    verify_task,
)


class TestTaskContract:
    def test_infer_from_capability(self):
        assert infer_task_type("随便", ["web_research"]) == (TASK_RESEARCH, "capability")
        assert infer_task_type("随便", ["coding"]) == (TASK_CODE, "capability")
        # general/knowledge 不强加类型（简单问答不背验证负担）
        assert infer_task_type("今天天气", ["general", "knowledge"])[0] == TASK_OTHER

    def test_infer_from_tools(self):
        assert infer_task_type("", tool_names=["db_query"])[0] == TASK_DB
        assert infer_task_type("", tool_names=["python_exec"])[0] == TASK_CODE
        assert infer_task_type("", tool_names=["web_search"])[0] == TASK_RESEARCH
        assert infer_task_type("", tool_names=["http_post"])[0] == TASK_BUSINESS

    def test_infer_from_keywords(self):
        assert infer_task_type("帮我写一份行业研究报告")[0] == TASK_RESEARCH
        assert infer_task_type("生成一份项目方案文档")[0] == TASK_DOC
        assert infer_task_type("重构这个函数")[0] == TASK_CODE
        assert infer_task_type("发送邮件通知客户")[0] == TASK_BUSINESS
        assert infer_task_type("今天星期几")[0] == TASK_OTHER

    def test_contract_checks_by_type(self):
        c = build_task_contract("研究行业趋势")
        assert c.task_type == TASK_RESEARCH
        assert "citations_present" in c.completion_checks
        assert "web_evidence" in c.completion_checks  # 研究任务必须有真实检索证据
        assert c.is_complex

        simple = build_task_contract("你好")
        assert simple.task_type in (TASK_OTHER, TASK_QA)
        assert not simple.is_complex

    def test_web_evidence_omitted_when_web_disallowed(self):
        # 用户明确禁网：不强加检索证据义务
        c = build_task_contract("研究行业趋势，不要联网")
        assert "citations_present" in c.completion_checks
        assert "web_evidence" not in c.completion_checks
        # 本次 Run 无网络检索工具（web_enabled=False 收窄后）：同样豁免
        c2 = build_task_contract("研究行业趋势", web_searchable=False)
        assert "web_evidence" not in c2.completion_checks

    def test_contract_dict(self):
        d = build_task_contract("查询数据库销售数据").to_dict()
        assert d["task_type"] == TASK_DB
        assert d["goal"] == "查询数据库销售数据"
        assert "db_evidence" in d["completion_checks"]

    def test_completion_requirements_follow_explicit_deliverables(self):
        contract = build_task_contract("深入核对表中给定的数据。保存为 `/workspace/audit/report.md` 与 `facts.json` 两个文件，不要联网。")
        assert contract.artifact_paths == ("audit/report.md", "facts.json")
        assert "artifact_produced" in contract.completion_checks
        assert "citations_present" not in contract.completion_checks
        assert build_task_contract("怎么用数据库验证统计结果", capability_ids=["data_analysis"]).completion_checks == ["answer_present"]

    def test_runnable_project_preserves_the_requirements_from_conversation_137(self):
        request = (
            "构建并交付可直接运行的前后端分离项目「设备保养与备件工作台」，文件必须可以从对话下载。\n"
            "实际功能包括：\n"
            "1. 登记设备保养工单，录入标题、设备编号、计划日期与优先级，支持搜索和筛选。\n"
            "2. 启动工单、申请一条或多条备件并登记领取。备件数量必须为正整数。存在未领取备件时阻止完工，并在页面提示原因。\n"
            "3. 完工后刷新页面仍能看到工单、备件和状态。工作台显示状态统计、校验错误和空状态。\n"
            "技术约束是 Vue 3.5、TypeScript 5 和 Vite 7 的真实生产构建，使用 @vitejs/plugin-vue 6 和 vue-tsc 3。"
            "后端用 FastAPI 0.125.0、Uvicorn 0.38.0 和 SQLite。可直接用浏览器 fetch 和 CSS。"
            "后端启动端口 8000，Vite 预览端口 5173，使用项目自己的同源 API 代理。\n"
            "交付源码 ZIP、启动与测试说明、OpenAPI 和可执行测试。请用浏览器验证至少一条从创建到完工的操作，"
            "以及未领取备件时不能完工的业务规则，不要用写死的数据模拟结果。"
        )
        contract = build_task_contract(request)
        assert contract.task_type == TASK_CODE
        assert contract.project_scope == "fullstack"
        assert contract.completion_checks == ["answer_present", "artifact_produced"]
        expected = ("runtime_start", "runtime_build", "runtime_tests", "runtime_browser", "openapi", "archive")
        assert contract.project_checks == expected
        assert contract.to_dict()["project_checks"] == list(expected)

    def test_project_requirements_preserve_named_platform_deliverables(self):
        contract = build_task_contract(
            "创建 FastAPI 后端 API 服务。执行测试。交付 `reports/pipeline.zip` 和 `docs/openapi.json`，提供浏览器测试说明。"
        )
        assert contract.project_scope == "backend"
        assert contract.project_checks == ("runtime_start", "runtime_tests", "openapi", "archive")
        assert contract.project_archive_path == "reports/pipeline.zip"
        assert contract.project_openapi_path == "docs/openapi.json"
        assert contract.artifact_paths == ()

    @pytest.mark.parametrize("prompt", [
        "如何创建前后端分离项目并验证浏览器流程？",
        "生成前后端系统的用户手册。交付 PDF 和 ZIP 压缩包。",
        "搭建一个前端项目，但只要方案。",
        "重构现有的脚本，保存为 `tools/setup.py`。",
    ])
    def test_project_verification_does_not_apply_to_advice_documents_or_discrete_files(self, prompt):
        result = build_task_contract(prompt)
        assert result.project_scope is None
        assert result.project_checks == ()

    def test_optional_project_checks_do_not_turn_into_requirements(self):
        contract = build_task_contract("实现前端网站。提供测试说明，不用浏览器验证，无需 OpenAPI，不用打包 ZIP。")
        assert contract.project_scope == "frontend"
        assert contract.project_checks == ("runtime_start",)

    def test_project_checks_follow_explicit_implementation_and_stack_clauses(self):
        contract = build_task_contract("开发一套工单管理系统。前端用Vue，后端使用FastAPI。交付源码ZIP和OpenAPI。")
        assert contract.project_scope == "fullstack"
        assert contract.project_checks == ("runtime_start", "openapi", "archive")
        repair = build_task_contract("修复当前前端项目，确保 npm run build 成功。执行测试。")
        assert repair.project_scope == "frontend"
        assert repair.project_checks == ("runtime_start", "runtime_build", "runtime_tests")

    def test_table_counts_with_exactly_identical_duplicates_are_independently_recomputed(self):
        prompt = (
            "核对有效记录，按事件编号去重，忽略 VOID。保存为 facts.json，字段 valid_rows、duplicate_ids 和 void_ids。\n"
            "格式为 编号,名称,状态：\nR1,琴,OK\nR2,棋,VOID\nR1,琴,OK\nR3,书,OK"
        )
        contract = build_task_contract(prompt)
        assert contract.expected_json == {"valid_rows": 2, "duplicate_ids": ["R1"], "void_ids": ["R2"]}
        assert contract.table_facts.raw_rows == 4
        ev = [ToolEvidence("write_file", path="facts.json", artifact_content={**contract.expected_json, "valid_rows": 1})]
        result = verify_task(contract, "有效记录 1 条。", ev)
        assert {item.id for item in result.failed_checks} == {"table_facts"}
        ev[0].artifact_content = contract.expected_json
        assert verify_task(contract, "有效记录 2 条，原始记录 4 条。", ev).status == STATUS_PASSED
        conflict = build_task_contract(prompt.replace("R1,琴,OK\nR3", "R1,画,OK\nR3"))
        assert conflict.table_facts is None


class TestVerification:
    def test_answer_present(self):
        c = build_task_contract("你好")
        r = verify_task(c, "", [])
        assert r.status == STATUS_FAILED  # 空回答不得按完成交付

        r2 = verify_task(c, "回答内容", [])
        assert r2.status == STATUS_PASSED

    def test_research_web_evidence_hard_gate(self):
        """研究任务无真实检索证据 → 直接 failed（防止凭记忆编造调研结论，143 任务教训）。"""
        c = build_task_contract("深度研究 agent swarm 的进展")
        assert "web_evidence" in c.completion_checks

        # 0 次检索：即使引用了链接也不得交付
        r = verify_task(c, "结论，详见 https://example.com/report", [])
        assert r.status == STATUS_FAILED
        ids = {x.id: x.passed for x in r.checks}
        assert ids["web_evidence"] is False
        web = next(x for x in r.checks if x.id == "web_evidence")
        assert "web_search" in web.detail  # 反馈指引模型先去检索

        # 检索尝试但全部失败：同样不通过（结论缺少可核验来源）
        ev_fail = [ToolEvidence(tool_name="web_search", status="failed", output="timeout")]
        r2 = verify_task(c, "结论", ev_fail)
        assert r2.status == STATUS_FAILED

        # 检索成功 + 引用真实来源：通过
        ev_ok = [ToolEvidence(tool_name="web_search", status="succeeded",
                              output="详见 https://src.example.com/1")]
        r3 = verify_task(c, "结论 https://src.example.com/1", ev_ok)
        assert r3.status == STATUS_PASSED

        # 用户提供来源：豁免检索证据义务
        c2 = build_task_contract("研究行业趋势，来源资料 https://s.example.com/1")
        assert "web_evidence" in c2.completion_checks
        r4 = verify_task(c2, "结论 https://s.example.com/1", [])
        ids4 = {x.id: x.passed for x in r4.checks}
        assert ids4["web_evidence"] is True

    def test_research_citations(self):
        c = build_task_contract("研究行业趋势")
        # 无来源且无联网工具：不通过
        r = verify_task(c, "结论：行业增长", [])
        assert r.status in (STATUS_FAILED, STATUS_PARTIAL)
        ids = {x.id: x.passed for x in r.checks}
        assert ids["citations_present"] is False

        # 模型自报来源不能替代真实读取。
        r2 = verify_task(c, "结论，来源：https://example.com/report", [])
        assert {x.id: x.passed for x in r2.checks}["citations_present"] is False

        # 联工具有输出 URL：通过
        ev = [ToolEvidence(tool_name="web_search", status="succeeded",
                           output="详见 https://src.example.com/1")]
        assert verify_task(c, "结论未引用链接", ev).status == STATUS_FAILED
        r3 = verify_task(c, "结论 https://src.example.com/1", ev)
        assert {x.id: x.passed for x in r3.checks}["citations_present"] is True
        ev[0].status = "failed"
        assert verify_task(c, "结论 https://src.example.com/1", ev).status == STATUS_FAILED

    def test_uploaded_document_analysis_uses_local_citation(self):
        """上传文档分析：来源义务由已读文件承担，不强制网络检索证据。"""
        attachment = (
            "深度解析这份调研报告的结构与论述不足\n\n[附件]（文件已存入会话工作区。回答前请务必先用 read_file 逐个读取以下文件）\n"
            "- /workspace/uploads/report.docx（文本副本：/workspace/uploads/report.docx.extracted.txt）"
        )
        contract = build_task_contract(attachment)
        assert "citations_present" in contract.completion_checks
        assert "web_evidence" not in contract.completion_checks

        # 未读取文件：即使正文写了引用也不通过
        r = verify_task(contract, "报告结构完整，详见 [1]。", [])
        assert {x.id: x.passed for x in r.checks}["citations_present"] is False

        # 已成功读取且有编号引用：通过
        evidence = [ToolEvidence(tool_name="read_file", status="succeeded", output="# 报告正文")]
        r2 = verify_task(contract, "报告结论见 [1]，另见来源清单。", evidence)
        assert {x.id: x.passed for x in r2.checks}["citations_present"] is True
        assert r2.status == STATUS_PASSED

        # 已成功读取但正文没有任何引用标注：仍不通过
        r3 = verify_task(contract, "报告结构完整。", evidence)
        assert {x.id: x.passed for x in r3.checks}["citations_present"] is False

    def test_uploaded_document_with_explicit_web_intent_keeps_web_gate(self):
        """附件 + 明确要求联网检索时，仍要求真实网络检索证据。"""
        request = ("参考上传的报告，再联网调研 2025 年最新进展并给出对比\n\n[附件]（附件说明）\n"
                   "- /workspace/uploads/report.docx")
        contract = build_task_contract(request)
        assert "web_evidence" in contract.completion_checks
        evidence = [ToolEvidence(tool_name="read_file", status="succeeded", output="正文")]
        r = verify_task(contract, "对比结论见 [1]。", evidence)
        assert {x.id: x.passed for x in r.checks}["web_evidence"] is False
        assert r.status == STATUS_FAILED

    def test_doc_artifact(self):
        c = build_task_contract("生成一份项目方案文档")
        r = verify_task(c, "已生成文档", [])
        assert {x.id: x.passed for x in r.checks}["artifact_produced"] is False

        ev = [ToolEvidence(tool_name="write_file", status="succeeded", path="report.md")]
        r2 = verify_task(c, "已生成文档", ev)
        assert r2.status == STATUS_PASSED

    def test_db_evidence(self):
        c = build_task_contract("查询数据库销售数据")
        r = verify_task(c, "销售数据为 42", [])
        assert {x.id: x.passed for x in r.checks}["db_evidence"] is False

        ev = [ToolEvidence(tool_name="db_query", status="succeeded", output="42")]
        r2 = verify_task(c, "销售数据为 42", ev)
        assert r2.status == STATUS_PASSED

    def test_side_effect_evidence(self):
        c = build_task_contract("发送邮件通知客户")
        ev_unknown = [ToolEvidence(tool_name="send_email_tool", status="unknown")]
        r = verify_task(c, "已发送", ev_unknown)
        assert {x.id: x.passed for x in r.checks}["side_effect_evidence"] is False

        ev_ok = [ToolEvidence(tool_name="send_email_tool", status="succeeded",
                              external_operation_id="op-1")]
        r2 = verify_task(c, "已发送", ev_ok)
        assert r2.status == STATUS_PASSED

    def test_partial_status(self):
        """answer 通过但证据缺口：partial（可交付但标记复核）。"""
        c = build_task_contract("研究行业趋势并生成方案文档")  # research 或 doc
        # 只满足一半：构造 research 契约 + 有回答无来源
        from app.services.assistant.task_contract import TaskContract

        contract = TaskContract(
            goal="x", task_type=TASK_RESEARCH,
            completion_checks=["answer_present", "citations_present", "artifact_produced"],
        )
        ev = [ToolEvidence(tool_name="write_file", status="succeeded", path="report.md")]
        r = verify_task(contract, "有回答但没来源", ev)
        # 3 检查 1 失败 → partial
        assert r.status == STATUS_PARTIAL

    def test_requires_action_requires_execution_evidence(self):
        """决策判定为执行型任务：仅声称完成、没有工具执行记录时必须判失败。"""
        c = build_task_contract("清空目录下所有文件", decision_intent="general_chat",
                                requires_action=True)
        assert "action_evidence" in c.completion_checks
        r = verify_task(c, "目录已清空。", [])
        assert {item.id: item.passed for item in r.checks}["action_evidence"] is False
        assert r.status == STATUS_FAILED

        ev = [ToolEvidence(tool_name="execute_command", status="succeeded")]
        r2 = verify_task(c, "目录已清空。", ev)
        assert r2.status == STATUS_PASSED

        # 未要求实际执行时，不额外增加执行证据义务
        plain = build_task_contract("解释一下什么是目录", decision_intent="general_chat")
        assert "action_evidence" not in plain.completion_checks

    def test_action_claim_requires_evidence(self):
        """声称“已执行/已清空”但没有任何执行记录时必须判定为缺口。"""
        from app.services.assistant.verification import action_claim_checks, action_claim_present

        assert action_claim_present("目录已清空。")
        assert not action_claim_present("可以执行 rm -rf 来清空目录。")
        failed = action_claim_checks("目录已清空。", [])
        assert [item.id for item in failed] == ["action_evidence"]
        assert action_claim_checks(
            "目录已清空。", [ToolEvidence(tool_name="execute_command", status="succeeded")],
        ) == []

    def test_feedback_text_lists_failed_checks(self):
        c = build_task_contract("研究行业趋势")
        r = verify_task(c, "结论无来源", [])
        fb = r.feedback_text()
        assert "完成条件" in fb
        assert "citations_present" in fb


class TestVerificationRuntimeIntegration:
    """Verification 挂入 Runtime 完成路径（结构化事件 + summary）。"""

    @pytest_asyncio.fixture(autouse=True)
    async def _db(self):
        import app.models  # noqa: F401  注册全部模型到 Base.metadata
        from app.core.database import init_db

        await init_db()
        yield

    @pytest.mark.asyncio
    async def test_verification_events_and_summary(self, monkeypatch):
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.services.agent_runtime import get_runtime_service

        class ScriptedModel(BaseChatModel):
            script: list = []

            @property
            def _llm_type(self):
                return "fake-verify"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                item = self.script.pop(0) if self.script else AIMessage(content="（结束）")
                msg = item() if callable(item) else item
                return ChatResult(generations=[ChatGeneration(message=msg)])

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "fake-provider", "base_url": "http://fake.local/v1", "api_key": "none",
                 "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(
            factory_mod, "build_chat_model",
            lambda *a, **k: ScriptedModel(script=[
                AIMessage(content="初步结论"),              # 主执行
                AIMessage(content="修正结论，来源：https://s.example.com/1"),  # 补充执行
            ]),
        )
        monkeypatch.setattr(svc_mod.settings, "AGENT_VERIFICATION_MAX_ROUNDS", 1, raising=False)

        svc = get_runtime_service()
        result = await svc.run_to_completion(
            agent_config={"id": 990301, "tools": [], "capability_ids": ["web_research"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "研究行业趋势，来源资料 https://s.example.com/1"}],
            user_id=1,
        )
        assert result["status"] == "completed"
        assert result["content"] == "修正结论，来源：https://s.example.com/1"
        # 验证通过结构化事件（verification.started/completed）
        assert result["run_id"]

        from app.core.database import AsyncSessionLocal
        from app.models.agent import AgentRunEvent
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentRunEvent).where(AgentRunEvent.run_id == result["run_id"])
            )).scalars().all()
        types = [r.event_type for r in rows]
        assert "verification_started" in types
        assert "verification_completed" in types
        # research 任务第一轮无来源 → 补充执行（run_retrying）
        assert "run_retrying" in types

        # 验证结果随 summary 交付
        from app.models.agent import AgentRun

        async with AsyncSessionLocal() as db:
            run = (await db.execute(
                select(AgentRun).where(AgentRun.id == result["run_id"])
            )).scalar_one()
        ver = (run.summary or {}).get("verification")
        assert ver is not None
        assert ver["task_type"] == "research"
        assert ver["status"] == "passed" and ver["fix_rounds"] == 1

    @pytest.mark.asyncio
    async def test_subagent_events_published_through_runtime(self, monkeypatch):
        """子代理过程透传：task 委派必须发布 subagent_started/completed（P0 痛点回归）。"""
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.services.agent_runtime import get_runtime_service

        class ScriptedModel(BaseChatModel):
            script: list = []

            @property
            def _llm_type(self):
                return "fake-subagent"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                item = self.script.pop(0) if self.script else AIMessage(content="（结束）")
                msg = item() if callable(item) else item
                return ChatResult(generations=[ChatGeneration(message=msg)])

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "fake-provider", "base_url": "http://fake.local/v1", "api_key": "none",
                 "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(
            factory_mod, "build_chat_model",
            lambda *a, **k: ScriptedModel(script=[
                AIMessage(content="", tool_calls=[{
                    "name": "task", "id": "t1", "type": "tool_call",
                    "args": {"description": "调研 A", "subagent_type": "researcher"},
                }]),   # 主代理：委派 researcher
                AIMessage(content="调研摘要内容"),   # researcher 子代理的模型回合
                AIMessage(content="汇总完成"),       # 主代理收回结果后最终回答
            ]),
        )

        svc = get_runtime_service()
        result = await svc.run_to_completion(
            agent_config={"id": 990303, "tools": ["web_search"], "capability_ids": ["web_research"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "帮我整理一下"}],
            user_id=1,
        )
        assert result["status"] in ("completed", "failed")

        from app.core.database import AsyncSessionLocal
        from app.models.agent import AgentRunEvent
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentRunEvent.event_type, AgentRunEvent.payload).where(
                    AgentRunEvent.run_id == result["run_id"])
            )).all()
        types = [r[0] for r in rows]
        assert "subagent_started" in types, types
        assert "subagent_completed" in types, types
        started = next(r[1] for r in rows if r[0] == "subagent_started")
        assert started.get("name") == "researcher"

    @pytest.mark.asyncio
    async def test_simple_qa_no_verification_burden(self, monkeypatch):
        """简单问答：无补充执行（不给简单问题加验证负担）。"""
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.services.agent_runtime import get_runtime_service

        class OneShot(BaseChatModel):
            @property
            def _llm_type(self):
                return "fake-qa"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(generations=[ChatGeneration(
                    message=AIMessage(content="你好呀"))])

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "fake-provider", "base_url": "http://fake.local/v1", "api_key": "none",
                 "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: OneShot())

        svc = get_runtime_service()
        result = await svc.run_to_completion(
            agent_config={"id": 990302, "tools": [], "capability_ids": ["general"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "你好"}],
            user_id=1,
        )
        assert result["status"] == "completed"
        assert result["content"] == "你好呀"

        from app.core.database import AsyncSessionLocal
        from app.models.agent import AgentRun
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            run = (await db.execute(
                select(AgentRun).where(AgentRun.id == result["run_id"])
            )).scalar_one()
        ver = (run.summary or {}).get("verification")
        assert ver is None or ver.get("fix_rounds", 0) == 0

    @pytest.mark.asyncio
    async def test_external_receipt_allows_plan_bookkeeping_rework(self, monkeypatch):
        """外部操作已成功但计划未关闭：补记计划属于账务性缺口，不得直接判失败。"""
        import app.services.agent_runtime.completion as completion_mod
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.services.agent_runtime import get_runtime_service

        def plan(status):
            return AIMessage(content="", tool_calls=[{
                "name": "write_todos", "id": f"plan-{status}", "type": "tool_call",
                "args": {"todos": [{"content": "清空目录下所有文件", "status": status}]},
            }])

        class ScriptedModel(BaseChatModel):
            script: list = []

            @property
            def _llm_type(self):
                return "fake-external-bookkeeping"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                item = self.script.pop(0) if self.script else AIMessage(content="（结束）")
                msg = item() if callable(item) else item
                return ChatResult(generations=[ChatGeneration(message=msg)])

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "fake-provider", "base_url": "http://fake.local/v1", "api_key": "none",
                 "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        async def fake_evidence(ctx):
            # 模拟 execute_command 已成功：工具账本存在外部回执
            return [], [], True

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(completion_mod, "_collect_evidence", fake_evidence)
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: ScriptedModel(script=[
            plan("in_progress"),
            AIMessage(content="目录已清空。"),
            plan("completed"),
            AIMessage(content="目录已清空，计划已关闭。"),
        ]))

        svc = get_runtime_service()
        result = await svc.run_to_completion(
            agent_config={"id": 990304, "tools": [], "capability_ids": ["general"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "清空目录下所有文件"}],
            user_id=1,
        )
        assert result["status"] == "completed"
        assert result["content"] == "目录已清空，计划已关闭。"

        from app.core.database import AsyncSessionLocal
        from app.models.agent import AgentRun
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            run = (await db.execute(
                select(AgentRun).where(AgentRun.id == result["run_id"])
            )).scalar_one()
        ver = (run.summary or {}).get("verification") or {}
        assert ver.get("status") == "passed"
        assert ver.get("stop_reason") in ("", None)
        assert ver.get("fix_rounds") == 1
