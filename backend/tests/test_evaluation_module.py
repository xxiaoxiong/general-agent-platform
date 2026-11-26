"""Agent 测评模块：评分器 / 社区数据集 / 运行器 / API（/api/v2/evaluation）。

运行：
    pytest backend/tests/test_evaluation_module.py -q
"""
import asyncio
import os
from threading import Event
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentToolExecution
from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalGrade, EvalRun, EvalRunItem,
    EvalSuite, EvalSuiteDataset, EvalSuiteVersion,
)

_CLEAN = (EvalGrade, EvalRunItem, EvalRun, EvalSuiteDataset, EvalSuiteVersion,
          EvalSuite, EvalDatasetItem, EvalDataset,
          AgentToolExecution, AgentRunEvent, AgentRun, Agent)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


# ────────────────────────────── 评分器 ──────────────────────────────


class TestScoringBasics:
    def test_normalize_text(self):
        from app.services.evaluation.scoring import normalize_text

        assert normalize_text("  Hello   World  ") == "hello world"
        assert normalize_text("42。") == "42"
        assert normalize_text('"ABC"') == "abc"

    @pytest.mark.asyncio
    async def test_exact_match(self):
        from app.services.evaluation.scoring import score_output

        async with AsyncSessionLocal() as s:
            ok = await score_output(
                s, method="exact_match", config={},
                input_text="q", expected=" 答案A。 ", criteria="",
                actual_output="答案A")
            assert ok["passed"] is True and ok["score"] == 10.0
            bad = await score_output(
                s, method="exact_match", config={},
                input_text="q", expected="答案A", criteria="",
                actual_output="答案B")
            assert bad["passed"] is False and bad["score"] == 0.0

    @pytest.mark.asyncio
    async def test_contains(self):
        from app.services.evaluation.scoring import score_output

        async with AsyncSessionLocal() as s:
            ok = await score_output(
                s, method="contains", config={},
                input_text="q", expected="最终答案 42", criteria="",
                actual_output="推理……最终答案 42 完成")
            assert ok["passed"] is True
            kw = await score_output(
                s, method="contains", config={"keywords": ["巴黎", "伦敦"]},
                input_text="q", expected="", criteria="",
                actual_output="首都是巴黎")
            assert kw["passed"] is True and kw["detail"]["hit_keywords"] == ["巴黎"]

    @pytest.mark.asyncio
    async def test_keyword_all_any(self):
        from app.services.evaluation.scoring import score_output

        async with AsyncSessionLocal() as s:
            all_mode = await score_output(
                s, method="keyword", config={"keywords": ["a", "b"], "match": "all"},
                input_text="q", expected="", criteria="", actual_output="a b c")
            assert all_mode["passed"] is True
            all_fail = await score_output(
                s, method="keyword", config={"keywords": ["a", "z"], "match": "all"},
                input_text="q", expected="", criteria="", actual_output="a b c")
            assert all_fail["passed"] is False
            any_mode = await score_output(
                s, method="keyword", config={"keywords": ["z", "b"], "match": "any"},
                input_text="q", expected="", criteria="", actual_output="a b c")
            assert any_mode["passed"] is True

    @pytest.mark.asyncio
    async def test_regex(self):
        from app.services.evaluation.scoring import ScoringError, score_output

        async with AsyncSessionLocal() as s:
            ok = await score_output(
                s, method="regex", config={"pattern": r"最终答案[:：]\s*42"},
                input_text="q", expected="", criteria="",
                actual_output="最终答案：42")
            assert ok["passed"] is True
            with pytest.raises(ScoringError):
                await score_output(
                    s, method="regex", config={"pattern": "["},
                    input_text="q", expected="", criteria="", actual_output="x")

    @pytest.mark.asyncio
    async def test_none_method(self):
        from app.services.evaluation.scoring import score_output

        async with AsyncSessionLocal() as s:
            r = await score_output(
                s, method="none", config={}, input_text="q",
                expected="", criteria="", actual_output="任意输出")
            assert r["score"] is None and r["passed"] is None

    @pytest.mark.asyncio
    async def test_constraint_scoring(self):
        """客观约束清单校验（长程任务评分）：确定性、无 LLM。"""
        from app.services.evaluation.scoring import ScoringError, score_output

        doc = "## 需求分析\n" + "内容" * 200 + "\n## 架构设计\n蓝绿部署\n## 部署方案\n"
        config = {
            "constraints": [
                {"type": "min_headings", "value": 3},
                {"type": "contains", "value": "蓝绿部署"},
                {"type": "not_contains", "value": "TODO"},
                {"type": "min_length", "value": 100},
            ],
            "pass_mode": "all",
        }
        async with AsyncSessionLocal() as s:
            ok = await score_output(
                s, method="constraint", config=config,
                input_text="q", expected="", criteria="", actual_output=doc)
            assert ok["passed"] is True and ok["score"] == 10.0
            assert ok["detail"]["passed_constraints"] == 4

            bad_config = {"constraints": [
                {"type": "contains", "value": "回滚预案"},
                {"type": "min_headings", "value": 3},
            ]}
            bad = await score_output(
                s, method="constraint", config=bad_config,
                input_text="q", expected="", criteria="", actual_output=doc)
            assert bad["passed"] is False
            assert bad["score"] == 5.0
            assert bad["detail"]["checks"][0]["passed"] is False

            # threshold 模式：比例达标即通过
            thr = await score_output(
                s, method="constraint",
                config={**bad_config, "pass_mode": "threshold", "min_ratio": 0.5},
                input_text="q", expected="", criteria="", actual_output=doc)
            assert thr["passed"] is True

            with pytest.raises(ScoringError):
                await score_output(
                    s, method="constraint", config={"constraints": []},
                    input_text="q", expected="", criteria="", actual_output=doc)

    @pytest.mark.asyncio
    async def test_constraint_answer_extract(self):
        from app.services.evaluation.scoring import score_output

        out = "推导过程……\n最终答案：42"
        config = {"constraints": [
            {"type": "answer", "values": ["42"],
             "pattern": r"最终答案[:：]\s*([\d./-]+)"},
            {"type": "min_length", "value": 5},
        ]}
        async with AsyncSessionLocal() as s:
            ok = await score_output(
                s, method="constraint", config=config,
                input_text="q", expected="42", criteria="", actual_output=out)
            assert ok["passed"] is True
            wrong = await score_output(
                s, method="constraint",
                config={"constraints": [
                    {"type": "answer", "values": ["43"],
                     "pattern": r"最终答案[:：]\s*([\d./-]+)"}]},
                input_text="q", expected="43", criteria="", actual_output=out)
            assert wrong["passed"] is False

    @pytest.mark.asyncio
    async def test_constraint_length_scope_separates_output_and_artifacts(self):
        """长度约束口径：min_length 测产出规模（回答+交付物），max_length 仅测最终回答。

        防止交付物规模扭曲测量：全写文件、正文简短的实干型交付不被 min_length
        误判过短，大交付物也不被 max_length 误判回答冗长。
        """
        from app.services.evaluation.scoring import score_output

        cfg = {"constraints": [
            {"type": "min_length", "value": 1000},
            {"type": "max_length", "value": 1000},
        ]}
        async with AsyncSessionLocal() as s:
            # 交付物 3000 + 回答 200：min 按合并文本 3200 通过，max 按回答 200 通过
            r = await score_output(
                s, method="constraint", config=cfg,
                input_text="q", expected="", criteria="",
                actual_output="x" * 3000 + "y" * 200, output_text="y" * 200)
            assert r["passed"] is True
            assert r["detail"]["checks"][0]["passed"] is True   # min_length 合并 3200
            assert r["detail"]["checks"][1]["passed"] is True   # max_length 仅回答 200

            # 回答本身超长：max 仅看回答，如实判失败
            r2 = await score_output(
                s, method="constraint", config=cfg,
                input_text="q", expected="", criteria="",
                actual_output="y" * 5000, output_text="y" * 5000)
            assert r2["passed"] is False
            assert r2["detail"]["checks"][1]["passed"] is False

            # 不传 output_text（无交付物并入）：max 回落到评分文本本身
            r3 = await score_output(
                s, method="constraint", config=cfg,
                input_text="q", expected="", criteria="",
                actual_output="y" * 5000)
            assert r3["detail"]["checks"][1]["passed"] is False

    @pytest.mark.asyncio
    async def test_llm_judge(self, monkeypatch):
        from app.services.evaluation import scoring as sp
        from app.services.model_service import ModelService

        async def _fake_chat(provider, mc, request):
            return {"content": '{"score": 8.5, "passed": true, "reasoning": "语义正确"}'}

        async def _fake_pick(db):
            return object(), object(), 77

        monkeypatch.setattr(ModelService, "chat", staticmethod(_fake_chat))
        monkeypatch.setattr(sp, "_pick_judge_model", _fake_pick)
        async with AsyncSessionLocal() as s:
            r = await sp.score_output(
                s, method="llm_judge", config={},
                input_text="任务", expected="期望", criteria="标准",
                actual_output="实际")
        assert r["score"] == 8.5
        assert r["passed"] is True
        assert r["detail"]["reasoning"] == "语义正确"
        assert r["detail"]["judge_model_config_id"] == 77

    @pytest.mark.asyncio
    async def test_llm_judge_parse_error(self, monkeypatch):
        from app.services.evaluation import scoring as sp
        from app.services.model_service import ModelService

        async def _bad_chat(provider, mc, request):
            return {"content": "这不是 JSON"}

        async def _fake_pick(db):
            return object(), object(), 1

        monkeypatch.setattr(ModelService, "chat", staticmethod(_bad_chat))
        monkeypatch.setattr(sp, "_pick_judge_model", _fake_pick)
        async with AsyncSessionLocal() as s:
            with pytest.raises(sp.ScoringError):
                await sp.score_output(
                    s, method="llm_judge", config={},
                    input_text="q", expected="", criteria="", actual_output="a")


# ────────────────────────────── 社区数据集 ──────────────────────────────


class TestCommunityDatasets:
    def test_load_builtin(self):
        from app.services.evaluation.community import load_community_datasets

        defs = load_community_datasets()
        assert len(defs) >= 5
        names = [d["name"] for d in defs]
        assert any("GSM8K" in n for n in names)
        assert any("BFCL" in n for n in names)
        # 复杂任务数据集（多步推理 / 工具链 / 多轮 / 对抗）
        assert any("BBH" in n for n in names)
        assert any("工具链" in n for n in names)
        assert any("多轮" in n for n in names)
        # 长程复杂任务数据集（工程构建 / 长文档 / 复杂数学 / 深度研究）
        assert any("工程项目构建" in n for n in names)
        assert any("长文档" in n for n in names)
        assert any("复杂数学" in n for n in names)
        assert any("深度研究" in n for n in names)
        for d in defs:
            assert len(d["items"]) >= 6
            assert d.get("reference"), "社区数据集必须注明出处"
            for it in d["items"]:
                assert it["input"]
                # 复杂任务维度标注（客观对照）
                assert it["complexity"] in ("L1", "L2", "L3", "L4")
                assert it["task_type"] in (
                    "direct_qa", "multi_step_reasoning", "tool_use",
                    "instruction_following", "multi_turn", "adversarial",
                    "project_build", "long_document", "complex_math",
                    "deep_research")
                assert isinstance(it.get("turns", []), list)
                assert isinstance(it.get("required_tools", []), list)
                # constraint 评分用例必须带客观约束清单
                if it.get("scoring_method") == "constraint":
                    cons = (it.get("scoring_config") or {}).get("constraints") or []
                    assert cons, f"{d['name']} constraint 用例缺少约束清单"

    @pytest.mark.asyncio
    async def test_seed_idempotent(self):
        from app.services.evaluation.community import (
            load_community_datasets, seed_community_datasets,
        )

        n = len(load_community_datasets())
        async with AsyncSessionLocal() as s:
            r1 = await seed_community_datasets(s)
            r2 = await seed_community_datasets(s)
        assert len(r1["created"]) == n and r1["updated"] == []
        # 已存在的 community 数据集会被整体刷新（升级基准版本）
        assert r2["created"] == [] and len(r2["updated"]) == n
        async with AsyncSessionLocal() as s:
            total = len((await s.execute(
                select(EvalDataset))).scalars().all())
        assert total == n

    @pytest.mark.asyncio
    async def test_seed_skips_manual_dataset(self):
        from app.services.evaluation.community import (
            load_community_datasets, seed_community_datasets,
        )

        name = load_community_datasets()[0]["name"]
        async with AsyncSessionLocal() as s:
            s.add(EvalDataset(name=name, source="manual", scoring_method="none"))
            await s.commit()
            r = await seed_community_datasets(s)
        assert name in r["skipped"] and name not in r["created"] and name not in r["updated"]

    @pytest.mark.asyncio
    async def test_refresh_releases_item_references(self):
        """刷新社区数据集时，历史结果行解除用例外键引用但保留完整快照。"""
        from app.services.evaluation.community import (
            load_community_datasets, seed_community_datasets,
        )
        from app.services.evaluation.runner import create_eval_run

        name = load_community_datasets()[0]["name"]
        async with AsyncSessionLocal() as s:
            await seed_community_datasets(s)
            ds = (await s.execute(
                select(EvalDataset).where(EvalDataset.name == name))).scalars().one()
            agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}", system_prompt="t",
                          tools=[], is_active=True)
            s.add(agent)
            await s.commit()
            run = await create_eval_run(
                s, name="刷新引用测试", dataset_id=ds.id, agent_id=agent.id,
                created_by=None, config={"limit": 2})
            run_id = run.id

        async with AsyncSessionLocal() as s:
            r = await seed_community_datasets(s)   # 触发整体替换路径
        assert name in r["updated"]

        async with AsyncSessionLocal() as s:
            items = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().all()
        assert len(items) == 2
        assert all(i.dataset_item_id is None for i in items)   # 外键已解除
        assert all(i.snap_input for i in items)                # 快照仍完整


# ────────────────────────────── 运行器 ──────────────────────────────


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aiter__(self):
        for e in self._events:
            yield e


class _FakeRuntime:
    """假 Runtime：起 Run 时直接落 AgentRun 行并返回即完成的事件流。

    - final_texts: 用户消息 → final_text（按当前轮 user_message 匹配）；
    - tool_calls: 用户消息 → [工具名 | (工具名, args)]，落 AgentToolExecution 事实行
      （可表征工具使用；带 args 时写入 args_snapshot，供交付物采集）；
    - steps: 每 Run 的 budget_steps（默认 1）。
    """

    def __init__(self, final_texts=None, error: Exception | None = None,
                 tool_calls=None, steps: int = 1, statuses=None):
        self.final_texts = final_texts or {}   # input → final_text
        self.tool_calls = tool_calls or {}     # input → [tool_name]
        self.steps = steps
        self.error = error
        self.statuses = statuses or {}
        self.started = []
        self.cancelled = []

    async def start_run_with_session(self, *, agent_config, messages,
                                     user_id, session_id=None,
                                     idempotency_key=None, use_memory_context="",
                                     db_info=None, endpoint="chat",
                                     user_message=None, **kw):
        if self.error is not None:
            raise self.error
        user_input = ((user_message or {}).get("content")
                      or (messages[-1] or {}).get("content", ""))
        final_text = self.final_texts.get(user_input, user_input)
        status = self.statuses.get(user_input, "completed")
        run_id = f"evalfake-{uuid.uuid4().hex[:12]}"
        self.started.append(run_id)
        tools = self.tool_calls.get(user_input, [])
        async with AsyncSessionLocal() as s:
            s.add(AgentRun(
                id=run_id, agent_id=agent_config.get("id"), thread_id=f"t-{run_id}",
                status=status, final_text=final_text,
                budget_steps=self.steps, budget_model_calls=self.steps,
                budget_tool_calls=len(tools), budget_tokens=10))
            for i, t in enumerate(tools):
                name, args, *tool_status = (t, None) if isinstance(t, str) else t
                s.add(AgentToolExecution(
                    run_id=run_id, tool_call_id=f"call-{i}", tool_name=name,
                    status=tool_status[0] if tool_status else "succeeded", attempt_count=1,
                    args_snapshot=dict(args) if args else None))
            await s.commit()
        return run_id, None, _FakeStream([{"type": "done", "status": status}])

    async def cancel_run(self, run_id, user_id):
        self.cancelled.append(run_id)
        return {"ok": True, "status": "cancelling"}


def _fake_runtime_module(monkeypatch, fake: _FakeRuntime):
    import app.services.agent_runtime as rt

    monkeypatch.setattr(rt, "get_runtime_service", lambda: fake, raising=False)
    import app.services.agent_runtime.service as rtsvc
    monkeypatch.setattr(rtsvc, "get_runtime_service", lambda: fake, raising=False)


async def _mk_dataset_and_agent(items) -> tuple:
    """items: [(input, expected, scoring_method)]"""
    async with AsyncSessionLocal() as s:
        ds = EvalDataset(name=f"ds-{uuid.uuid4().hex[:8]}", source="manual",
                         scoring_method="exact_match")
        s.add(ds)
        await s.flush()
        for pos, (inp, exp, method) in enumerate(items):
            s.add(EvalDatasetItem(
                dataset_id=ds.id, position=pos, input=inp, expected=exp,
                scoring_method=method))
        agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}",
                      system_prompt="测评测试", tools=[], is_active=True)
        s.add(agent)
        await s.commit()
        return ds.id, agent.id


async def _mk_complex_dataset_and_agent(items: list) -> tuple:
    """items: EvalDatasetItem 构造 kwargs 列表（复杂任务维度）。"""
    async with AsyncSessionLocal() as s:
        ds = EvalDataset(name=f"ds-{uuid.uuid4().hex[:8]}", source="manual",
                         scoring_method="exact_match")
        s.add(ds)
        await s.flush()
        for pos, kw in enumerate(items):
            s.add(EvalDatasetItem(dataset_id=ds.id, position=pos, **kw))
        agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}",
                      system_prompt="测评测试", tools=[], is_active=True)
        s.add(agent)
        await s.commit()
        return ds.id, agent.id


class TestRunner:
    @pytest.mark.asyncio
    async def test_create_run_snapshots_and_limit(self):
        from app.services.evaluation.runner import create_eval_run

        ds_id, agent_id = await _mk_dataset_and_agent(
            [("q1", "a1", None), ("q2", "a2", None), ("q3", "a3", None)])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="快照测试", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={"limit": 2})
            items = (await s.execute(
                select(EvalRunItem).where(
                    EvalRunItem.eval_run_id == run.id)
                .order_by(EvalRunItem.position))).scalars().all()
        assert run.status == "pending"
        assert len(items) == 2
        assert items[0].snap_input == "q1" and items[0].snap_expected == "a1"
        assert items[0].snap_scoring_method == "exact_match"
        assert run.aggregate["total"] == 2

    @pytest.mark.asyncio
    async def test_execute_pass_fail_aggregate(self, monkeypatch):
        from app.services.evaluation.runner import (
            create_eval_run, execute_eval_run,
        )

        fake = _FakeRuntime(final_texts={"q1": "a1", "q2": "X", "q3": "a3"})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_dataset_and_agent(
            [("q1", "a1", None), ("q2", "a2", None), ("q3", "a3", None)])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="执行测试", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            run = (await s.execute(select(EvalRun).where(
                EvalRun.id == run_id))).scalar_one()
            items = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id)
                .order_by(EvalRunItem.position))).scalars().all()
        assert run.status == "completed"
        assert [i.status for i in items] == ["passed", "failed", "passed"]
        assert all(i.run_id and i.run_id.startswith("evalfake-") for i in items)
        agg = run.aggregate
        assert agg["total"] == 3 and agg["passed"] == 2 and agg["failed"] == 1
        assert agg["pass_rate"] == round(2 / 3, 4)
        assert agg["avg_score"] == round((10 + 0 + 10) / 3, 3)

    @pytest.mark.asyncio
    async def test_execute_error_item(self, monkeypatch):
        from app.services.evaluation.runner import (
            create_eval_run, execute_eval_run,
        )

        fake = _FakeRuntime(error=RuntimeError("模型调用失败"))
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_dataset_and_agent([("q1", "a1", None)])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="异常测试", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            run = (await s.execute(select(EvalRun).where(
                EvalRun.id == run_id))).scalar_one()
            item = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert run.status == "completed"
        assert item.status == "error"
        assert "模型调用失败" in (item.error or "")

    @pytest.mark.asyncio
    async def test_cancel_and_resume(self, monkeypatch):
        from app.services.evaluation.runner import (
            cancel_eval_run, create_eval_run, execute_eval_run, resume_eval_run,
        )

        ds_id, agent_id = await _mk_dataset_and_agent(
            [("q1", "a1", None), ("q2", "a2", None)])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="取消测试", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
            run = (await s.execute(select(EvalRun).where(
                EvalRun.id == run_id))).scalar_one()
            r = await cancel_eval_run(s, run)
        assert r["status"] == "cancelled"
        async with AsyncSessionLocal() as s:
            items = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().all()
        assert [i.status for i in items] == ["skipped", "skipped"]

        # 恢复：skipped 重置为 pending 并可重新执行
        fake = _FakeRuntime()
        _fake_runtime_module(monkeypatch, fake)
        async with AsyncSessionLocal() as s:
            run = (await s.execute(select(EvalRun).where(
                EvalRun.id == run_id))).scalar_one()
            await resume_eval_run(s, run)
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            run = (await s.execute(select(EvalRun).where(
                EvalRun.id == run_id))).scalar_one()
        assert run.status == "completed"
        assert run.aggregate["completed"] == 2


class TestComplexityAndCapability:
    """复杂任务体系：多轮执行 / 工具能力客观校验 / 聚合分层。"""

    @pytest.mark.asyncio
    async def test_multi_turn_execution(self, monkeypatch):
        from app.services.evaluation.runner import (
            create_eval_run, execute_eval_run,
        )

        fake = _FakeRuntime(final_texts={"第一轮问题": "第一轮回答",
                                         "第二轮追问": "最终答案 42"})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "第一轮问题", "expected": "最终答案 42",
            "complexity": "L4", "task_type": "multi_turn",
            "turns": ["第二轮追问"],
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="多轮测试", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.status == "passed"           # 评分针对最终轮输出
        assert item.snap_complexity == "L4" and item.snap_task_type == "multi_turn"
        turns = item.stats["turns"]
        assert len(turns) == 2
        assert turns[0]["output"] == "第一轮回答" and turns[1]["output"] == "最终答案 42"
        assert turns[0]["run_id"] != turns[1]["run_id"]   # 每轮独立 Run（可追溯）
        assert item.run_id == turns[-1]["run_id"]
        assert len(fake.started) == 2

    @pytest.mark.parametrize("run_status", ["failed", "limit_exceeded", "waiting_input"])
    @pytest.mark.asyncio
    async def test_incomplete_turn_fails_even_when_output_matches(self, monkeypatch, run_status):
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        fake = _FakeRuntime(final_texts={"第一轮": "正确答案", "第二轮": "正确答案"},
                            statuses={"第一轮": run_status})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "第一轮", "expected": "正确答案",
            "complexity": "L4", "task_type": "multi_turn", "turns": ["第二轮"],
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="未完成的多轮任务", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.scalars(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).one()
        assert item.status == "failed" and item.passed is False and item.score is None
        assert item.actual_output == "正确答案"
        assert item.score_detail["execution"]["turns_expected"] == 2
        assert item.stats["turns_count"] == len(fake.started) == 1
        assert item.stats["turns"][0]["status"] == run_status
        assert fake.cancelled == (fake.started if run_status == "waiting_input" else [])

    @pytest.mark.asyncio
    async def test_multi_turn_deadline_cancels_active_run(self, monkeypatch):
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        class SlowRuntime(_FakeRuntime):
            async def start_run_with_session(self, **kwargs):
                run_id, session_id, stream = await super().start_run_with_session(**kwargs)

                async def wait_for_turn():
                    await asyncio.sleep(0.5 if len(self.started) == 1 else 0.7)
                    async for event in stream:
                        yield event

                return run_id, session_id, wait_for_turn()

        fake = SlowRuntime(statuses={"第二轮": "running"})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "第一轮", "expected": "第二轮", "turns": ["第二轮"],
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="多轮共享时限", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={"timeout_s": 1.0})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.scalars(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).one()
        assert len(fake.started) == 2
        assert fake.cancelled == [fake.started[1]]
        assert item.status == "error" and "用例执行超时" in item.error
        assert item.score_detail["execution"]["cancellation_status"] == "cancelling"

    @pytest.mark.asyncio
    async def test_failed_required_tool_is_not_execution_evidence(self, monkeypatch):
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        fake = _FakeRuntime(final_texts={"请计算": "391"},
                            tool_calls={"请计算": [("calculator", None, "failed")]})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "请计算", "expected": "391", "required_tools": ["calculator"],
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="工具执行失败", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.scalars(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).one()
        assert item.status == "failed" and item.passed is False
        assert item.stats["used_tools"] == item.stats["missing_tools"] == ["calculator"]
        assert item.score_detail["capability"]["succeeded_tools"] == []

    @pytest.mark.asyncio
    async def test_required_tools_enforced(self, monkeypatch):
        from app.services.evaluation.runner import (
            create_eval_run, execute_eval_run,
        )

        # 输出正确但全程没用要求的工具 → 客观校验判不通过
        fake = _FakeRuntime(final_texts={"用工具算 17*23": "391"})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "用工具算 17*23", "expected": "391",
            "complexity": "L3", "task_type": "tool_use",
            "required_tools": ["calculator"], "expected_min_steps": 2,
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="工具校验", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.passed is False and item.score == 10.0   # 质量分保留、通过判定被校验覆盖
        check = item.score_detail["capability_check"]
        assert check["missing_tools"] == ["calculator"]
        assert item.stats["missing_tools"] == ["calculator"]
        assert item.stats["steps_met"] is False              # 1 步 < 期望 2 步

        # 调用了要求工具 → 通过
        fake2 = _FakeRuntime(final_texts={"用工具算 17*23": "391"},
                             tool_calls={"用工具算 17*23": ["calculator"]}, steps=2)
        _fake_runtime_module(monkeypatch, fake2)
        async with AsyncSessionLocal() as s:
            run2 = await create_eval_run(
                s, name="工具校验2", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run2_id = run2.id
        await execute_eval_run(run2_id)
        async with AsyncSessionLocal() as s:
            item2 = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run2_id))).scalars().one()
        assert item2.passed is True
        assert item2.stats["used_tools"] == ["calculator"]
        assert item2.stats["tool_chain"][0]["tool_name"] == "calculator"
        assert item2.stats["steps_met"] is True

    @pytest.mark.asyncio
    async def test_forbid_tools(self, monkeypatch):
        from app.services.evaluation.runner import (
            create_eval_run, execute_eval_run,
        )

        fake = _FakeRuntime(final_texts={"只许心算": "59"},
                            tool_calls={"只许心算": ["calculator"]})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "只许心算", "expected": "59",
            "complexity": "L4", "task_type": "adversarial",
            "scoring_config": {"forbid_tools": True},
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="禁工具", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.passed is False
        assert "forbid_tools" not in item.stats   # 统计不含配置键
        assert item.score_detail["capability"]["forbid_tools"] is True

    @pytest.mark.asyncio
    async def test_include_artifacts_scoring(self, monkeypatch):
        """工程构建类用例：评分覆盖工具交付物（文件写入内容 / 成功执行的代码）。"""
        from app.services.evaluation.runner import create_eval_run, execute_eval_run

        # 最终回复只是交付总结，真正交付物（代码）在工具调用里
        fake = _FakeRuntime(
            final_texts={"构建任务库": "构建完成，交付物见工作区文件。"},
            tool_calls={"构建任务库": [
                ("python_exec", {"code": "def add_task():\n    return 1\n"}),
            ]})
        _fake_runtime_module(monkeypatch, fake)
        ds_id, agent_id = await _mk_complex_dataset_and_agent([{
            "input": "构建任务库",
            "scoring_method": "constraint",
            "scoring_config": {
                "constraints": [{"type": "regex", "pattern": "def\\s+add_task"}],
                "include_artifacts": True,
            },
        }])
        async with AsyncSessionLocal() as s:
            run = await create_eval_run(
                s, name="交付物评分", dataset_id=ds_id, agent_id=agent_id,
                created_by=None, config={})
            run_id = run.id
        await execute_eval_run(run_id)
        async with AsyncSessionLocal() as s:
            item = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run_id))).scalars().one()
        assert item.passed is True
        assert (item.stats or {}).get("artifact_chars", 0) > 0
        # 不含交付物口径时同一输出不通过（交付物不在最终回复文本中）
        ds2_id, _ = await _mk_complex_dataset_and_agent([{
            "input": "构建任务库",
            "scoring_method": "constraint",
            "scoring_config": {
                "constraints": [{"type": "regex", "pattern": "def\\s+add_task"}],
            },
        }])
        async with AsyncSessionLocal() as s:
            run2 = await create_eval_run(
                s, name="纯文本评分", dataset_id=ds2_id, agent_id=agent_id,
                created_by=None, config={})
            run2_id = run2.id
        await execute_eval_run(run2_id)
        async with AsyncSessionLocal() as s:
            item2 = (await s.execute(select(EvalRunItem).where(
                EvalRunItem.eval_run_id == run2_id))).scalars().one()
        assert item2.passed is False
        assert (item2.stats or {}).get("artifact_chars") == 0

    @pytest.mark.asyncio
    async def test_collect_artifact_text_excludes_failed(self):
        """交付物采集只统计 succeeded 调用：失败的工具尝试不算交付物。"""
        from app.services.evaluation.runner import _collect_artifact_text

        run_id = f"evalfake-{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as s:
            agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}",
                          system_prompt="t", tools=[], is_active=True)
            s.add(agent)
            await s.flush()
            s.add(AgentRun(
                id=run_id, agent_id=agent.id, thread_id=f"t-{run_id}",
                status="completed", final_text="x"))
            s.add(AgentToolExecution(
                run_id=run_id, tool_call_id="c1", tool_name="write_file",
                status="succeeded", attempt_count=1,
                args_snapshot={"path": "/workspace/a.py", "content": "def ok(): pass"}))
            s.add(AgentToolExecution(
                run_id=run_id, tool_call_id="c2", tool_name="python_exec",
                status="failed", attempt_count=1,
                args_snapshot={"code": "def broken(): pass"}))
            s.add(AgentToolExecution(
                run_id=run_id, tool_call_id="c3", tool_name="python_exec",
                status="succeeded", attempt_count=1,
                args_snapshot={"code": "print('built')"}))
            await s.commit()
            text = await _collect_artifact_text(s, [run_id])
        assert "def ok(): pass" in text
        assert "print('built')" in text
        assert "def broken(): pass" not in text
        assert "/workspace/a.py" in text

    @pytest.mark.asyncio
    async def test_collect_artifact_text_from_internal_tool_events(self):
        """内部工具（write_file 等）不经账本：交付物从事件日志采集。

        事件里的写入内容必须完整（>400 字符不截断——历史 400 字符截断会让
        长代码约束假失败）；失败/无终态的内部调用不算交付物。
        """
        from app.services.evaluation.runner import _collect_artifact_text

        run_id = f"evalfake-{uuid.uuid4().hex[:12]}"
        long_content = "def add_task(name):\n    return name\n" + "# pad line\n" * 200
        async with AsyncSessionLocal() as s:
            agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}",
                          system_prompt="t", tools=[], is_active=True)
            s.add(agent)
            await s.flush()
            s.add(AgentRun(
                id=run_id, agent_id=agent.id, thread_id=f"t-{run_id}",
                status="completed", final_text="x"))
            # 成功的内部 write_file（无账本行，证据只在事件日志）
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=1,
                event_type="tool_started",
                payload={"tool": "write_file", "tool_call_id": "i1",
                         "internal": True,
                         "input": {"path": "/tmp/p/task_manager.py",
                                   "content": long_content}}))
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=2,
                event_type="tool_completed",
                payload={"tool": "write_file", "tool_call_id": "i1",
                         "internal": True, "output": "ok"}))
            # 失败的内部 write_file：不算交付物
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=3,
                event_type="tool_started",
                payload={"tool": "write_file", "tool_call_id": "i2",
                         "internal": True,
                         "input": {"path": "/tmp/p/broken.py",
                                   "content": "def broken(): pass"}}))
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=4,
                event_type="tool_failed",
                payload={"tool": "write_file", "tool_call_id": "i2",
                         "internal": True, "error": "path invalid"}))
            # 只有 started、无终态：结果未确认，不算交付物
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=5,
                event_type="tool_started",
                payload={"tool": "write_file", "tool_call_id": "i3",
                         "internal": True,
                         "input": {"path": "/tmp/p/pending.py",
                                   "content": "def pending(): pass"}}))
            await s.commit()
            text = await _collect_artifact_text(s, [run_id])
        assert long_content in text  # 完整内容（>400 字符）不被截断
        assert "/tmp/p/task_manager.py" in text
        assert "def broken(): pass" not in text
        assert "def pending(): pass" not in text

    @pytest.mark.asyncio
    async def test_collect_artifact_text_prefers_longer_source(self):
        """同一调用账本/事件两份交付物内容取更完整（更长）的一份（抗单一口径截断）。"""
        from app.services.evaluation.runner import _collect_artifact_text

        run_id = f"evalfake-{uuid.uuid4().hex[:12]}"
        full = "def add_task():\n    pass\n" + "# tail\n" * 100
        async with AsyncSessionLocal() as s:
            agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}",
                          system_prompt="t", tools=[], is_active=True)
            s.add(agent)
            await s.flush()
            s.add(AgentRun(
                id=run_id, agent_id=agent.id, thread_id=f"t-{run_id}",
                status="completed", final_text="x"))
            # 账本参数被截断（旧数据 400 字符截断形态），事件里是完整内容
            s.add(AgentToolExecution(
                run_id=run_id, tool_call_id="c1", tool_name="python_exec",
                status="succeeded", attempt_count=1,
                args_snapshot={"code": full[:120]},
            ))
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=1,
                event_type="tool_started",
                payload={"tool": "python_exec", "tool_call_id": "c1",
                         "input": {"code": full}}))
            s.add(AgentRunEvent(
                run_id=run_id, thread_id=f"t-{run_id}", sequence=2,
                event_type="tool_completed",
                payload={"tool": "python_exec", "tool_call_id": "c1",
                         "output": "ok"}))
            await s.commit()
            text = await _collect_artifact_text(s, [run_id])
        assert full in text
        assert text.count("def add_task") == 1  # 同调用只保留一份（不重复计）

    @pytest.mark.asyncio
    async def test_collect_run_facts_merges_internal_tool_events(self):
        """工具链双口径合并：内部工具与未入账本的拒绝调用不得漏报，按事件序号排序。"""
        from app.services.evaluation.runner import _collect_run_facts

        run_id = f"evalfake-{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as s:
            agent = Agent(name=f"agent-{uuid.uuid4().hex[:8]}",
                          system_prompt="t", tools=[], is_active=True)
            s.add(agent)
            await s.flush()
            s.add(AgentRun(
                id=run_id, agent_id=agent.id, thread_id=f"t-{run_id}",
                status="completed", final_text="x"))
            # 账本：python_exec 一次成功（平台工具，事件与账本都有 → 同 tool_call_id 去重）
            s.add(AgentToolExecution(
                run_id=run_id, tool_call_id="p1", tool_name="python_exec",
                status="succeeded", attempt_count=1,
                args_snapshot={"code": "print('built')"}))
            for seq, etype, payload in (
                (1, "tool_started", {"tool": "python_exec", "tool_call_id": "p1",
                                     "input": {"code": "print('built')"}}),
                (2, "tool_completed", {"tool": "python_exec", "tool_call_id": "p1",
                                       "output": "built"}),
                (3, "tool_started", {"tool": "write_file", "tool_call_id": "i1",
                                     "internal": True,
                                     "input": {"path": "/tmp/a.py", "content": "x = 1"}}),
                (4, "tool_completed", {"tool": "write_file", "tool_call_id": "i1",
                                       "internal": True, "output": "ok"}),
                (5, "tool_failed", {"tool": "web_search", "tool_call_id": "w1",
                                    "error": "策略拒绝"}),
            ):
                s.add(AgentRunEvent(
                    run_id=run_id, thread_id=f"t-{run_id}", sequence=seq,
                    event_type=etype, payload=payload))
            await s.commit()
            facts = await _collect_run_facts(s, run_id)
        assert facts["used_tools"] == ["python_exec", "web_search", "write_file"]
        assert facts["tool_total"] == 3  # 账本 1 + 事件独有 2（同 p1 去重）
        assert facts["tool_failed"] == 1  # web_search 策略拒绝那次
        assert [e["tool_name"] for e in facts["tool_chain"]] == \
            ["python_exec", "write_file", "web_search"]  # 按事件序号
        internal = [e for e in facts["tool_chain"] if e.get("internal")]
        assert [e["tool_name"] for e in internal] == ["write_file"]

    def test_aggregate_layers(self):
        from app.services.evaluation.runner import compute_aggregate

        def mk(pos, complexity, task_type, status, score, stats, detail=None):
            return EvalRunItem(
                position=pos, snap_complexity=complexity, snap_task_type=task_type,
                status=status, score=score, stats=stats, score_detail=detail,
                snap_meta={"required_tools": ["calculator"]})

        items = [
            mk(0, "L3", "tool_use", "passed", 10.0, {
                "steps": 3, "tool_calls": 2, "used_tools": ["calculator", "python_exec"],
                "output_chars": 2000,
                "tool_chain": [{"tool_name": "calculator", "status": "succeeded"},
                               {"tool_name": "python_exec", "status": "failed"}]}),
            mk(1, "L3", "tool_use", "failed", 2.0, {
                "steps": 1, "tool_calls": 0, "used_tools": [], "output_chars": 500,
                "missing_tools": ["calculator"], "tool_chain": []}),
            mk(2, "L4", "adversarial", "error", None, {}),
            mk(3, "L4", "long_document", "passed", 7.5, {
                "steps": 4, "tool_calls": 0, "used_tools": [], "output_chars": 3000},
             {"method": "constraint", "total_constraints": 8,
              "passed_constraints": 6, "checks": []}),
        ]
        agg = compute_aggregate(items)
        assert agg["total"] == 4 and agg["passed"] == 2
        assert agg["by_complexity"]["L3"]["total"] == 2
        assert agg["by_complexity"]["L3"]["passed"] == 1
        assert agg["by_complexity"]["L4"]["error"] == 1
        assert agg["by_task_type"]["tool_use"]["pass_rate"] == 0.5
        assert agg["by_task_type"]["long_document"]["total"] == 1
        assert agg["tool_usage"]["calculator"] == {"calls": 1, "failed": 0}
        assert agg["tool_usage"]["python_exec"] == {"calls": 1, "failed": 1}
        cap = agg["capability"]
        assert cap["multi_step_count"] == 2
        assert cap["zero_tool_count"] == 2
        assert cap["tool_error_rate"] == 0.5
        assert cap["missing_required_tools_count"] == 1
        # 长程任务客观指标：输出规模 + 约束达成
        assert cap["avg_output_chars"] == round(5500 / 3, 3)
        assert cap["max_output_chars"] == 3000
        assert cap["constraint_items"] == 1
        assert cap["constraint_total"] == 8 and cap["constraint_passed"] == 6
        assert cap["constraint_ratio"] == 0.75
        # 异常恢复：出现工具失败但最终通过
        assert cap["tool_failure_trials"] == 1
        assert cap["recovered_trials"] == 1
        assert cap["recovery_rate"] == 1.0
        # 问题用例：failed+缺工具、error（未持久化行 id 为 None，按问题清单逐条断言）
        problem_lists = [p["problems"] for p in agg["problem_items"]]
        assert len(problem_lists) == 2
        assert ["failed", "missing_required_tools"] in problem_lists
        assert ["error"] in problem_lists


# ────────────────────────────── API ──────────────────────────────


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def users():
    from app.models.user import User

    sync_engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(sync_engine) as s:
        normal = User(username="eval_u1", email="eval_u1@test.dev",
                      hashed_password="x", is_active=True)
        admin = User(username="eval_admin", email="eval_admin@test.dev",
                     hashed_password="x", is_active=True, is_superuser=True)
        s.add_all([normal, admin])
        s.commit()
        yield {"normal_id": normal.id, "admin_id": admin.id}
        for model in (EvalRunItem, EvalRun, EvalDatasetItem, EvalDataset):
            s.query(model).delete()
        s.query(User).filter(User.username.in_(["eval_u1", "eval_admin"])).delete()
        s.commit()
    sync_engine.dispose()


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _sync():
    return create_engine(os.environ["DATABASE_SYNC_URL"])


def _mk_agent_sync() -> int:
    with Session(_sync()) as s:
        agent = Agent(name=f"测评API-{uuid.uuid4().hex[:6]}",
                      system_prompt="api 测试", tools=[], is_active=True)
        s.add(agent)
        s.commit()
        return agent.id


class TestApiAuth:
    def test_requires_auth(self, client):
        assert client.get("/api/v2/evaluation/datasets").status_code == 401

    def test_requires_superuser(self, client, users):
        r = client.get("/api/v2/evaluation/datasets",
                       headers=_auth(users["normal_id"]))
        assert r.status_code == 403


class TestDatasetApi:
    def test_crud_and_import(self, client, users):
        h = _auth(users["admin_id"])
        # 创建（含用例）
        r = client.post("/api/v2/evaluation/datasets", headers=h, json={
            "name": f"API数据集-{uuid.uuid4().hex[:6]}",
            "description": "API 测试", "category": "general",
            "scoring_method": "exact_match",
            "items": [{"input": "q1", "expected": "a1"},
                      {"input": "q2", "expected": "a2"}],
        })
        assert r.status_code == 200, r.text
        ds = r.json()
        assert ds["item_count"] == 2

        # 详情
        r = client.get(f"/api/v2/evaluation/datasets/{ds['id']}", headers=h)
        assert r.status_code == 200
        assert len(r.json()["items"]) == 2

        # 用例增改删（含复杂任务维度字段）
        r = client.post(f"/api/v2/evaluation/datasets/{ds['id']}/items",
                        headers=h, json={"input": "q3", "expected": "a3",
                                         "complexity": "L4", "task_type": "tool_use",
                                         "required_tools": ["calculator"],
                                         "expected_min_steps": 2,
                                         "turns": ["追问轮"]})
        item_id = r.json()["id"]
        assert r.json()["position"] == 2
        assert r.json()["complexity"] == "L4"
        assert r.json()["required_tools"] == ["calculator"]
        assert r.json()["turns"] == ["追问轮"]
        r = client.put(f"/api/v2/evaluation/datasets/{ds['id']}/items/{item_id}",
                       headers=h, json={"input": "q3x", "expected": "a3x",
                                        "complexity": "L3", "task_type": "multi_turn",
                                        "required_tools": [], "expected_min_steps": None,
                                        "turns": []})
        assert r.json()["input"] == "q3x"
        assert r.json()["complexity"] == "L3"
        r = client.delete(
            f"/api/v2/evaluation/datasets/{ds['id']}/items/{item_id}", headers=h)
        assert r.json()["deleted"] is True

        # JSONL 导入（追加）
        content = '{"input": "q4", "expected": "a4"}\n{"question": "q5", "answer": "a5"}'
        r = client.post("/api/v2/evaluation/datasets/import", headers=h, json={
            "dataset_id": ds["id"], "content": content})
        assert r.status_code == 200, r.text
        assert r.json()["imported"] == 2
        r = client.get(f"/api/v2/evaluation/datasets/{ds['id']}", headers=h)
        assert len(r.json()["items"]) == 4

        # 更新 / 删除
        r = client.put(f"/api/v2/evaluation/datasets/{ds['id']}", headers=h,
                       json={"description": "改过"})
        assert r.json()["description"] == "改过"
        r = client.delete(f"/api/v2/evaluation/datasets/{ds['id']}", headers=h)
        assert r.json()["deleted"] is True

    def test_import_new_dataset(self, client, users):
        h = _auth(users["admin_id"])
        name = f"导入数据集-{uuid.uuid4().hex[:6]}"
        r = client.post("/api/v2/evaluation/datasets/import", headers=h, json={
            "name": name, "scoring_method": "contains",
            "content": '{"input": "x", "expected": "y"}'})
        assert r.status_code == 200
        ds_id = r.json()["dataset_id"]
        r = client.get(f"/api/v2/evaluation/datasets/{ds_id}", headers=h)
        assert r.json()["name"] == name
        assert r.json()["items"][0]["input"] == "x"

    def test_seed_community(self, client, users):
        h = _auth(users["admin_id"])
        r = client.post("/api/v2/evaluation/datasets/seed-community", headers=h)
        assert r.status_code == 200, r.text
        assert len(r.json()["created"]) >= 5
        r = client.get("/api/v2/evaluation/datasets", headers=h)
        names = [d["name"] for d in r.json()["items"]]
        assert any("GSM8K" in n for n in names)


class TestRunApi:
    def test_created_run_is_committed_before_background_execution(self, client, users, monkeypatch):
        from app.services.evaluation import runner as runner_mod

        finished = Event()
        observed = []

        async def observe_run(eval_run_id):
            async with AsyncSessionLocal() as session:
                run = await session.get(EvalRun, eval_run_id)
                items = (await session.scalars(
                    select(EvalRunItem).where(EvalRunItem.eval_run_id == eval_run_id)
                )).all()
                observed.append((run.status if run else None, [item.snap_input for item in items]))
            finished.set()

        monkeypatch.setattr(runner_mod, "execute_eval_run", observe_run)
        headers = _auth(users["admin_id"])
        dataset = client.post("/api/v2/evaluation/datasets", headers=headers, json={
            "name": "后台调度回归数据集", "scoring_method": "exact_match",
            "items": [{"input": "已提交的输入", "expected": "期望结果"}],
        })
        assert dataset.status_code == 200, dataset.text
        response = client.post("/api/v2/evaluation/runs", headers=headers, json={
            "dataset_id": dataset.json()["id"], "agent_id": _mk_agent_sync(),
        })

        assert response.status_code == 200, response.text
        assert finished.wait(timeout=5)
        assert observed == [("pending", ["已提交的输入"])]

    def test_run_lifecycle_endpoints(self, client, users, monkeypatch):
        h = _auth(users["admin_id"])

        async def _noop(eval_run_id):
            return None

        # 后台执行在测试内禁用（真实执行由 runner 测试覆盖）
        import app.services.evaluation.runner as runner_mod
        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)

        # 建数据集 + Agent
        r = client.post("/api/v2/evaluation/datasets", headers=h, json={
            "name": f"任务数据集-{uuid.uuid4().hex[:6]}",
            "scoring_method": "exact_match",
            "items": [{"input": "q1", "expected": "a1"},
                      {"input": "q2", "expected": "a2"}]})
        ds_id = r.json()["id"]
        agent_id = _mk_agent_sync()

        # 创建测评任务
        r = client.post("/api/v2/evaluation/runs", headers=h, json={
            "name": "API 测评任务", "dataset_id": ds_id, "agent_id": agent_id,
            "timeout_s": 60, "limit": 0})
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["status"] == "pending"
        assert run["aggregate"]["total"] == 2

        # 列表 / 详情
        r = client.get("/api/v2/evaluation/runs", headers=h)
        assert any(x["id"] == run["id"] for x in r.json()["items"])
        r = client.get(f"/api/v2/evaluation/runs/{run['id']}", headers=h)
        assert r.json()["dataset_name"] is not None

        # 结果明细（预览）+ 详情
        r = client.get(f"/api/v2/evaluation/runs/{run['id']}/items", headers=h)
        items = r.json()["items"]
        assert len(items) == 2
        assert items[0]["input_preview"].startswith("q1")
        r = client.get(f"/api/v2/evaluation/run-items/{items[0]['id']}", headers=h)
        assert r.json()["snap_input"] == "q1"
        assert r.json()["snap_scoring_method"] == "exact_match"

        # Markdown 导出
        r = client.get(f"/api/v2/evaluation/runs/{run['id']}/markdown", headers=h)
        assert r.status_code == 200
        assert "测评报告" in r.text and "用例明细" in r.text

        # 取消（pending → cancelled）
        r = client.post(f"/api/v2/evaluation/runs/{run['id']}/cancel", headers=h)
        assert r.json()["status"] == "cancelled"

        # 恢复
        r = client.post(f"/api/v2/evaluation/runs/{run['id']}/resume", headers=h)
        assert r.json()["status"] == "pending"

    def test_create_run_validation(self, client, users):
        h = _auth(users["admin_id"])
        r = client.post("/api/v2/evaluation/runs", headers=h, json={
            "dataset_id": 999999, "agent_id": 999999})
        assert r.status_code == 422

    def test_run_item_trace(self, client, users, monkeypatch):
        """执行轨迹端点：事件时间线 + 工具执行账本（纯客观）。"""
        h = _auth(users["admin_id"])

        async def _noop(eval_run_id):
            return None

        import app.services.evaluation.runner as runner_mod
        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)

        r = client.post("/api/v2/evaluation/datasets", headers=h, json={
            "name": f"轨迹数据集-{uuid.uuid4().hex[:6]}",
            "scoring_method": "exact_match",
            "items": [{"input": "tq1", "expected": "ta1"}]})
        ds_id = r.json()["id"]
        agent_id = _mk_agent_sync()
        r = client.post("/api/v2/evaluation/runs", headers=h, json={
            "name": "轨迹任务", "dataset_id": ds_id, "agent_id": agent_id})
        run_id = r.json()["id"]
        r = client.get(f"/api/v2/evaluation/runs/{run_id}/items", headers=h)
        item_id = r.json()["items"][0]["id"]

        # 落事实行：AgentRun + 事件 + 工具执行，并关联到结果行
        fake_run_id = f"trace-{uuid.uuid4().hex[:12]}"
        with Session(_sync()) as s:
            s.add(AgentRun(id=fake_run_id, agent_id=agent_id,
                           thread_id=f"t-{fake_run_id}", status="completed",
                           final_text="ta1"))
            s.add(AgentRunEvent(run_id=fake_run_id, thread_id=f"t-{fake_run_id}",
                                sequence=1, event_type="run.started",
                                payload={"k": 1}))
            s.add(AgentToolExecution(run_id=fake_run_id, tool_call_id="call-0",
                                     tool_name="calculator", status="succeeded",
                                     attempt_count=1))
            s.query(EvalRunItem).filter(EvalRunItem.id == item_id).update(
                {"run_id": fake_run_id})
            s.commit()

        r = client.get(f"/api/v2/evaluation/run-items/{item_id}/trace", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["item_id"] == item_id
        assert body["runs"][0]["run_id"] == fake_run_id
        assert body["runs"][0]["events"][0]["event_type"] == "run.started"
        assert body["runs"][0]["tools"][0]["tool_name"] == "calculator"
