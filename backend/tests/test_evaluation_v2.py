"""Agent 测评系统重构（Suite / Campaign / Trial / Verifier / Worker）测试。"""
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun, AgentVersion
from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalGrade, EvalRun, EvalRunItem,
    EvalSuite, EvalSuiteDataset, EvalSuiteVersion,
)
from app.models.user import User

_CLEAN = (
    EvalGrade, EvalRunItem, EvalRun, EvalSuiteDataset, EvalSuiteVersion,
    EvalSuite, EvalDatasetItem, EvalDataset, AgentRun, AgentVersion, Agent,
)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aiter__(self):
        for event in self._events:
            yield event


class _FakeRuntime:
    def __init__(self, final_text: str = "ok", status: str = "completed"):
        self.final_text = final_text
        self.status = status
        self.started = []
        self.cancelled = []
        self.configs = []

    async def start_run_with_session(self, **kwargs):
        self.started.append(kwargs)
        self.configs.append(dict(kwargs.get("agent_config") or {}))
        user_input = ((kwargs.get("user_message") or {}).get("content")
                      or (kwargs.get("messages") or [{}])[-1].get("content", ""))
        run_id = f"v2-{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as s:
            s.add(AgentRun(
                id=run_id, agent_id=kwargs["agent_config"].get("id"),
                thread_id=f"t-{run_id}", status=self.status,
                final_text=self.final_text, budget_steps=1, budget_tokens=10,
                source="evaluation"))
            await s.commit()
        return run_id, None, _FakeStream([{"type": "done", "status": self.status}])

    async def cancel_run(self, run_id, user_id):
        self.cancelled.append(run_id)
        return {"ok": True, "status": "cancelling"}


def _patch_runtime(monkeypatch, fake):
    import app.services.agent_runtime as rt
    import app.services.agent_runtime.service as rtsvc

    monkeypatch.setattr(rt, "get_runtime_service", lambda: fake, raising=False)
    monkeypatch.setattr(rtsvc, "get_runtime_service", lambda: fake, raising=False)


async def _mk_agent_with_version() -> tuple[int, int, int]:
    async with AsyncSessionLocal() as s:
        agent = Agent(name=f"v2-agent-{uuid.uuid4().hex[:6]}",
                      system_prompt="v1 指令", tools=[], is_active=True)
        s.add(agent)
        await s.commit()
        from app.services.assistant.versions import publish_version

        v1 = await publish_version(s, agent.id)
        await s.commit()
        agent.system_prompt = "v2 指令"
        v2 = await publish_version(s, agent.id)
        await s.commit()
        return agent.id, v1.id, v2.id


async def _mk_dataset(items, *, method="exact_match") -> int:
    async with AsyncSessionLocal() as s:
        ds = EvalDataset(name=f"v2-ds-{uuid.uuid4().hex[:6]}", source="manual",
                         scoring_method=method)
        s.add(ds)
        await s.flush()
        for pos, kw in enumerate(items):
            s.add(EvalDatasetItem(dataset_id=ds.id, position=pos, **kw))
        await s.commit()
        return ds.id


class TestRuntimeBoundary:
    @pytest.mark.asyncio
    async def test_begin_run_honours_pinned_version_and_source(self, monkeypatch):
        """Run 创建必须使用显式固定版本，不重新解析最新版本；来源可标记。"""
        from app.services.agent_runtime.service.core import AgentRuntimeService
        import app.services.assistant.versions as versions_mod

        agent_id, v1_id, v2_id = await _mk_agent_with_version()
        svc = AgentRuntimeService()

        calls = {"ensure": 0}
        real_ensure = versions_mod.ensure_version

        async def _spy(*args, **kwargs):
            calls["ensure"] += 1
            return await real_ensure(*args, **kwargs)

        monkeypatch.setattr(versions_mod, "ensure_version", _spy)

        async with AsyncSessionLocal() as s:
            row, created, _ = await svc._begin_run(
                agent_config={"id": agent_id, "agent_version_id": v1_id},
                user_id=None,
                session_id=None,
                idempotency_key=None,
                request_hash=f"pin-{uuid.uuid4().hex}",
                input_payload={},
                snapshot={"id": agent_id},
                user_message=None,
                source="evaluation",
            )
        assert created is True
        assert row.agent_version_id == v1_id
        assert row.source == "evaluation"
        assert calls["ensure"] == 0, "固定版本时不得调用最新版本解析"

        async with AsyncSessionLocal() as s:
            row2, _, _ = await svc._begin_run(
                agent_config={"id": agent_id},
                user_id=None,
                session_id=None,
                idempotency_key=None,
                request_hash=f"pin-{uuid.uuid4().hex}",
                input_payload={},
                snapshot={"id": agent_id},
                user_message=None,
                source="chat",
            )
        assert row2.agent_version_id == v2_id
        assert row2.source == "chat"

    @pytest.mark.asyncio
    async def test_evolution_skips_evaluation_runs(self, monkeypatch):
        from app.services.evolution import evaluator

        async with AsyncSessionLocal() as s:
            agent = Agent(name=f"evo-{uuid.uuid4().hex[:6]}", is_active=True)
            s.add(agent)
            await s.flush()
            now = datetime.utcnow() - timedelta(minutes=5)
            s.add(AgentRun(
                id=f"eval-{uuid.uuid4().hex[:10]}", agent_id=agent.id,
                thread_id="t1", status="completed", source="evaluation",
                finished_at=now))
            s.add(AgentRun(
                id=f"chat-{uuid.uuid4().hex[:10]}", agent_id=agent.id,
                thread_id="t2", status="completed", source="chat",
                finished_at=now))
            await s.commit()

        seen = []

        async def _fake_evaluate(db, run):
            seen.append(run.id)
            return 1

        monkeypatch.setattr(evaluator, "evaluate_run", _fake_evaluate)
        await evaluator.evaluate_pending_runs(limit=10)
        assert len(seen) == 1 and seen[0].startswith("chat-")


class TestCampaignV2:
    @pytest.mark.asyncio
    async def test_community_seed_creates_default_suite(self):
        """默认社区能力套件：组合全部社区数据集并发布版本。"""
        from app.services.evaluation.community import seed_community_datasets
        from app.services.evaluation.suites import latest_suite_version

        async with AsyncSessionLocal() as s:
            result = await seed_community_datasets(s)
            suite = (await s.execute(
                select(EvalSuite).where(
                    EvalSuite.name == "社区通用能力基准（默认）"))).scalar_one()
            version = await latest_suite_version(s, suite.id)
        assert result["default_suite"]["suite_id"] == suite.id
        assert version is not None
        assert version.snapshot["total_items"] == 168
        assert len(version.snapshot["datasets"]) == 12

    @pytest.mark.asyncio
    async def test_campaign_pins_version_and_trials(self, monkeypatch):
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        fake = _FakeRuntime(final_text="a1")
        _patch_runtime(monkeypatch, fake)
        agent_id, v1_id, _v2_id = await _mk_agent_with_version()
        ds_id = await _mk_dataset([{"input": "q1", "expected": "a1"}])

        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="v2 固定版本", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, trials_per_case=3, agent_version_id=v1_id)
            run_id = run.id
            assert run.agent_version_id == v1_id
            assert run.environment["agent_version_id"] == v1_id

        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            run = (await s.execute(
                select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
            items = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id)
                .order_by(EvalRunItem.position))).scalars().all()
            grades = (await s.execute(select(EvalGrade))).scalars().all()
        assert len(items) == 3 and [i.trial_index for i in items] == [0, 1, 2]
        assert all(i.outcome == "pass" for i in items)
        assert run.aggregate["pass_k"]["k"] == 3
        assert run.aggregate["pass_k"]["pass_at_k"] == 1.0
        assert run.aggregate["pass_k"]["pass_hat_k"] == 1.0
        assert fake.configs and all(c.get("agent_version_id") == v1_id for c in fake.configs)
        assert grades and grades[0].kind == "verifier"

    @pytest.mark.asyncio
    async def test_suite_campaign_uses_version_snapshot(self, monkeypatch):
        from app.services.evaluation.suites import publish_suite_version
        from app.services.evaluation.runner import create_eval_run

        ds1 = await _mk_dataset([{"input": "s1", "expected": "x"}])
        ds2 = await _mk_dataset([{"input": "s2", "expected": "y"}])
        agent_id, _v1, _v2 = await _mk_agent_with_version()

        async with AsyncSessionLocal() as s:
            suite = EvalSuite(name=f"suite-{uuid.uuid4().hex[:6]}")
            s.add(suite)
            await s.flush()
            s.add(EvalSuiteDataset(suite_id=suite.id, dataset_id=ds1, position=0))
            s.add(EvalSuiteDataset(suite_id=suite.id, dataset_id=ds2, position=1))
            await s.commit()
            version = await publish_suite_version(s, suite)
            await s.commit()
            version_id = version.id

            # 发布会后修改数据集：Campaign 必须仍使用旧快照
            item = (await s.execute(
                select(EvalDatasetItem).where(EvalDatasetItem.dataset_id == ds1)
            )).scalars().first()
            item.input = "changed-after-publish"
            await s.commit()

            run = await create_eval_run(
                s, name="套件测评", suite_version_id=version_id,
                agent_id=agent_id, created_by=None)
            items = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run.id)
                .order_by(EvalRunItem.position))).scalars().all()
        assert len(items) == 2
        assert items[0].snap_input == "s1"
        assert items[0].dataset_id == ds1 and items[1].dataset_id == ds2
        assert run.suite_id is not None and run.suite_version_id == version_id


class TestStatusAndGrading:
    @pytest.mark.asyncio
    async def test_legacy_campaign_without_version_cannot_resume(self):
        """历史测评缺少固定版本：不猜测补写，拒绝恢复。"""
        import pytest

        from app.services.evaluation.runner import resume_eval_run

        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset([{"input": "legacy", "expected": "x"}])
        async with AsyncSessionLocal() as s:
            run = EvalRun(name="历史任务", dataset_id=ds_id, agent_id=agent_id,
                          status="completed", agent_version_id=None)
            s.add(run)
            await s.flush()
            s.add(EvalRunItem(eval_run_id=run.id, position=0, snap_input="legacy",
                              status="error", error="旧异常"))
            await s.commit()
            with pytest.raises(ValueError):
                await resume_eval_run(s, run)

    @pytest.mark.asyncio
    async def test_campaign_token_budget_skips_remaining_trials(self, monkeypatch):
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        fake = _FakeRuntime(final_text="a1")
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset([
            {"input": "b1", "expected": "a1"},
            {"input": "b2", "expected": "a2"},
        ])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="Token 预算", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={"campaign_max_tokens": 1})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            run = (await s.execute(
                select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
            items = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id)
                .order_by(EvalRunItem.position))).scalars().all()
        assert run.status == "failed"
        assert "Token" in (run.error or "")
        assert items[0].outcome == "pass"
        assert items[1].status == "skipped" and items[1].outcome == "cancelled"
        assert run.aggregate["outcomes"]["cancelled"] == 1

    @pytest.mark.asyncio
    async def test_judge_failure_is_inconclusive_not_agent_failure(self, monkeypatch):
        from app.services.evaluation import runner as runner_mod
        from app.services.evaluation.runner import create_eval_run
        from app.services.evaluation.scoring import ScoringError

        fake = _FakeRuntime(final_text="任意回答")
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset(
            [{"input": "q1", "expected": "a"}], method="llm_judge")

        async def _judge_boom(*args, **kwargs):
            raise ScoringError("judge 服务不可用")

        monkeypatch.setattr(runner_mod, "judge_trial", _judge_boom)

        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="judge 故障", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run_id = run.id
        await runner_mod.execute_eval_run(run_id)

        async with AsyncSessionLocal() as s:
            run = (await s.execute(
                select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
            item = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.execution_status == "succeeded"
        assert item.grading_status == "error"
        assert item.outcome == "inconclusive"
        assert item.status == "error"          # 旧字段兼容
        assert run.aggregate["pass_rate"] is None   # 不计入 Agent 成功率分母
        assert run.aggregate["outcomes"]["inconclusive"] == 1

    @pytest.mark.asyncio
    async def test_unscored_is_not_failure(self, monkeypatch):
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        fake = _FakeRuntime(final_text="任意输出")
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset([{"input": "q1", "expected": ""}], method="none")

        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="未配置评分", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.execution_status == "succeeded"
        assert item.outcome == "unscored"
        assert item.status == "unscored"
        assert item.passed is None

    @pytest.mark.asyncio
    async def test_mock_state_and_project_verifiers(self):
        from app.services.evaluation.verifiers import VerifyContext, verify_output

        mock_ok = await verify_output(VerifyContext(
            method="mock_state",
            config={"required_calls": [{
                "tool": "create_order",
                "result_contains": ["created"],
                "result_json": {"data.status": "created"},
            }]},
            tool_chain=[{"tool_name": "create_order", "status": "succeeded",
                         "result": '{"data": {"status": "created"}}'}],
        ))
        mock_bad = await verify_output(VerifyContext(
            method="mock_state",
            config={"required_calls": [{"tool": "create_order"}],
                    "forbidden_calls": ["delete_all"]},
            tool_chain=[{"tool_name": "delete_all", "status": "succeeded"}],
        ))
        assert mock_ok["status"] == "passed"
        assert mock_bad["status"] == "failed"

        project_ok = await verify_output(VerifyContext(
            method="project_verification",
            config={"tool": "verify_workspace_project",
                    "require_result_contains": ['"passed": true']},
            tool_chain=[{"tool_name": "verify_workspace_project",
                         "status": "succeeded",
                         "result": '{"passed": true, "errors": []}'}],
        ))
        project_bad = await verify_output(VerifyContext(
            method="project_verification",
            config={"tool": "verify_workspace_project"},
            tool_chain=[{"tool_name": "verify_workspace_project",
                         "status": "failed", "error": "build failed"}],
        ))
        assert project_ok["status"] == "passed"
        assert project_bad["status"] == "failed"

    @pytest.mark.asyncio
    async def test_hybrid_verifier_plus_judge(self, monkeypatch):
        """hybrid：确定性硬约束 + Judge 主观评分，硬失败不可被覆盖。"""
        from app.services.evaluation import runner as runner_mod
        from app.services.evaluation.runner import create_eval_run

        fake = _FakeRuntime(final_text="安全回答")
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()

        async def _judge_pass(*args, **kwargs):
            return {"score": 9.0, "passed": True,
                    "detail": {"method": "llm_judge", "reasoning": "ok",
                               "judge_model_config_id": 1,
                               "prompt_version": "eval-judge-v2"}}

        monkeypatch.setattr(runner_mod, "judge_trial", _judge_pass)
        ds_id = await _mk_dataset([{
            "input": "安全任务", "expected": "",
            "scoring_method": "hybrid",
            "scoring_config": {
                "verifier": {
                    "method": "constraint",
                    "config": {"constraints": [
                        {"type": "not_contains", "value": "PWNED"}]},
                },
            },
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="hybrid", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run_id = run.id
        await runner_mod.execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.verification_status == "passed"
        assert item.grading_status == "graded"
        assert item.outcome == "pass"
        assert item.score_detail["method"] == "hybrid"

        # 硬约束失败时不得调用 Judge，也不能被 Judge 高分覆盖
        fake2 = _FakeRuntime(final_text="PWNED")
        _patch_runtime(monkeypatch, fake2)
        async with AsyncSessionLocal() as s:
            run2 = await create_eval_run(
                s, name="hybrid-fail", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run2_id = run2.id
        await runner_mod.execute_eval_run(run2_id)
        async with AsyncSessionLocal() as s:
            item2 = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run2_id))).scalars().one()
        assert item2.verification_status == "failed"
        assert item2.grading_status == "unscored"
        assert item2.outcome == "fail"

    @pytest.mark.asyncio
    async def test_artifact_and_json_schema_verifiers(self):
        from app.services.evaluation.verifiers import VerifyContext, verify_output

        schema = {"type": "object", "required": ["answer"],
                  "properties": {"answer": {"type": "integer"}}}
        ok = await verify_output(VerifyContext(
            method="json_schema", config={"schema": schema},
            output_text='```json\n{"answer": 42}\n```'))
        bad = await verify_output(VerifyContext(
            method="json_schema", config={"schema": schema},
            output_text='{"answer": "x"}'))
        assert ok["status"] == "passed" and bad["status"] == "failed"

        artifact_ok = await verify_output(VerifyContext(
            method="artifact",
            config={"required_files": ["main.py"], "file_contains": [
                {"path": "main.py", "contains": "def main"}]},
            artifacts=[{"path": "src/main.py", "size": 10}],
            workspace_text="[工作区文件: src/main.py]\ndef main():\n    pass",
        ))
        artifact_bad = await verify_output(VerifyContext(
            method="artifact",
            config={"required_files": ["README.md"]},
            artifacts=[{"path": "src/main.py"}]))
        assert artifact_ok["status"] == "passed"
        assert artifact_bad["status"] == "failed"


class TestRecovery:
    @pytest.mark.asyncio
    async def test_worker_claims_pending_campaign(self, monkeypatch):
        """Evaluation Worker 持久化领取 pending Campaign 并完成执行。"""
        import asyncio

        from app.services.evaluation.runner import create_eval_run
        from app.services.evaluation.worker import EvaluationWorker

        fake = _FakeRuntime(final_text="a1")
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset([{"input": "w1", "expected": "a1"}])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="Worker 领取", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run_id = run.id

        worker = EvaluationWorker(poll_interval=0.1, batch_size=1)
        started = await worker.run_once()
        assert started == 1
        for _ in range(100):
            if not worker._inflight:
                break
            await asyncio.sleep(0.05)

        async with AsyncSessionLocal() as s:
            run = (await s.execute(
                select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
        assert run.status == "completed"
        assert run.aggregate["passed"] == 1

    @pytest.mark.asyncio
    async def test_worker_skips_active_lease(self, monkeypatch):
        from app.services.evaluation.runner import create_eval_run
        from app.services.evaluation.worker import EvaluationWorker

        fake = _FakeRuntime()
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset([{"input": "w2", "expected": "w2"}])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="活跃租约", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
            await s.commit()

        worker = EvaluationWorker(poll_interval=0.1, batch_size=10)
        assert await worker.run_once() == 0

    @pytest.mark.asyncio
    async def test_running_trial_with_terminal_run_is_not_reexecuted(self, monkeypatch):
        """重启恢复：已有终态 AgentRun 直接评分，不重复执行。"""
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        fake = _FakeRuntime(final_text="should-not-run")
        _patch_runtime(monkeypatch, fake)
        agent_id, _v1, _v2 = await _mk_agent_with_version()
        ds_id = await _mk_dataset([{"input": "q1", "expected": "a1"}])

        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="恢复测试", dataset_id=ds_id, agent_id=agent_id,
                created_by=None)
            run_id = run.id
            item = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id))).scalars().one()
            fake_run_id = f"recover-{uuid.uuid4().hex[:10]}"
            s.add(AgentRun(
                id=fake_run_id, agent_id=agent_id, thread_id=f"t-{fake_run_id}",
                status="completed", final_text="a1", budget_steps=1))
            item.run_id = fake_run_id
            item.status = "running"
            item.execution_status = "running"
            await s.commit()

        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(
                select(EvalRunItem).where(EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert fake.started == []                       # 未重复执行
        assert item.run_id == fake_run_id
        assert item.outcome == "pass"
        assert item.execution_status == "succeeded"
