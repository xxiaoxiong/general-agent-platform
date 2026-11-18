"""Agent 测评模块：评分器 / 社区数据集 / 运行器 / API（/api/v2/evaluation）。

运行：
    pytest backend/tests/test_evaluation_module.py -q
"""
import os
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun
from app.models.evaluation import EvalDataset, EvalDatasetItem, EvalRun, EvalRunItem

_CLEAN = (EvalRunItem, EvalRun, EvalDatasetItem, EvalDataset, AgentRun, Agent)


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
        assert len(defs) == 3
        names = [d["name"] for d in defs]
        assert any("GSM8K" in n for n in names)
        assert any("BFCL" in n for n in names)
        for d in defs:
            assert len(d["items"]) == 20
            assert d.get("reference"), "社区数据集必须注明出处"
            for it in d["items"]:
                assert it["input"]

    @pytest.mark.asyncio
    async def test_seed_idempotent(self):
        from app.services.evaluation.community import seed_community_datasets

        async with AsyncSessionLocal() as s:
            r1 = await seed_community_datasets(s)
            r2 = await seed_community_datasets(s)
        assert len(r1["created"]) == 3 and r1["skipped"] == []
        assert r2["created"] == [] and len(r2["skipped"]) == 3
        async with AsyncSessionLocal() as s:
            total = len((await s.execute(
                select(EvalDataset))).scalars().all())
        assert total == 3


# ────────────────────────────── 运行器 ──────────────────────────────


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aiter__(self):
        for e in self._events:
            yield e


class _FakeRuntime:
    """假 Runtime：起 Run 时直接落 AgentRun 行并返回即完成的事件流。"""

    def __init__(self, final_texts=None, error: Exception | None = None):
        self.final_texts = final_texts or {}   # input → final_text
        self.error = error
        self.started = []

    async def start_run_with_session(self, *, agent_config, messages,
                                     user_id, session_id=None,
                                     idempotency_key=None, use_memory_context="",
                                     db_info=None, endpoint="chat",
                                     user_message=None, **kw):
        if self.error is not None:
            raise self.error
        user_input = (messages[0] or {}).get("content", "")
        final_text = self.final_texts.get(user_input, user_input)
        run_id = f"evalfake-{uuid.uuid4().hex[:12]}"
        self.started.append(run_id)
        async with AsyncSessionLocal() as s:
            s.add(AgentRun(
                id=run_id, agent_id=agent_config.get("id"), thread_id=f"t-{run_id}",
                status="completed", final_text=final_text,
                budget_steps=1, budget_model_calls=1, budget_tokens=10))
            await s.commit()
        return run_id, None, _FakeStream([{"type": "done", "status": "completed"}])


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

        # 用例增改删
        r = client.post(f"/api/v2/evaluation/datasets/{ds['id']}/items",
                        headers=h, json={"input": "q3", "expected": "a3"})
        item_id = r.json()["id"]
        assert r.json()["position"] == 2
        r = client.put(f"/api/v2/evaluation/datasets/{ds['id']}/items/{item_id}",
                       headers=h, json={"input": "q3x", "expected": "a3x"})
        assert r.json()["input"] == "q3x"
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
        assert len(r.json()["created"]) == 3
        r = client.get("/api/v2/evaluation/datasets", headers=h)
        names = [d["name"] for d in r.json()["items"]]
        assert any("GSM8K" in n for n in names)


class TestRunApi:
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
