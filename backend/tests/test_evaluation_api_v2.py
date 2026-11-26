"""测评 v2 API：套件、套件 Campaign、版本对比、重新评分、质量审计。"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.core.database import AsyncSessionLocal, init_db
from app.models.agent import Agent, AgentRun
from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalGrade, EvalRun, EvalRunItem,
    EvalSuite, EvalSuiteDataset, EvalSuiteVersion,
)
from app.models.user import User


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def users():
    sync_engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(sync_engine) as s:
        admin = User(username=f"ev2_admin_{uuid.uuid4().hex[:6]}",
                     email=f"{uuid.uuid4().hex[:6]}@test.dev",
                     hashed_password="x", is_active=True, is_superuser=True)
        s.add(admin)
        s.commit()
        admin_id = admin.id
        yield {"admin_id": admin_id}
        for model in (EvalGrade, EvalRunItem, EvalRun, EvalSuiteDataset,
                      EvalSuiteVersion, EvalSuite, EvalDatasetItem, EvalDataset,
                      AgentRun, Agent):
            s.query(model).delete()
        s.query(User).filter(User.id == admin_id).delete()
        s.commit()
    sync_engine.dispose()


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _sync():
    return create_engine(os.environ["DATABASE_SYNC_URL"])


def _mk_agent() -> int:
    with Session(_sync()) as s:
        agent = Agent(name=f"api-v2-{uuid.uuid4().hex[:6]}",
                      system_prompt="api", tools=[], is_active=True)
        s.add(agent)
        s.commit()
        return agent.id


def _mk_dataset(client, headers, name: str, inputs: list[str]) -> int:
    r = client.post("/api/v2/evaluation/datasets", headers=headers, json={
        "name": name, "scoring_method": "exact_match",
        "items": [{"input": text, "expected": "a"} for text in inputs],
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


class TestSuiteCampaignApi:
    def test_suite_publish_and_campaign(self, client, users, monkeypatch):
        from app.services.evaluation import runner as runner_mod

        async def _noop(run_id):
            return None

        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)
        headers = _auth(users["admin_id"])
        suffix = uuid.uuid4().hex[:6]
        ds1 = _mk_dataset(client, headers, f"套件A-{suffix}", ["s1"])
        ds2 = _mk_dataset(client, headers, f"套件B-{suffix}", ["s2", "s3"])

        r = client.post("/api/v2/evaluation/suites", headers=headers, json={
            "name": f"套件-{suffix}", "description": "api 测试",
            "dataset_ids": [ds1, ds2]})
        assert r.status_code == 200, r.text
        suite = r.json()
        assert suite["dataset_ids"] == [ds1, ds2]

        r = client.post(f"/api/v2/evaluation/suites/{suite['id']}/versions",
                        headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["total_items"] == 3

        r = client.post("/api/v2/evaluation/runs", headers=headers, json={
            "name": "套件 Campaign", "suite_id": suite["id"],
            "agent_id": _mk_agent(), "trials_per_case": 2})
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["suite_id"] == suite["id"]
        assert run["suite_version_id"] is not None
        assert run["trials_per_case"] == 2
        assert run["aggregate"]["total"] == 6
        assert run["agent_version_id"] is not None

        r = client.get(f"/api/v2/evaluation/runs/{run['id']}/items", headers=headers)
        items = r.json()["items"]
        assert len(items) == 6
        assert {i["trial_index"] for i in items} == {0, 1}
        assert sum(1 for i in items if i["dataset_id"] == ds2) == 4

        r = client.get("/api/v2/evaluation/suites", headers=headers)
        assert any(s["id"] == suite["id"] for s in r.json()["items"])

    def test_compare_and_audit(self, client, users):
        headers = _auth(users["admin_id"])
        agent_id = _mk_agent()
        suffix = uuid.uuid4().hex[:6]
        ds_id = _mk_dataset(client, headers, f"对比集-{suffix}", ["c1", "c2"])

        with Session(_sync()) as s:
            base = EvalRun(name="base", dataset_id=ds_id, agent_id=agent_id,
                           status="completed", aggregate={})
            target = EvalRun(name="target", dataset_id=ds_id, agent_id=agent_id,
                             status="completed", aggregate={})
            s.add_all([base, target])
            s.flush()
            s.add_all([
                EvalRunItem(eval_run_id=base.id, position=0, snap_input="c1",
                            snap_expected="a", status="passed", outcome="pass"),
                EvalRunItem(eval_run_id=base.id, position=1, snap_input="c2",
                            snap_expected="a", status="failed", outcome="fail"),
                EvalRunItem(eval_run_id=target.id, position=0, snap_input="c1",
                            snap_expected="a", status="failed", outcome="fail"),
                EvalRunItem(eval_run_id=target.id, position=1, snap_input="c2",
                            snap_expected="a", status="passed", outcome="pass"),
            ])
            s.commit()
            base_id, target_id = base.id, target.id

        r = client.get("/api/v2/evaluation/compare", headers=headers,
                       params={"base_run_id": base_id, "target_run_id": target_id})
        assert r.status_code == 200, r.text
        summary = r.json()["summary"]
        assert summary["improved"] == 1 and summary["regressed"] == 1
        assert summary["cases_paired"] == 2

        r = client.get("/api/v2/evaluation/quality-audit", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["totals"]["cases"] >= 2

    def test_artifact_preview_and_path_guard(self, client, users, monkeypatch):
        from pathlib import Path

        from app.core.config import settings
        from app.services.evaluation import runner as runner_mod

        async def _noop(run_id):
            return None

        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)
        headers = _auth(users["admin_id"])
        suffix = uuid.uuid4().hex[:6]
        ds_id = _mk_dataset(client, headers, f"产物集-{suffix}", ["p1"])
        agent_id = _mk_agent()
        r = client.post("/api/v2/evaluation/runs", headers=headers, json={
            "dataset_id": ds_id, "agent_id": agent_id})
        run_id = r.json()["id"]
        item_id = client.get(
            f"/api/v2/evaluation/runs/{run_id}/items", headers=headers
        ).json()["items"][0]["id"]

        session_id = 424242
        root = Path(settings.AGENT_FILES_ROOT) / str(users["admin_id"]) / str(agent_id) / str(session_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_text(
            "最终产物内容\napi_key=supersecret123\n", encoding="utf-8")
        with Session(_sync()) as s:
            s.query(EvalRunItem).filter(EvalRunItem.id == item_id).update(
                {"stats": {"session_id": session_id}})
            s.commit()

        r = client.get(f"/api/v2/evaluation/run-items/{item_id}/artifact",
                       headers=headers, params={"path": "report.md"})
        assert r.status_code == 200, r.text
        assert "最终产物内容" in r.text
        assert "supersecret123" not in r.text and "***" in r.text

        r = client.get(f"/api/v2/evaluation/run-items/{item_id}/artifact",
                       headers=headers, params={"path": "../report.md"})
        assert r.status_code in (400, 404)

    def test_regression_archive_endpoint(self, client, users, monkeypatch):
        from app.services.evaluation import runner as runner_mod

        async def _noop(run_id):
            return None

        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)
        headers = _auth(users["admin_id"])
        suffix = uuid.uuid4().hex[:6]
        ds_id = _mk_dataset(client, headers, f"回归集-{suffix}", ["g1"])
        r = client.post("/api/v2/evaluation/runs", headers=headers, json={
            "dataset_id": ds_id, "agent_id": _mk_agent()})
        run_id = r.json()["id"]
        item_id = client.get(
            f"/api/v2/evaluation/runs/{run_id}/items", headers=headers
        ).json()["items"][0]["id"]

        with Session(_sync()) as s:
            s.query(EvalRunItem).filter(EvalRunItem.id == item_id).update({
                "execution_status": "succeeded", "actual_output": "wrong",
                "status": "failed", "outcome": "fail", "passed": False,
            })
            s.commit()

        r = client.post(
            f"/api/v2/evaluation/run-items/{item_id}/to-regression",
            headers=headers, json={})
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True
        assert r.json()["dataset_id"] != ds_id

    def test_manual_review_endpoint(self, client, users, monkeypatch):
        from app.services.evaluation import runner as runner_mod

        async def _noop(run_id):
            return None

        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)
        headers = _auth(users["admin_id"])
        suffix = uuid.uuid4().hex[:6]
        ds_id = _mk_dataset(client, headers, f"复核集-{suffix}", ["m1"])
        r = client.post("/api/v2/evaluation/runs", headers=headers, json={
            "dataset_id": ds_id, "agent_id": _mk_agent()})
        run_id = r.json()["id"]
        item_id = client.get(
            f"/api/v2/evaluation/runs/{run_id}/items", headers=headers
        ).json()["items"][0]["id"]

        r = client.post(f"/api/v2/evaluation/run-items/{item_id}/review",
                        headers=headers,
                        json={"decision": "confirm_pass", "note": "人工确认"})
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "pass"
        detail = client.get(f"/api/v2/evaluation/run-items/{item_id}",
                            headers=headers).json()
        assert detail["score_detail"]["manual_review"]["note"] == "人工确认"
        assert any(g["kind"] == "manual" for g in detail["grades"])

        r = client.post(f"/api/v2/evaluation/run-items/{item_id}/review",
                        headers=headers, json={"decision": "reject_score"})
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "inconclusive"

    def test_regrade_endpoint(self, client, users, monkeypatch):
        from app.services.evaluation import runner as runner_mod

        async def _noop(run_id):
            return None

        monkeypatch.setattr(runner_mod, "start_eval_run_background", _noop)
        headers = _auth(users["admin_id"])
        suffix = uuid.uuid4().hex[:6]
        ds_id = _mk_dataset(client, headers, f"重评集-{suffix}", ["r1"])
        r = client.post("/api/v2/evaluation/runs", headers=headers, json={
            "dataset_id": ds_id, "agent_id": _mk_agent()})
        run_id = r.json()["id"]
        item_id = client.get(
            f"/api/v2/evaluation/runs/{run_id}/items", headers=headers
        ).json()["items"][0]["id"]

        with Session(_sync()) as s:
            s.query(EvalRunItem).filter(EvalRunItem.id == item_id).update({
                "execution_status": "succeeded", "run_id": None,
                "actual_output": "a", "status": "pending", "outcome": "pending",
            })
            s.commit()

        r = client.post(f"/api/v2/evaluation/run-items/{item_id}/regrade",
                        headers=headers, json={})
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "pass"

        detail = client.get(f"/api/v2/evaluation/run-items/{item_id}",
                            headers=headers).json()
        assert detail["grades"], "重新评分必须留痕"
