"""Goal Runtime 与 Deep Research 验收测试。

覆盖：
- 目标契约：从 TaskContract/决策构建稳定验收 ID、revision 修订、进度计算；
- 目标监督：未通过继续、连续无进展暂停、全部通过才 completed；
- 研究证据链：真实读取、原文定位、Claim 核验与完成检查；
- /api/v2/goals 查询与目标修订。
"""
import os
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentRun, AgentRunSteering, AgentSession
from app.models.goal import GoalContract, GoalIteration
from app.models.research import (
    ResearchClaim, ResearchEvidence, ResearchPlan, ResearchSource,
)
from app.models.user import User

sync_engine = create_engine(os.environ["DATABASE_SYNC_URL"])


@pytest.fixture(scope="module")
def runtime_setup():
    from app.core.database import Base

    Base.metadata.create_all(sync_engine)
    suffix = uuid.uuid4().hex[:8]
    runs: list[str] = []
    sessions: list[int] = []
    with Session(sync_engine) as db:
        user = User(
            username=f"goal_rt_{suffix}", email=f"goal_rt_{suffix}@test.dev",
            hashed_password="x", is_active=True,
        )
        db.add(user)
        db.commit()
        agent = Agent(
            name=f"goal-rt-agent-{suffix}", system_prompt="目标运行时测试",
            temperature="0.7", tools=[], is_active=True, owner_id=user.id,
        )
        db.add(agent)
        db.commit()
        user_id, agent_id = user.id, agent.id
    yield {"user_id": user_id, "agent_id": agent_id, "runs": runs, "sessions": sessions}
    with Session(sync_engine) as db:
        if runs:
            db.execute(delete(GoalIteration).where(GoalIteration.run_id.in_(runs)))
            db.execute(delete(GoalContract).where(GoalContract.run_id.in_(runs)))
            db.execute(delete(ResearchEvidence).where(ResearchEvidence.research_id.in_(
                select(ResearchPlan.research_id).where(ResearchPlan.run_id.in_(runs))
            )))
            db.execute(delete(ResearchClaim).where(ResearchClaim.research_id.in_(
                select(ResearchPlan.research_id).where(ResearchPlan.run_id.in_(runs))
            )))
            db.execute(delete(ResearchSource).where(ResearchSource.research_id.in_(
                select(ResearchPlan.research_id).where(ResearchPlan.run_id.in_(runs))
            )))
            db.execute(delete(ResearchPlan).where(ResearchPlan.run_id.in_(runs)))
            db.execute(delete(AgentRunSteering).where(AgentRunSteering.run_id.in_(runs)))
            db.execute(delete(AgentRun).where(AgentRun.id.in_(runs)))
        if sessions:
            db.execute(delete(AgentSession).where(AgentSession.id.in_(sessions)))
        db.execute(delete(Agent).where(Agent.id == agent_id))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()


def _make_run(setup, *, title="目标测试"):
    run_id = uuid.uuid4().hex
    with Session(sync_engine) as db:
        session = AgentSession(
            agent_id=setup["agent_id"], user_id=setup["user_id"], title=title,
            messages=[], version=0,
        )
        db.add(session)
        db.commit()
        db.add(AgentRun(
            id=run_id, agent_id=setup["agent_id"], session_id=session.id,
            user_id=setup["user_id"], thread_id=f"agent-{setup['agent_id']}-session-{session.id}",
            status="running", input_payload={"messages": [{"role": "user", "content": title}]},
            agent_config_snapshot={"goal_mode": True}, created_at=datetime.utcnow(),
        ))
        db.commit()
        setup["sessions"].append(session.id)
    setup["runs"].append(run_id)
    return run_id, session.id


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_contract_build_revision_and_progress(runtime_setup):
    from app.services.assistant.task_contract import build_task_contract
    from app.services.goal_runtime.contracts import (
        ensure_goal_contract, progress_of, revise_goal_contract,
    )

    run_id, session_id = _make_run(runtime_setup, title="构建电商项目并交付源码")
    task_contract = build_task_contract(
        "构建一个前后端分离的电商项目，用浏览器验证，并打包源码 ZIP",
    )
    contract = await ensure_goal_contract(
        run_id=run_id,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=session_id,
        objective="构建一个前后端分离的电商项目，用浏览器验证，并打包源码 ZIP",
        task_contract=task_contract,
        decision={"acceptance_criteria": ["首页可访问", "购物车可用"], "constraints": ["使用 Vue 3"]},
    )
    assert contract["goal_id"] and contract["revision"] == 1
    descriptions = [item["description"] for item in contract["acceptance_criteria"]]
    assert any("项目可真实启动" in item for item in descriptions)
    assert any("浏览器端验收" in item for item in descriptions)
    assert any("首页可访问" in item for item in descriptions)
    assert [item["id"] for item in contract["acceptance_criteria"]][:2] == ["AC-001", "AC-002"]
    assert contract["progress"]["percent"] == 0
    assert progress_of(contract["acceptance_criteria"])["required_total"] >= 3

    revised = await revise_goal_contract(
        goal_id=contract["goal_id"],
        user_id=runtime_setup["user_id"],
        objective="构建电商项目并交付源码 ZIP 与运行说明",
        acceptance_criteria=["首页可访问", "结算流程可用"],
        note="补充结算流程",
    )
    assert revised["revision"] == 2
    assert revised["objective"].endswith("运行说明")
    revised_descriptions = [item["description"] for item in revised["acceptance_criteria"]]
    assert "结算流程可用" in revised_descriptions
    # 原有未删除的验收项保留（不静默删除用户条件）
    assert "首页可访问" in revised_descriptions


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_supervisor_continues_then_blocks_and_completes(runtime_setup):
    from app.services.goal_runtime.contracts import ensure_goal_contract
    from app.services.goal_runtime.supervisor import supervise_goal

    run_id, session_id = _make_run(runtime_setup, title="生成报告并保存到 /workspace/report.md")
    from app.services.assistant.task_contract import build_task_contract

    task_contract = build_task_contract("生成报告并保存到 /workspace/report.md")
    contract = await ensure_goal_contract(
        run_id=run_id,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=session_id,
        objective="生成报告并保存到 /workspace/report.md",
        task_contract=task_contract,
        decision={},
    )
    goal_id = contract["goal_id"]
    text = "已完成分析。\n\n## 目标核对\n| 目标项 | 完成情况 | 证据 |\n| --- | --- | --- |\n| 报告 | 未完成 | 尚未写入 |"
    checks = {
        "answer_present": {"id": "answer_present", "passed": True, "detail": ""},
        "artifact_produced": {"id": "artifact_produced", "passed": False, "detail": "缺少交付文件"},
        "goal_plan_present": {"id": "goal_plan_present", "passed": True, "detail": ""},
        "goal_audit_present": {"id": "goal_audit_present", "passed": True, "detail": ""},
    }
    first = await supervise_goal(
        goal_id=goal_id, run_id=run_id, text=text, checks=checks,
        failed_ids=["artifact_produced"], previous_review={},
    )
    assert first is not None and first.action == "continue"
    assert first.iteration == 1 and first.feedback
    second = await supervise_goal(
        goal_id=goal_id, run_id=run_id, text=text, checks=checks,
        failed_ids=["artifact_produced"], previous_review={
            "goal_signature": first.goal.get("signature", ""),
            "goal_stall": first.stall_count,
        },
    )
    # 同一缺口重复一次：默认停滞阈值 2，继续；再次重复触发 blocked
    assert second is not None and second.action == "continue"
    third = await supervise_goal(
        goal_id=goal_id, run_id=run_id, text=text, checks=checks,
        failed_ids=["artifact_produced"], previous_review={
            "goal_signature": second.goal.get("signature", ""),
            "goal_stall": second.stall_count,
        },
    )
    assert third is not None and third.action == "blocked"
    assert third.stop_reason == "goal_blocked"

    budget = await supervise_goal(
        goal_id=goal_id, run_id=run_id, text=text, checks=checks,
        failed_ids=["artifact_produced"], previous_review={},
        budget_reason="模型调用次数超过预算(300)",
    )
    assert budget is not None and budget.action == "paused"
    assert budget.stop_reason == "goal_budget"

    passed_checks = {
        **checks,
        "artifact_produced": {"id": "artifact_produced", "passed": True, "detail": "已核对 1 份交付文件"},
    }
    completed = await supervise_goal(
        goal_id=goal_id, run_id=run_id,
        text="已保存 /workspace/report.md。\n\n## 目标核对\n| 报告 | 已完成 | /workspace/report.md |",
        checks=passed_checks, failed_ids=[], previous_review={},
    )
    assert completed is not None and completed.action == "complete"
    assert completed.goal["progress"]["percent"] == 100


@pytest.mark.asyncio(loop_scope="session")
async def test_research_evidence_chain_and_completion_checks(runtime_setup):
    from app.services.goal_runtime.contracts import ensure_goal_contract
    from app.services.research.service import activate_research_plan
    from app.services.research.store import (
        add_claim, add_evidence, add_source, get_plan,
    )
    from app.services.research.verifier import research_checks, verify_claim

    run_id, session_id = _make_run(runtime_setup, title="调研 AI Agent 可观测方案")
    contract = await ensure_goal_contract(
        run_id=run_id,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=session_id,
        objective="调研 AI Agent 可观测方案并给出可核验结论",
        decision={"acceptance_criteria": ["比较至少两种方案"]},
    )
    plan = await activate_research_plan(
        run_id=run_id, goal_id=contract["goal_id"],
        user_id=runtime_setup["user_id"], session_id=session_id,
        objective="调研 AI Agent 可观测方案并给出可核验结论",
        execution_context={"decision": {"intent": "research"}},
    )
    assert plan["questions"]
    research_id = plan["research_id"]

    source = await add_source(
        research_id=research_id, user_id=runtime_setup["user_id"],
        url="https://example.com/observability",
        title="Agent Observability", source_type="docs",
        http_status=200,
        published_at="2020-01-01",
        content="OpenTelemetry traces every agent step. Langfuse records spans and scores.",
    )
    assert source["status"] == "read"
    evidence = await add_evidence(
        research_id=research_id, user_id=runtime_setup["user_id"],
        source_id=source["source_id"],
        quote="OpenTelemetry traces every agent step.",
        locator="Overview / paragraph 1",
        question_id=plan["questions"][0]["id"],
    )
    with pytest.raises(ValueError):
        await add_evidence(
            research_id=research_id, user_id=runtime_setup["user_id"],
            source_id=source["source_id"], quote="Not present in the source body.",
        )

    checks_before = {item["id"]: item for item in await research_checks(
        run_id=run_id, user_id=runtime_setup["user_id"],
    )}
    assert checks_before["research_plan_present"]["passed"] is True
    assert checks_before["research_claims_verified"]["passed"] is False

    claim = await add_claim(
        research_id=research_id, user_id=runtime_setup["user_id"],
        statement="OpenTelemetry 可以追踪 Agent 的每一步执行",
        question_id=plan["questions"][0]["id"],
        support_evidence_ids=[evidence["evidence_id"]],
    )
    verified = await verify_claim(
        research_id=research_id, claim_id=claim["claim_id"],
        user_id=runtime_setup["user_id"],
    )
    assert verified["status"] == "supported"
    assert verified["verification"]["support"][0]["valid"] is True

    unsupported = await add_claim(
        research_id=research_id, user_id=runtime_setup["user_id"],
        statement="Langfuse 是唯一可用的可观测方案",
        contradict_evidence_ids=[evidence["evidence_id"]],
    )
    insufficient = await verify_claim(
        research_id=research_id, claim_id=unsupported["claim_id"],
        user_id=runtime_setup["user_id"],
    )
    assert insufficient["status"] == "insufficient"

    checks_after = {item["id"]: item for item in await research_checks(
        run_id=run_id, user_id=runtime_setup["user_id"],
    )}
    assert checks_after["research_evidence_present"]["passed"] is True
    assert checks_after["research_claims_verified"]["passed"] is False  # 仍有无证据主张
    stored = await get_plan(run_id=run_id, user_id=runtime_setup["user_id"])
    assert stored is not None and stored.revision == 1

    # 用户调整研究计划：revision 递增，已有问题与证据不被删除
    from app.services.research.store import update_plan

    revised_plan = await update_plan(
        research_id=research_id, user_id=runtime_setup["user_id"],
        topic="更新后的研究主题",
        scope={"time_range": "2025-2026"},
        questions=[{"question": "补充问题：成本如何比较？", "priority": "high"}],
        reason="用户调整范围",
    )
    assert revised_plan["revision"] == 2
    assert revised_plan["topic"] == "更新后的研究主题"
    assert revised_plan["scope"]["time_range"] == "2025-2026"
    assert len(revised_plan["questions"]) == len(plan["questions"]) + 1

    # 来源发布时间不在研究范围内：原支持证据失效，核验必须降级为 insufficient
    outdated = await verify_claim(
        research_id=research_id, claim_id=claim["claim_id"],
        user_id=runtime_setup["user_id"],
    )
    assert outdated["status"] == "insufficient"
    reasons = [item["reason"] for item in outdated["verification"]["support"]]
    assert any("时间范围" in reason for reason in reasons)


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_contract_reused_across_runs(runtime_setup):
    """续做必须复用同一 goal_id（历史验收证据与 revision 不丢失）。"""
    from app.services.goal_runtime.contracts import ensure_goal_contract

    first_run, first_session = _make_run(runtime_setup, title="第一个目标运行")
    first = await ensure_goal_contract(
        run_id=first_run,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=first_session,
        objective="持续完成同一目标",
        decision={"acceptance_criteria": ["交付最终结果"]},
    )
    second_run, second_session = _make_run(runtime_setup, title="继续目标")
    second = await ensure_goal_contract(
        run_id=second_run,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=second_session,
        objective="持续完成同一目标",
        decision={"acceptance_criteria": ["交付最终结果"]},
        previous_run_id=first_run,
    )
    assert second["goal_id"] == first["goal_id"]
    assert second["run_id"] == second_run
    assert second["revision"] == first["revision"]
    assert second["status"] == "active"

    # 崩溃恢复：同一 Run 重新进入执行必须复用原契约
    recovered = await ensure_goal_contract(
        run_id=second_run,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=second_session,
        objective="继续",
        decision={},
    )
    assert recovered["goal_id"] == first["goal_id"]
    # 续做请求“继续”不能把原始目标覆盖成“继续”
    assert recovered["objective"] == "持续完成同一目标"


def test_research_activation_requires_goal_and_research_intent():
    from types import SimpleNamespace

    from app.services.research.service import should_activate_research

    research_contract = SimpleNamespace(task_type="research")
    assert should_activate_research(
        goal_mode=True, task_contract=research_contract,
        execution_context={"task_type": "research"},
    ) is True
    assert should_activate_research(
        goal_mode=False, task_contract=research_contract,
        execution_context={"task_type": "research"},
    ) is False
    assert should_activate_research(
        goal_mode=True, task_contract=SimpleNamespace(task_type="qa"),
        execution_context={"task_type": "general_chat", "decision": {"intent": "general_chat"}},
        capability_ids=[],
    ) is False


def test_research_tools_registered_with_risk_profiles():
    from app.services.agent_runtime.tools.classification import (
        CLASS_READ_ONLY, get_side_effect_contract,
    )
    from app.services.agent_runtime.tools.policy import get_tool_risk
    from app.services.tools.research_tools import RESEARCH_TOOL_NAMES

    assert RESEARCH_TOOL_NAMES
    for name in RESEARCH_TOOL_NAMES:
        assert get_tool_risk(name) is not None, f"{name} 未登记风险分类"
    read_source = get_tool_risk("research_read_source")
    assert read_source is not None and read_source.network_access is True
    # 研究工具是平台内部状态/只读读取，不得阻断“继续”续做
    for name in RESEARCH_TOOL_NAMES:
        assert get_side_effect_contract(name).side_effect_class == CLASS_READ_ONLY


def test_goal_can_use_work_orchestrator(monkeypatch):
    """Goal 不再固定跳过 WorkPlan：复合目标按配置允许跨引擎拆分。"""
    from app.core.config import settings
    from app.services.work_orchestrator.complexity import is_composite

    monkeypatch.setattr(settings, "GOAL_ENABLE_WORK_ORCHESTRATOR", True)
    verdict = is_composite(message="先调研市场数据，再生成 Word 报告", goal_mode=True)
    assert verdict.composite is True

    monkeypatch.setattr(settings, "GOAL_ENABLE_WORK_ORCHESTRATOR", False)
    disabled = is_composite(message="先调研市场数据，再生成 Word 报告", goal_mode=True)
    assert disabled.composite is False


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_pause_request_is_cooperative(runtime_setup):
    """用户暂停：CAS 写意图，模型调用边界抛出 GoalPauseRequested。"""
    from datetime import timedelta

    from app.services.agent_runtime.completion import CompletionMiddleware
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.agent_runtime.lifecycle import GoalPauseRequested
    from app.services.agent_runtime.repositories import RunRepository
    from app.services.assistant.task_contract import build_task_contract
    from app.core.database import AsyncSessionLocal

    run_id, session_id = _make_run(runtime_setup, title="可暂停目标")
    with Session(sync_engine) as db:
        row = db.get(AgentRun, run_id)
        row.worker_id = "test-worker"
        row.lease_version = 1
        row.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
        db.commit()

    async with AsyncSessionLocal() as db:
        repo = RunRepository(db)
        assert await repo.request_pause(run_id, runtime_setup["user_id"]) is True
        assert await repo.request_pause(run_id, runtime_setup["user_id"]) is False
        assert await repo.is_pause_requested(run_id) is True
        await db.commit()

    ctx = RuntimeContext(
        RunScope(runtime_setup["user_id"]), run_id, f"thread-{run_id}",
        runtime_setup["agent_id"], session_id=session_id, goal_mode=True,
        task_contract=build_task_contract("继续目标"),
    )
    middleware = CompletionMiddleware(ctx, ctx.task_contract)
    with pytest.raises(GoalPauseRequested):
        await middleware.abefore_model({}, None)


def test_pause_api_marks_run_requested(runtime_setup):
    from datetime import timedelta

    from app.core.security import create_access_token
    from app.main import app

    run_id, _session_id = _make_run(runtime_setup, title="暂停 API")
    with Session(sync_engine) as db:
        row = db.get(AgentRun, run_id)
        row.worker_id = "test-worker"
        row.lease_version = 1
        row.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
        db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(runtime_setup['user_id'])}"}
    with TestClient(app) as client:
        response = client.post(f"/api/v2/runs/{run_id}/pause", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pause_requested"
        again = client.post(f"/api/v2/runs/{run_id}/pause", headers=headers)
        assert again.status_code == 200 and again.json()["idempotent"] is True
        missing = client.post(
            f"/api/v2/runs/{uuid.uuid4().hex}/pause", headers=headers,
        )
        assert missing.status_code == 404
    with Session(sync_engine) as db:
        assert db.get(AgentRun, run_id).pause_requested is True


def test_model_rate_limit_is_recoverable_pause():
    from app.services.agent_runtime.service._common import _is_model_rate_limit_error

    assert _is_model_rate_limit_error(Exception("Error code: 429 - 调用太频繁"))
    assert _is_model_rate_limit_error(Exception("Too Many Requests"))
    assert not _is_model_rate_limit_error(Exception("Error code: 404 - model not found"))


def test_long_source_paging_reads_later_sections():
    from app.services.research.readers.web import page_text

    text = "A" * 5000 + "B" * 5000
    chunk, start, next_offset, has_more = page_text(text, max_chars=4000, offset=0)
    assert len(chunk) == 4000 and start == 0 and has_more is True
    later, start, next_offset, has_more = page_text(text, max_chars=4000, offset=next_offset)
    assert later.startswith("A") and start == 4000
    tail, _start, next_offset, has_more = page_text(text, max_chars=4000, offset=8000)
    assert tail.startswith("B") and len(tail) == 2000 and has_more is False


def test_goal_api_returns_contract_and_revision(runtime_setup):
    from app.core.security import create_access_token
    from app.main import app

    run_id, session_id = _make_run(runtime_setup, title="API 目标")
    goal_id = uuid.uuid4().hex
    criteria = [{
        "id": "AC-001", "description": "交付完整最终结论", "verification_type": "semantic",
        "required": True, "status": "pending", "check_ids": ["answer_present"],
        "evidence_refs": [], "last_checked_at": None, "failure_reason": "",
        "next_step": "", "source": "task_contract",
    }]
    with Session(sync_engine) as db:
        db.add(GoalContract(
            goal_id=goal_id, run_id=run_id, origin_run_id=run_id, session_id=session_id,
            user_id=runtime_setup["user_id"], agent_id=runtime_setup["agent_id"],
            revision=1, objective="交付一份可核验报告",
            acceptance_criteria=criteria, constraints=[], deliverables=[],
            stop_conditions=[], revisions=[], status="active", iterations=0,
            progress={"percent": 0, "required_total": 1, "required_passed": 0,
                      "required_failed": 1, "optional_total": 0, "counts": {}},
        ))
        db.commit()

    headers = {"Authorization": f"Bearer {create_access_token(runtime_setup['user_id'])}"}
    with TestClient(app) as client:
        response = client.get(f"/api/v2/goals/by-run/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["goal_id"] == goal_id
        assert payload["acceptance_criteria"]

        revised = client.post(
            f"/api/v2/goals/{goal_id}/revision", headers=headers,
            json={"objective": "交付一份带来源与冲突说明的可核验报告", "note": "API 修订"},
        )
        assert revised.status_code == 200, revised.text
        assert revised.json()["revision"] == 2

        missing = client.get(
            f"/api/v2/goals/{uuid.uuid4().hex}", headers=headers,
        )
        assert missing.status_code == 404


def test_goal_team_incomplete_is_resumable_pause():
    """Goal + Team 未达成时必须暂停并可续做，不能被当成普通团队失败。"""
    from app.services.agent_runtime.service._common import is_resumable_stop

    assert is_resumable_stop(
        "goal_blocked", goal_mode=True, team_mode=True, private_document=False,
    ) is True
    assert is_resumable_stop(
        "goal_rounds", goal_mode=True, team_mode=True, private_document=False,
    ) is True
    assert is_resumable_stop(
        "goal_work_plan", goal_mode=True, team_mode=False, private_document=False,
    ) is True
    # 非目标团队保持原有语义：团队失败按 failed 收口
    assert is_resumable_stop(
        "budget", goal_mode=False, team_mode=True, private_document=False,
    ) is False
    # 私有文档内容不因目标模式改变文档图收口语义
    assert is_resumable_stop(
        "goal_budget", goal_mode=True, team_mode=False, private_document=True,
    ) is False
    assert is_resumable_stop(
        "unknown", goal_mode=True, team_mode=False, private_document=False,
    ) is False


def test_python_exec_is_local_sandbox_not_external():
    """本地受限代码执行没有外部副作用，不能被步骤验收当作外部操作阻塞。"""
    from app.services.agent_runtime.tools.classification import (
        CLASS_NON_IDEMPOTENT, has_external_side_effect, tool_outcome_class,
    )

    # 恢复语义保持保守（结果未知时仍需核对），但不属于外部副作用
    assert tool_outcome_class("python_exec") == CLASS_NON_IDEMPOTENT
    assert has_external_side_effect("python_exec") is False
    assert has_external_side_effect("webhook_notify") is True
    assert has_external_side_effect("research_read_source") is False


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_contract_includes_steering_before_creation(runtime_setup):
    """契约创建前受理的补充要求必须进入契约，而不是只注入消息。"""
    from app.services.goal_runtime.contracts import ensure_goal_contract

    run_id, session_id = _make_run(runtime_setup, title="创建前补充要求")
    with Session(sync_engine) as db:
        db.add(AgentRunSteering(
            run_id=run_id, user_id=runtime_setup["user_id"],
            message_id=uuid.uuid4().hex, content="必须包含唯一标记 EARLY-1",
        ))
        db.commit()

    contract = await ensure_goal_contract(
        run_id=run_id,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=session_id,
        objective="交付一份说明",
    )
    assert contract["revision"] == 2
    assert any(
        item.get("source") == "steering" and "EARLY-1" in item.get("description", "")
        for item in contract["acceptance_criteria"]
    )
    assert [item["source"] for item in contract["revisions"]] == ["run_created", "steering"]


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_work_plan_completion_records_iteration(runtime_setup):
    """WorkPlan 完成/暂停必须记录执行器级 GoalIteration，保持目标轮次一致。"""
    from app.services.goal_runtime.contracts import ensure_goal_contract
    from app.services.goal_runtime.supervisor import (
        complete_goal_contract, record_goal_iteration,
    )

    run_id, session_id = _make_run(runtime_setup, title="WorkPlan 目标轮次")
    contract = await ensure_goal_contract(
        run_id=run_id,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=session_id,
        objective="完成多步骤计划",
    )
    goal_id = contract["goal_id"]
    await complete_goal_contract(
        goal_id, evidence="work_plan", run_id=run_id,
        plan=[{"id": "step-1", "title": "第一步", "status": "completed"}],
        evidence_refs=["tool:python_exec"],
    )
    with Session(sync_engine) as db:
        row = db.execute(
            select(GoalContract).where(GoalContract.goal_id == goal_id)
        ).scalar_one()
        goal_pk = row.id
        assert row.status == "completed" and row.iterations == 1
        first = db.execute(
            select(GoalIteration).where(GoalIteration.goal_id == goal_id)
        ).scalar_one()
        assert first.executor == "work_plan" and first.status == "completed"
        assert first.plan and first.plan[0]["id"] == "step-1"

    recorded = await record_goal_iteration(
        goal_id=goal_id, run_id=run_id, status="paused", executor="work_plan",
        plan=[{"id": "step-2", "status": "needs_reconciliation"}],
        result={"action": "paused", "stop_reason": "goal_work_plan"},
    )
    assert recorded == 2
    with Session(sync_engine) as db:
        assert db.get(GoalContract, goal_pk).iterations == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_team_supervisor_requires_task_board_completion(runtime_setup):
    """团队任务板未完成 → 目标继续；全部完成且验收通过 → 才允许完成。"""
    from app.services.goal_runtime.contracts import ensure_goal_contract
    from app.services.goal_runtime.supervisor import supervise_goal

    run_id, session_id = _make_run(runtime_setup, title="团队目标组合")
    contract = await ensure_goal_contract(
        run_id=run_id,
        user_id=runtime_setup["user_id"],
        agent_id=runtime_setup["agent_id"],
        session_id=session_id,
        objective="由团队完成两项子任务并交付结论",
    )
    checks = {
        "answer_present": {"passed": True, "detail": ""},
        "plan_finished": {"passed": False, "detail": "团队任务未全部完成（1/2）"},
    }
    pending = await supervise_goal(
        goal_id=contract["goal_id"], run_id=run_id,
        text="团队阶段结论。", checks=checks,
        failed_ids=["plan_finished"], executor="team",
    )
    assert pending is not None and pending.action == "continue"
    assert "团队任务未全部完成" in (pending.feedback or "")

    checks["plan_finished"] = {"passed": True, "detail": ""}
    completed = await supervise_goal(
        goal_id=contract["goal_id"], run_id=run_id,
        text="团队最终结论。", checks=checks,
        failed_ids=[], executor="team",
    )
    assert completed is not None and completed.action == "complete"
    assert completed.goal["progress"]["percent"] == 100


@pytest.mark.asyncio(loop_scope="session")
async def test_research_conflict_requires_disclosure(runtime_setup, monkeypatch):
    """冲突证据必须被识别，且最终报告未披露冲突时阻止完成。"""
    from langchain_core.messages import AIMessage
    from unittest.mock import AsyncMock

    from app.services.agent_runtime import completion as completion_mod
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.assistant.task_contract import build_task_contract
    from app.services.research.service import activate_research_plan
    from app.services.research.store import add_claim, add_evidence, add_source
    from app.services.research.verifier import verify_claim

    run_id, session_id = _make_run(runtime_setup, title="冲突证据研究")
    plan = await activate_research_plan(
        run_id=run_id, goal_id=None, user_id=runtime_setup["user_id"],
        session_id=session_id, objective="比较两种方案的真实支持情况",
        execution_context={"decision": {"intent": "research"}},
    )
    support_source = await add_source(
        research_id=plan["research_id"], user_id=runtime_setup["user_id"],
        url="https://example.com/pro", title="支持观点", source_type="docs",
        http_status=200, content="The benchmark shows option A is faster than option B.",
    )
    contradict_source = await add_source(
        research_id=plan["research_id"], user_id=runtime_setup["user_id"],
        url="https://example.com/con", title="反驳观点", source_type="blog",
        http_status=200, content="A newer benchmark shows option B is faster than option A.",
    )
    support = await add_evidence(
        research_id=plan["research_id"], user_id=runtime_setup["user_id"],
        source_id=support_source["source_id"],
        quote="The benchmark shows option A is faster than option B.",
        locator="Results / table 1",
    )
    contradict = await add_evidence(
        research_id=plan["research_id"], user_id=runtime_setup["user_id"],
        source_id=contradict_source["source_id"],
        quote="A newer benchmark shows option B is faster than option A.",
        locator="Results / table 2",
    )
    claim = await add_claim(
        research_id=plan["research_id"], user_id=runtime_setup["user_id"],
        statement="Option A is faster than option B in the benchmark.",
        support_evidence_ids=[support["evidence_id"]],
        contradict_evidence_ids=[contradict["evidence_id"]],
    )
    verified = await verify_claim(
        research_id=plan["research_id"], claim_id=claim["claim_id"],
        user_id=runtime_setup["user_id"],
    )
    assert verified["status"] == "conflicting"

    ctx = RuntimeContext(
        RunScope(runtime_setup["user_id"]), run_id, f"thread-{run_id}",
        runtime_setup["agent_id"], session_id=session_id,
        task_contract=build_task_contract("比较两种方案"), goal_mode=True,
    )
    ctx.agent_config["research_enabled"] = True
    monkeypatch.setattr(
        completion_mod, "_collect_evidence", AsyncMock(return_value=([], [], False)),
    )
    middleware = completion_mod.CompletionMiddleware(ctx, ctx.task_contract)

    hidden = await middleware.aafter_model({
        "messages": [AIMessage(content="Option A 更快，结论已核验。", id="answer-hidden")],
        "todos": [{"content": "完成比较", "status": "completed"}],
    }, None)
    hidden_checks = {
        item["id"]: item for item in hidden["completion_review"]["report"]["checks"]
    }
    assert hidden_checks["research_conflicts_disclosed"]["passed"] is False

    disclosed = await middleware.aafter_model({
        "messages": [AIMessage(
            content="Option A 与 Option B 的基准结论存在冲突，已披露并保留两种来源。",
            id="answer-disclosed",
        )],
        "todos": [{"content": "完成比较", "status": "completed"}],
    }, None)
    disclosed_checks = {
        item["id"]: item for item in disclosed["completion_review"]["report"]["checks"]
    }
    assert disclosed_checks["research_conflicts_disclosed"]["passed"] is True
