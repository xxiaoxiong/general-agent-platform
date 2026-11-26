"""Workspace Sandbox：Workspace 1:1 / 调度预留 / Job Lease-Fencing / 生命周期 / 回收。

运行：
    cd backend && python -m pytest tests/test_sandbox_runtime.py -q
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.sandbox import (
    PreviewGrant, Project, ProjectMember, SandboxCommandOperation, SandboxEvent,
    SandboxInstance, SandboxJob, SandboxResourceProfile, SandboxResourceReservation,
    SandboxRunnerNode, Workspace, WorkspaceService,
)
from app.models.user import User
from app.services.sandbox import jobs, projects, runtime, scheduler
from app.services.sandbox.common import (
    SandboxNotFoundError, SandboxQuotaError, SandboxResourceUnavailable,
)
from app.services.sandbox.common import utcnow
from app.services.sandbox.profiles import ensure_default_profiles

pytestmark = pytest.mark.asyncio

_CLEAN = (
    SandboxEvent, PreviewGrant, WorkspaceService, SandboxCommandOperation,
    SandboxResourceReservation, SandboxJob, SandboxInstance, SandboxRunnerNode,
    Workspace, ProjectMember, Project,
)


@pytest_asyncio.fixture(autouse=True)
async def _db(monkeypatch):
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.execute(delete(User).where(User.username.like("sbxrt-%")))
        await s.execute(delete(SandboxResourceProfile))
        await s.commit()
        await ensure_default_profiles(s)
        await s.commit()
    yield
    await engine.dispose()


async def _user(name: str) -> User:
    async with AsyncSessionLocal() as s:
        user = User(
            username=f"sbxrt-{name}", email=f"sbxrt-{name}@test.dev",
            hashed_password="x", is_active=True,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _runner(name: str = "runner-a", **overrides) -> SandboxRunnerNode:
    payload = {
        "node_name": name,
        "runtime_provider": "docker",
        "runtime_version": "test",
        "cpu_total_millis": 8000,
        "cpu_allocatable_millis": 8000,
        "memory_total_bytes": 16 * 1024 ** 3,
        "memory_allocatable_bytes": 16 * 1024 ** 3,
        "disk_total_bytes": 100 * 1024 ** 3,
        "disk_allocatable_bytes": 100 * 1024 ** 3,
    }
    payload.update(overrides)
    async with AsyncSessionLocal() as s:
        runner = await runtime.register_runner(s, payload)
        await s.commit()
        await s.refresh(runner)
        return runner


async def _project_with_workspace(user: User, name: str = "项目A"):
    async with AsyncSessionLocal() as s:
        project, workspace = await projects.create_project(s, user, name)
        await s.commit()
        await s.refresh(project)
        await s.refresh(workspace)
        return project, workspace


async def test_workspace_is_one_to_one_and_volume_stable():
    user = await _user("u1")
    project, workspace = await _project_with_workspace(user)
    async with AsyncSessionLocal() as s:
        again = await runtime.ensure_workspace(s, project.id, user.id)
        await s.commit()
        count = len((await s.execute(
            select(Workspace).where(Workspace.project_id == project.id)
        )).scalars().all())
        assert again.id == workspace.id
        assert count == 1
        assert workspace.volume_id.startswith(settings.SANDBOX_VOLUME_PREFIX)
        assert workspace.volume_id == runtime.build_volume_id(project.id)


async def test_provision_creates_job_reservation_and_is_idempotent():
    user = await _user("u2")
    project, workspace = await _project_with_workspace(user)
    await _runner()
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        assert sandbox.observed_state == "queued"
        assert sandbox.generation == 1
        job_rows = (await s.execute(select(SandboxJob))).scalars().all()
        assert len(job_rows) == 1
        assert job_rows[0].job_type == "provision"
        assert job_rows[0].status == "queued"
        reservation = (await s.execute(select(SandboxResourceReservation))).scalar_one()
        assert reservation.status == "reserved"
        assert reservation.cpu_reserved_millis == sandbox.cpu_request_millis
        runner = (await s.execute(select(SandboxRunnerNode))).scalar_one()
        assert runner.cpu_reserved_millis == sandbox.cpu_request_millis
        assert runner.memory_reserved_bytes == sandbox.memory_request_bytes
        # 幂等：再次 provision 返回同一 Sandbox，不创建新 Generation / 新 Job
        again = await runtime.provision_workspace(s, workspace)
        await s.commit()
        assert again.id == sandbox.id
        assert len((await s.execute(select(SandboxJob))).scalars().all()) == 1
        assert len((await s.execute(select(SandboxInstance))).scalars().all()) == 1


async def test_job_claim_fencing_complete_marks_workspace_ready():
    user = await _user("u3")
    project, workspace = await _project_with_workspace(user)
    runner = await _runner()
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner, limit=2)
        await s.commit()
        assert len(claimed) == 1
        job = claimed[0]
        assert job.status == "leased"
        assert job.lease_token == 1
        assert job.attempt == 1
        # 正确 Fencing Token 续租成功；旧/错误 token 被拒绝
        assert await jobs.heartbeat_job(s, job.id, runner, 1) is True
        assert await jobs.heartbeat_job(s, job.id, runner, 0) is False
        await s.commit()
        completed = await jobs.complete_job(s, job.id, runner, 1, {
            "container_id": "c1", "container_name": f"gap-sbx-{sandbox.id}-g1",
            "container_ip": "10.10.0.2", "image_digest": "sha256:abc",
            "observed_state": "ready",
        })
        await runtime.apply_job_complete(s, completed, completed.result_json or {})
        await s.commit()
        await s.refresh(sandbox)
        await s.refresh(workspace)
        assert sandbox.observed_state == "ready"
        assert sandbox.container_ip == "10.10.0.2"
        assert workspace.status == "ready"
        assert workspace.current_sandbox_id == sandbox.id
        reservation = (await s.execute(select(SandboxResourceReservation))).scalar_one()
        assert reservation.status == "active"


async def test_job_lease_expiry_requeue_and_fencing_rejection():
    user = await _user("u4")
    project, workspace = await _project_with_workspace(user)
    runner = await _runner()
    async with AsyncSessionLocal() as s:
        await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        # 租约过期 -> Reconciler 回收为 queued
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()
        from app.services.sandbox.reconciler import get_reconciler

        summary = await get_reconciler().run_once()
        assert summary["recovered"] >= 1
        await s.refresh(job)
        assert job.status == "queued"
        assert job.lease_owner is None
        # 二次领取 attempt+1 / token+1
        claimed2 = await jobs.claim_jobs(s, runner)
        await s.commit()
        assert claimed2[0].id == job.id
        assert claimed2[0].lease_token == 2
        # 旧 token 完成被拒绝（fencing）
        from app.services.sandbox.common import JobLeaseLost

        with pytest.raises(JobLeaseLost):
            await jobs.complete_job(s, job.id, runner, 1, {})


async def test_failed_job_requeues_then_applies_failure():
    user = await _user("u5")
    project, workspace = await _project_with_workspace(user)
    runner = await _runner()
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        job.max_attempts = 2
        await s.commit()
        job2, retry = await jobs.fail_job(s, job.id, runner, job.lease_token, "docker_error", "boom")
        await s.commit()
        assert retry is True
        assert job2.status == "queued"
        claimed2 = await jobs.claim_jobs(s, runner)
        await s.commit()
        job3, retry2 = await jobs.fail_job(s, claimed2[0].id, runner, claimed2[0].lease_token,
                                           "docker_error", "boom again")
        assert retry2 is False
        assert job3.status == "failed"
        await runtime.apply_job_failure(s, job3, "docker_error", "boom again")
        await s.commit()
        await s.refresh(sandbox)
        await s.refresh(workspace)
        assert sandbox.observed_state == "failed"
        assert workspace.status == "degraded"


async def test_stop_releases_compute_and_start_reserves_again():
    user = await _user("u6")
    project, workspace = await _project_with_workspace(user)
    runner = await _runner()
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        done = await jobs.complete_job(s, job.id, runner, job.lease_token, {
            "container_id": "c1", "container_ip": "10.1.0.2", "observed_state": "ready",
        })
        await runtime.apply_job_complete(s, done, done.result_json or {})
        await s.commit()
        await s.refresh(sandbox)
        assert sandbox.observed_state == "ready"
        # 停止：释放 CPU/内存，磁盘保留
        stop_job = await runtime.request_lifecycle(s, workspace, sandbox, "stop")
        await s.commit()
        claimed_stop = await jobs.claim_jobs(s, runner)
        await s.commit()
        stop = next(i for i in claimed_stop if i.id == stop_job.id)
        done_stop = await jobs.complete_job(s, stop.id, runner, stop.lease_token, {})
        await runtime.apply_job_complete(s, done_stop, {})
        await s.commit()
        await s.refresh(sandbox)
        assert sandbox.observed_state == "stopped"
        reservation = (await s.execute(
            select(SandboxResourceReservation).where(
                SandboxResourceReservation.sandbox_id == sandbox.id)
        )).scalar_one()
        await s.refresh(reservation)
        assert reservation.cpu_reserved_millis == 0
        assert reservation.memory_reserved_bytes == 0
        assert reservation.disk_reserved_bytes > 0
        runner_row = (await s.execute(select(SandboxRunnerNode))).scalar_one()
        await s.refresh(runner_row)
        assert runner_row.cpu_reserved_millis == 0
        # 重新启动：重新预留 CPU/内存
        sandbox.desired_state = "stopped"
        await runtime.request_lifecycle(s, workspace, sandbox, "start")
        await s.commit()
        await s.refresh(runner_row)
        assert runner_row.cpu_reserved_millis == sandbox.cpu_request_millis
        assert runner_row.memory_reserved_bytes == sandbox.memory_request_bytes
        await s.refresh(reservation)
        assert reservation.cpu_reserved_millis == sandbox.cpu_request_millis


async def test_delete_releases_all_reservations():
    user = await _user("u7")
    project, workspace = await _project_with_workspace(user)
    runner = await _runner()
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        done = await jobs.complete_job(s, job.id, runner, job.lease_token,
                                       {"observed_state": "ready"})
        await runtime.apply_job_complete(s, done, done.result_json or {})
        await s.commit()
        delete_job = await runtime.request_lifecycle(s, workspace, sandbox, "delete")
        await s.commit()
        claimed_del = await jobs.claim_jobs(s, runner)
        await s.commit()
        target = next(i for i in claimed_del if i.id == delete_job.id)
        done_del = await jobs.complete_job(s, target.id, runner, target.lease_token, {})
        await runtime.apply_job_complete(s, done_del, {})
        await s.commit()
        await s.refresh(sandbox)
        assert sandbox.desired_state == "deleted"
        reservation = (await s.execute(
            select(SandboxResourceReservation).where(
                SandboxResourceReservation.sandbox_id == sandbox.id)
        )).scalar_one()
        await s.refresh(reservation)
        assert reservation.status == "released"
        assert reservation.cpu_reserved_millis == 0
        assert reservation.disk_reserved_bytes == 0
        runner_row = (await s.execute(select(SandboxRunnerNode))).scalar_one()
        await s.refresh(runner_row)
        assert runner_row.cpu_reserved_millis == 0


async def test_quota_and_resource_exhaustion(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_MAX_ACTIVE_PER_USER", 1)
    user = await _user("u8")
    _p1, w1 = await _project_with_workspace(user, "项目1")
    _p2, w2 = await _project_with_workspace(user, "项目2")
    await _runner(cpu_allocatable_millis=600, memory_allocatable_bytes=600 * 1024 * 1024,
                  disk_allocatable_bytes=6 * 1024 ** 3)
    async with AsyncSessionLocal() as s:
        await runtime.provision_workspace(s, w1)
        await s.commit()
        with pytest.raises(SandboxQuotaError):
            await runtime.provision_workspace(s, w2)
        await s.rollback()
    # 资源不足（第二个 Runner 容量小于 standard 请求）
    runner2 = await _runner(
        name="runner-b", cpu_allocatable_millis=100,
        memory_allocatable_bytes=64 * 1024 * 1024,
        disk_allocatable_bytes=1024 ** 3,
    )
    async with AsyncSessionLocal() as s:
        from app.services.sandbox.profiles import get_profile

        profile = await get_profile(s)
        with pytest.raises(SandboxResourceUnavailable):
            await scheduler.select_runner(s, profile, exclude_runner_id=None)


async def test_idle_reclaim_queues_stop(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_IDLE_TIMEOUT", 1)
    user = await _user("u9")
    project, workspace = await _project_with_workspace(user)
    runner = await _runner()
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        done = await jobs.complete_job(s, job.id, runner, job.lease_token,
                                       {"observed_state": "ready"})
        await runtime.apply_job_complete(s, done, done.result_json or {})
        await s.commit()
        await s.refresh(workspace)
        workspace.last_active_at = utcnow() - timedelta(seconds=60)
        profile = await s.get(SandboxResourceProfile, workspace.resource_profile_id)
        profile.idle_timeout_seconds = 1
        await s.commit()
        reclaimed = await runtime.idle_reclaim(s)
        await s.commit()
        assert reclaimed == 1
        stop_jobs = (await s.execute(
            select(SandboxJob).where(SandboxJob.job_type == "stop")
        )).scalars().all()
        assert len(stop_jobs) == 1
        assert stop_jobs[0].idempotency_key.startswith("idle-stop:")


async def test_runner_offline_detection():
    runner = await _runner("runner-stale")
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(SandboxRunnerNode).where(SandboxRunnerNode.id == runner.id)
        )).scalar_one()
        row.last_heartbeat_at = utcnow() - timedelta(hours=1)
        await s.commit()
        count = await runtime.mark_stale_runners_offline(s)
        await s.commit()
        assert count == 1
        await s.refresh(row)
        assert row.status == "offline"
