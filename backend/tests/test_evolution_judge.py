"""自进化：LLM-as-judge（judge.py）。

运行：
    pytest backend/tests/test_evolution_judge.py -q
"""
import json

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun
from app.models.model_provider import ModelConfig, ModelProvider
from app.services.evolution.judge import (
    JudgeError, _extract_json, _pick_judge_model, _task_text, _validate,
    judge_run,
)

_CLEAN = (AgentRun, Agent, ModelConfig, ModelProvider)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _mk_model(model_type="chat", is_active=True) -> tuple:
    import uuid

    suffix = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as s:
        p = ModelProvider(name=f"jp-{suffix}", display_name="J",
                          provider_type="openai_compatible", base_url="http://x")
        s.add(p)
        await s.flush()
        mc = ModelConfig(provider_id=p.id, name=f"jm-{suffix}", display_name="J",
                         model_type=model_type, is_active=is_active)
        s.add(mc)
        await s.commit()
        return p.id, mc.id


def _mk_run(**kw) -> AgentRun:
    return AgentRun(
        id=kw.get("id", "judge-run-1"), agent_id=1, thread_id="t",
        status="completed", final_text=kw.get("final_text", "最终答案"),
        input_payload=kw.get("input_payload", {
            "messages": [{"role": "user", "content": "做个天气查询"}]}),
        agent_config_snapshot=kw.get("snapshot"),
    )


class TestExtractValidate:
    def test_extract_plain_and_codeblock(self):
        assert _extract_json('{"a": 1}') == {"a": 1}
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert _extract_json('前缀 {"a": [1,2]} 后缀') == {"a": [1, 2]}

    def test_extract_no_json_raises(self):
        with pytest.raises(JudgeError):
            _extract_json("没有任何结构化内容")

    def _full(self, **over):
        data = {
            "dimensions": {k: 7 for k in (
                "task_completion", "answer_quality", "tool_selection",
                "efficiency", "groundedness", "format_compliance")},
            "verdict": "pass", "issues": [], "confidence": 0.9,
        }
        data.update(over)
        return data

    def test_validate_ok(self):
        out = _validate(self._full())
        assert out["verdict"] == "pass"
        assert out["overall"] == 7.0
        assert out["confidence"] == 0.9
        assert set(out["dimensions"]) == {
            "task_completion", "answer_quality", "tool_selection",
            "efficiency", "groundedness", "format_compliance"}

    def test_validate_missing_dimension(self):
        data = self._full()
        del data["dimensions"]["efficiency"]
        with pytest.raises(JudgeError):
            _validate(data)

    def test_validate_bad_verdict(self):
        with pytest.raises(JudgeError):
            _validate(self._full(verdict="maybe"))

    def test_validate_clamps_confidence_and_caps_issues(self):
        data = self._full(confidence=99, issues=[{"i": n} for n in range(8)])
        out = _validate(data)
        assert out["confidence"] == 1.0
        assert len(out["issues"]) == 5


class TestTaskText:
    def test_last_user_message(self):
        run = _mk_run(input_payload={"messages": [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "答"},
            {"role": "user", "content": "第二问"},
        ]})
        assert _task_text(run) == "第二问"

    def test_fallback_task_key(self):
        run = _mk_run(input_payload={"task": "汇总周报"})
        assert _task_text(run) == "汇总周报"


class TestPickJudgeModel:
    @pytest.mark.asyncio
    async def test_explicit_setting_wins(self, monkeypatch):
        _pid, mc1 = await _mk_model()
        _pid2, mc2 = await _mk_model()
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", mc2)
        async with AsyncSessionLocal() as s:
            _p, mc, mc_id = await _pick_judge_model(s, _mk_run(
                snapshot={"model_config_id": mc1}))
        assert mc_id == mc2

    @pytest.mark.asyncio
    async def test_run_snapshot_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0)
        _pid, mc1 = await _mk_model()
        async with AsyncSessionLocal() as s:
            _p, _mc, mc_id = await _pick_judge_model(
                s, _mk_run(snapshot={"model_config_id": mc1}))
        assert mc_id == mc1

    @pytest.mark.asyncio
    async def test_any_active_chat_model(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0)
        _pid, mc1 = await _mk_model()
        async with AsyncSessionLocal() as s:
            _p, _mc, mc_id = await _pick_judge_model(s, _mk_run(snapshot=None))
        assert mc_id == mc1

    @pytest.mark.asyncio
    async def test_no_model_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0)
        with pytest.raises(JudgeError):
            async with AsyncSessionLocal() as s:
                await _pick_judge_model(s, _mk_run(snapshot=None))


class TestJudgeRun:
    def _valid_content(self) -> str:
        return json.dumps({
            "dimensions": {k: 8 for k in (
                "task_completion", "answer_quality", "tool_selection",
                "efficiency", "groundedness", "format_compliance")},
            "verdict": "pass",
            "issues": [{"dimension": "efficiency", "evidence": "绕路",
                        "suggestion": "精简步骤"}],
            "confidence": 0.8,
        }, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_judge_success(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0)
        _pid, mc1 = await _mk_model()

        async def fake_chat(provider, mc, request):
            return {"content": self._valid_content()}

        monkeypatch.setattr(
            "app.services.model_service.ModelService.chat", fake_chat)
        async with AsyncSessionLocal() as s:
            out = await judge_run(s, _mk_run(snapshot={"model_config_id": mc1}),
                                  {"deterministic_score": 9})
        assert out["verdict"] == "pass"
        assert out["overall"] == 8.0
        assert out["judge_model_config_id"] == mc1
        assert out["issues"][0]["dimension"] == "efficiency"

    @pytest.mark.asyncio
    async def test_judge_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0)
        _pid, mc1 = await _mk_model()
        calls = {"n": 0}

        async def fake_chat(provider, mc, request):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": "坏输出"}
            return {"content": self._valid_content()}

        monkeypatch.setattr(
            "app.services.model_service.ModelService.chat", fake_chat)
        async with AsyncSessionLocal() as s:
            out = await judge_run(s, _mk_run(snapshot={"model_config_id": mc1}), {})
        assert calls["n"] == 2
        assert out["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_judge_two_failures_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0)
        _pid, mc1 = await _mk_model()

        async def fake_chat(provider, mc, request):
            return {"content": "坏输出"}

        monkeypatch.setattr(
            "app.services.model_service.ModelService.chat", fake_chat)
        with pytest.raises(JudgeError):
            async with AsyncSessionLocal() as s:
                await judge_run(s, _mk_run(snapshot={"model_config_id": mc1}), {})
