"""自进化闭环 API（/api/v2/evolution，仅超级管理员）。

运行：
    pytest backend/tests/test_api_v2_evolution.py -q
"""
import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def users():
    from app.models.evolution import AgentEvaluation
    from app.models.user import User

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        # 与其他测试文件共享临时 DB，先清掉遗留评估数据保证断言确定
        s.query(AgentEvaluation).delete()
        s.commit()
        normal = User(username="evo_u1", email="evo_u1@test.dev",
                      hashed_password="x", is_active=True)
        admin = User(username="evo_admin", email="evo_admin@test.dev",
                     hashed_password="x", is_active=True, is_superuser=True)
        s.add_all([normal, admin])
        s.commit()
        return {"normal_id": normal.id, "admin_id": admin.id}


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _sync():
    return create_engine(os.environ["DATABASE_SYNC_URL"])


def _mk_agent() -> int:
    from app.models.agent import Agent

    with Session(_sync()) as s:
        agent = Agent(name="进化 API 测试", system_prompt="测试提示词基线内容",
                      tools=[], is_active=True)
        s.add(agent)
        s.commit()
        return agent.id


def _mk_run(run_id: str, agent_id: int, status: str = "cancelled") -> str:
    from app.models.agent import AgentRun

    with Session(_sync()) as s:
        s.add(AgentRun(id=run_id, agent_id=agent_id, thread_id=f"t-{run_id}",
                       status=status, finished_at=datetime.utcnow()))
        s.commit()
    return run_id


def _mk_report_with_suggestion(agent_id: int) -> int:
    from app.models.evolution import EvolutionReport

    with Session(_sync()) as s:
        report = EvolutionReport(
            agent_id=agent_id,
            window_start=datetime.utcnow(), window_end=datetime.utcnow(),
            status="draft", summary_text="# 摘要",
            suggestions=[{
                "type": "prompt", "target": "system_prompt",
                "current": "测试提示词基线内容",
                "proposed": "API 应用后的新提示词全文",
                "auto_applicable": True, "priority": 5,
            }],
            metrics_before={"sample_count": 3},
        )
        s.add(report)
        s.commit()
        return report.id


class TestAuth:
    def test_requires_auth(self, client):
        r = client.get("/api/v2/evolution/overview")
        assert r.status_code == 401

    def test_requires_superuser(self, client, users):
        r = client.get("/api/v2/evolution/overview",
                       headers=_auth(users["normal_id"]))
        assert r.status_code == 403


class TestOverviewEvaluations:
    def test_overview_structure(self, client, users):
        r = client.get("/api/v2/evolution/overview",
                       headers=_auth(users["admin_id"]))
        assert r.status_code == 200
        body = r.json()
        assert "overall" in body and "by_version" in body
        assert body["overall"]["sample_count"] == 0

    def test_evaluations_list(self, client, users):
        r = client.get("/api/v2/evolution/evaluations",
                       headers=_auth(users["admin_id"]))
        assert r.status_code == 200
        body = r.json()
        assert {"items", "total", "page", "page_size"} <= set(body)

    def test_trigger_requires_selector(self, client, users):
        r = client.post("/api/v2/evolution/evaluations/trigger", json={},
                        headers=_auth(users["admin_id"]))
        assert r.status_code == 422

    def test_trigger_idempotent(self, client, users, monkeypatch):
        async def fake_write(trace_id, scores):
            pass

        monkeypatch.setattr(
            "app.services.evolution.langfuse_api.write_scores", fake_write)
        agent_id = _mk_agent()
        run_id = _mk_run("api-evo-run-1", agent_id)
        headers = _auth(users["admin_id"])

        r = client.post("/api/v2/evolution/evaluations/trigger",
                        json={"run_ids": [run_id]}, headers=headers)
        assert r.status_code == 200
        assert r.json()["queued"] == 1

        r = client.post("/api/v2/evolution/evaluations/trigger",
                        json={"run_ids": [run_id]}, headers=headers)
        assert r.json()["queued"] == 0, "重复触发幂等"

        r = client.get("/api/v2/evolution/evaluations", headers=headers)
        assert r.json()["total"] >= 1


class TestReportsAndChanges:
    def test_generate_unknown_agent_422(self, client, users):
        r = client.post("/api/v2/evolution/reports/generate",
                        json={"agent_id": 999999}, headers=_auth(users["admin_id"]))
        assert r.status_code == 422

    def test_generate_and_export(self, client, users, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("LLM 不可用")

        monkeypatch.setattr(
            "app.services.evolution.evolution_advisor._llm_suggestions", boom)
        agent_id = _mk_agent()
        headers = _auth(users["admin_id"])

        r = client.post("/api/v2/evolution/reports/generate",
                        json={"agent_id": agent_id, "window_hours": 24},
                        headers=headers)
        assert r.status_code == 200
        report = r.json()
        assert report["status"] == "draft"
        assert "summary_text" in report and "suggestions" in report

        r = client.get(f"/api/v2/evolution/reports/{report['id']}", headers=headers)
        assert r.status_code == 200

        r = client.get(f"/api/v2/evolution/reports/{report['id']}/markdown",
                       headers=headers)
        assert r.status_code == 200
        assert r.json()["markdown"]

        r = client.get("/api/v2/evolution/reports", headers=headers)
        assert r.json()["total"] >= 1

        r = client.get("/api/v2/evolution/reports/999999", headers=headers)
        assert r.status_code == 404

    def test_apply_rollback_roundtrip(self, client, users):
        agent_id = _mk_agent()
        report_id = _mk_report_with_suggestion(agent_id)
        headers = _auth(users["admin_id"])

        r = client.post(
            f"/api/v2/evolution/reports/{report_id}/suggestions/0/apply",
            headers=headers)
        assert r.status_code == 200
        result = r.json()
        assert result["target"] == "system_prompt"
        change_id = result["change_id"]

        r = client.post(
            f"/api/v2/evolution/reports/{report_id}/suggestions/9/apply",
            headers=headers)
        assert r.status_code == 422, "越界序号拒绝"

        r = client.get("/api/v2/evolution/changes", headers=headers)
        items = r.json()["items"]
        assert any(i["id"] == change_id and i["status"] == "applied"
                   for i in items)

        r = client.post(f"/api/v2/evolution/changes/{change_id}/rollback",
                        headers=headers)
        assert r.status_code == 200

        r = client.post(f"/api/v2/evolution/changes/{change_id}/rollback",
                        headers=headers)
        assert r.status_code == 422, "二次回滚拒绝"
