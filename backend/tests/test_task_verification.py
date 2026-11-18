"""P4-01/P4-02：Task Contract + Verification（§9 完成条件与验证器）。

运行：
    pytest backend/tests/test_task_verification.py -q
"""
import pytest
import pytest_asyncio

from app.assistant.task_contract import (
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
from app.assistant.verification import (
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
        assert c.is_complex

        simple = build_task_contract("你好")
        assert simple.task_type in (TASK_OTHER, TASK_QA)
        assert not simple.is_complex

    def test_contract_dict(self):
        d = build_task_contract("查询数据库销售数据").to_dict()
        assert d["task_type"] == TASK_DB
        assert d["goal"] == "查询数据库销售数据"
        assert "db_evidence" in d["completion_checks"]


class TestVerification:
    def test_answer_present(self):
        c = build_task_contract("你好")
        r = verify_task(c, "", [])
        assert r.status == STATUS_FAILED  # 空回答不得按完成交付

        r2 = verify_task(c, "回答内容", [])
        assert r2.status == STATUS_PASSED

    def test_research_citations(self):
        c = build_task_contract("研究行业趋势")
        # 无来源且无联网工具：不通过
        r = verify_task(c, "结论：行业增长", [])
        assert r.status in (STATUS_FAILED, STATUS_PARTIAL)
        ids = {x.id: x.passed for x in r.checks}
        assert ids["citations_present"] is False

        # 回答带来源 URL：通过
        r2 = verify_task(c, "结论，来源：https://example.com/report", [])
        assert {x.id: x.passed for x in r2.checks}["citations_present"] is True

        # 联工具有输出 URL：通过
        ev = [ToolEvidence(tool_name="web_search", status="succeeded",
                           output="详见 https://src.example.com/1")]
        r3 = verify_task(c, "结论", ev)
        assert {x.id: x.passed for x in r3.checks}["citations_present"] is True

    def test_doc_artifact(self):
        c = build_task_contract("生成一份项目方案文档")
        r = verify_task(c, "已生成文档", [])
        assert {x.id: x.passed for x in r.checks}["artifact_produced"] is False

        ev = [ToolEvidence(tool_name="write_file", status="succeeded")]
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
        from app.assistant.task_contract import TaskContract

        contract = TaskContract(
            goal="x", task_type=TASK_RESEARCH,
            completion_checks=["answer_present", "citations_present", "artifact_produced"],
        )
        ev = [ToolEvidence(tool_name="write_file", status="succeeded")]
        r = verify_task(contract, "有回答但没来源", ev)
        # 3 检查 1 失败 → partial
        assert r.status == STATUS_PARTIAL

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
            messages=[{"role": "user", "content": "研究行业趋势"}],
            user_id=1,
        )
        assert result["status"] == "completed"
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
        assert ver["status"] in ("passed", "partial")

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
