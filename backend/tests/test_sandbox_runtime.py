"""Workspace Sandbox：Workspace 1:1 / 调度预留 / Job Lease-Fencing / 生命周期 / 回收。

运行：
    cd backend && python -m pytest tests/test_sandbox_runtime.py -q
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone

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
from app.models.agent import AgentRunEvent, AgentSession
from app.services.sandbox import (
    jobs, projects, runtime, scheduler, service_manager, session_files,
)
from app.services.sandbox.common import (
    SandboxAccessDenied, SandboxNotFoundError, SandboxQuotaError,
    SandboxResourceUnavailable, SandboxWriteBusy, sandbox_enabled_for,
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
    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_ROLLOUT_USER_IDS", "")
    monkeypatch.setattr(settings, "SANDBOX_ROLLOUT_PROJECT_IDS", "")
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

async def test_generation_change_interrupts_running_operations():
    user = await _user("u10")
    project, workspace = await _project_with_workspace(user)
    await _runner()
    async with AsyncSessionLocal() as s:
        sandbox = await runtime.provision_workspace(s, workspace)
        s.add(SandboxCommandOperation(
            id="op-old-generation", workspace_id=workspace.id, sandbox_id=sandbox.id,
            sandbox_generation=sandbox.generation, run_id="run-old",
            command="sleep 100", status="running",
        ))
        await s.commit()
        sandbox.observed_state = "failed"
        sandbox.failure_message = "container lost"
        await s.commit()
        renewed = await runtime.provision_workspace(s, workspace)
        await s.commit()
        operation = await s.get(SandboxCommandOperation, "op-old-generation")
        assert renewed.generation == sandbox.generation + 1
        assert operation.status == "interrupted"


async def test_user_file_write_respects_agent_write_lease():
    user = await _user("u11")
    project, workspace, sandbox = await _ready_for_session(user)
    async with AsyncSessionLocal() as s:
        session = AgentSession(
            agent_id=1, user_id=user.id, project_id=project.id,
            title="lease", messages=[],
        )
        s.add(session)
        await s.commit()
        await s.refresh(session)
        from app.services.sandbox.write_lease import acquire_write_lease

        token = await acquire_write_lease(s, workspace.id, "run:busy-run", user_id=user.id)
        await s.commit()
        assert token is not None
        with pytest.raises(SandboxWriteBusy):
            await session_files.acquire_user_write_lease(
                s, session, user.id, purpose="upload")
        await s.rollback()


async def test_member_removal_revokes_agent_tool_access():
    owner = await _user("u12")
    member = await _user("u13")
    project, workspace, sandbox = await _ready_for_session(owner)
    async with AsyncSessionLocal() as s:
        await projects.add_member(s, project.id, owner, member.id, "operator")
        await s.commit()
        from app.services.tools.sandbox_tools import resolve_sandbox

        ref = await resolve_sandbox(
            project.id, user_id=member.id, min_role="operator", wait_seconds=0)
        assert ref.sandbox_id == sandbox.id
        await projects.remove_member(s, project.id, owner, member.id)
        await s.commit()
        with pytest.raises(SandboxAccessDenied):
            await resolve_sandbox(
                project.id, user_id=member.id, min_role="operator", wait_seconds=0)


async def test_rollout_whitelist_blocks_other_users(monkeypatch):
    user = await _user("u14")
    project, workspace = await _project_with_workspace(user)
    monkeypatch.setattr(settings, "SANDBOX_ROLLOUT_USER_IDS", "999999")
    assert sandbox_enabled_for(user.id) is False
    async with AsyncSessionLocal() as s:
        with pytest.raises(SandboxNotFoundError):
            from app.services.sandbox.acl import require_project_access

            await require_project_access(s, project.id, user)
        monkeypatch.setattr(settings, "SANDBOX_ROLLOUT_USER_IDS", str(user.id))
        assert sandbox_enabled_for(user.id) is True


async def test_missing_container_auto_recovery_enqueues_start():
    user = await _user("u15")
    project, workspace, sandbox = await _ready_for_session(user)
    async with AsyncSessionLocal() as s:
        sandbox = await s.get(SandboxInstance, sandbox.id)
        sandbox.observed_state = "failed"
        sandbox.failure_code = "container_missing"
        sandbox.failure_message = "container missing"
        sandbox.last_observed_at = utcnow() - timedelta(minutes=5)
        await s.commit()
        from app.services.sandbox.reconciler import get_reconciler

        summary = await get_reconciler().run_once()
        assert summary["auto_recover"] >= 1
        start_jobs = (await s.execute(
            select(SandboxJob).where(SandboxJob.job_type == "start")
        )).scalars().all()
        assert start_jobs and start_jobs[-1].status == "queued"


async def test_stats_values_parsing_and_usage_metrics():
    from app.sandbox_runner.executor import _stats_values
    from app.observability import metrics

    cpu, memory = _stats_values({
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2_000_000},
            "system_cpu_usage": 10_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000},
            "system_cpu_usage": 8_000_000,
        },
        "memory_stats": {"usage": 300 * 1024 * 1024, "stats": {"cache": 100 * 1024 * 1024}},
    })
    assert cpu == 1.0
    assert memory == 200 * 1024 * 1024
    metrics.set_sandbox_usage(cpu_cores=1.5, memory_bytes=1234, volume_bytes=5678)
    body, _content_type = metrics.render()
    text = body.decode("utf-8")
    assert "sandbox_cpu_usage 1.5" in text
    assert "sandbox_memory_usage_bytes 1234.0" in text
    assert "sandbox_volume_usage_bytes 5678.0" in text


async def _ready_for_session(user: User):
    project, workspace = await _project_with_workspace(user)
    await _runner(name=f"runner-{user.id}")
    async with AsyncSessionLocal() as s:
        workspace = await s.get(Workspace, workspace.id)
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        runner = (await s.execute(select(SandboxRunnerNode))).scalar_one()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        done = await jobs.complete_job(s, job.id, runner, job.lease_token, {
            "container_id": f"c-{sandbox.id}", "container_ip": "10.0.0.9",
            "observed_state": "ready",
        })
        await runtime.apply_job_complete(s, done, done.result_json or {})
        await s.commit()
        await s.refresh(workspace)
        await s.refresh(sandbox)
        return project, workspace, sandbox


async def test_service_state_sync_emits_events_and_updates_health(monkeypatch):
    """Service 状态变化发布 service.healthy / service.exited（任务书 §12.3）。"""
    user = await _user("u-svc")
    project, workspace, sandbox = await _ready_for_session(user)
    async with AsyncSessionLocal() as s:
        service = WorkspaceService(
            workspace_id=workspace.id, sandbox_id=sandbox.id, name="frontend",
            command="npm run dev", working_directory="/workspace", port=5173,
            desired_state="running", observed_state="starting",
        )
        s.add(service)
        await s.commit()

    async def _healthy(_sandbox_id):
        return {"services": [{"name": "frontend", "observed_state": "healthy",
                              "process_id": "4242", "exit_code": None}]}

    monkeypatch.setattr(service_manager.agent_client, "list_services", _healthy)
    async with AsyncSessionLocal() as s:
        workspace_row = await s.get(Workspace, workspace.id)
        sandbox_row = await runtime.active_sandbox(s, workspace.id)
        changed = await service_manager.sync_service_states(s, workspace_row, sandbox_row)
        await s.commit()
        assert changed == 1

    async with AsyncSessionLocal() as s:
        service_row = (await s.execute(select(WorkspaceService))).scalar_one()
        assert service_row.observed_state == "healthy"
        assert service_row.process_id == "4242"
        healthy_events = (await s.execute(
            select(SandboxEvent).where(SandboxEvent.event_type == "service.healthy")
        )).scalars().all()
        assert len(healthy_events) == 1
        assert healthy_events[0].generation == sandbox.generation
        assert healthy_events[0].workspace_id == workspace.id

    async def _missing(_sandbox_id):
        return {"services": []}

    monkeypatch.setattr(service_manager.agent_client, "list_services", _missing)
    async with AsyncSessionLocal() as s:
        workspace_row = await s.get(Workspace, workspace.id)
        sandbox_row = await runtime.active_sandbox(s, workspace.id)
        changed = await service_manager.sync_service_states(s, workspace_row, sandbox_row)
        await s.commit()
        assert changed == 1

    async with AsyncSessionLocal() as s:
        service_row = (await s.execute(select(WorkspaceService))).scalar_one()
        assert service_row.observed_state == "exited"
        exited_events = (await s.execute(
            select(SandboxEvent).where(SandboxEvent.event_type == "service.exited")
        )).scalars().all()
        assert len(exited_events) == 1
        assert exited_events[0].payload.get("reason") == "process_missing"


async def test_service_count_and_port_limits_enforced():
    """Project 级最大 Service 数 / Preview 端口数（任务书 §11.3）。"""
    user = await _user("u-svc-limit")
    _project, workspace, sandbox = await _ready_for_session(user)
    async with AsyncSessionLocal() as s:
        profile = await s.get(SandboxResourceProfile, workspace.resource_profile_id)
        profile.max_services = 1
        profile.max_ports = 1
        s.add(WorkspaceService(
            workspace_id=workspace.id, sandbox_id=sandbox.id, name="exists",
            command="python3 -m http.server 5000", working_directory="/workspace",
            port=5000, desired_state="running", observed_state="healthy",
        ))
        await s.commit()

    async with AsyncSessionLocal() as s:
        workspace_row = await s.get(Workspace, workspace.id)
        sandbox_row = await runtime.active_sandbox(s, workspace.id)
        user_row = await s.get(User, user.id)
        with pytest.raises(SandboxQuotaError):
            await service_manager.create_service(
                s, workspace_row, sandbox_row, user_row,
                name="another", command="python3 -m http.server 5001", port=5001,
            )


async def test_report_sandbox_states_reconciles_and_quarantines_unknown():
    """Runner 上报：已知 Sandbox 更新 Observed State / OOM；无标签容器只告警不接管。"""
    user = await _user("u-report")
    _project, workspace, sandbox = await _ready_for_session(user)
    async with AsyncSessionLocal() as s:
        runner = (await s.execute(select(SandboxRunnerNode))).scalars().first()
        summary = await runtime.report_sandbox_states(s, runner, [
            {
                "sandbox_id": sandbox.id, "workspace_id": workspace.id,
                "observed_state": "failed", "oom_killed": True,
                "container_id": "c-report",
            },
            {"container_id": "unknown-1", "container_name": "gap-sbx-unknown"},
        ])
        await s.commit()
        assert summary == {"updated": 1, "unknown": 1, "oom": 1}

    async with AsyncSessionLocal() as s:
        row = await s.get(SandboxInstance, sandbox.id)
        assert row.observed_state == "failed"
        assert row.failure_code == "oom"
        oom_events = (await s.execute(
            select(SandboxEvent).where(SandboxEvent.event_type == "sandbox.oom")
        )).scalars().all()
        assert len(oom_events) == 1
        unknown_events = (await s.execute(
            select(SandboxEvent).where(
                SandboxEvent.event_type == "sandbox.unknown_container")
        )).scalars().all()
        assert len(unknown_events) == 1
        assert unknown_events[0].payload.get("container_id") == "unknown-1"

    # 重复上报同一次 OOM 不重复计数
    async with AsyncSessionLocal() as s:
        runner = (await s.execute(select(SandboxRunnerNode))).scalars().first()
        summary = await runtime.report_sandbox_states(s, runner, [
            {
                "sandbox_id": sandbox.id, "workspace_id": workspace.id,
                "observed_state": "failed", "oom_killed": True,
            },
        ])
        await s.commit()
        assert summary["oom"] == 0


async def test_volume_soft_quota_warns_once_and_stops(monkeypatch):
    """软磁盘配额：预警一次；超限停止 Sandbox（Volume 保留，任务书 §6.5）。"""
    monkeypatch.setattr(settings, "SANDBOX_VOLUME_QUOTA_MODE", "soft")
    user = await _user("u-vol")
    _project, workspace, sandbox = await _ready_for_session(user)
    quota = int(sandbox.disk_quota_bytes)
    warn_usage = int(quota * 0.9)

    async with AsyncSessionLocal() as s:
        summary = await runtime.check_volume_quotas(
            s, [{"sandbox_id": sandbox.id, "volume_bytes": warn_usage}])
        await s.commit()
        assert summary == {"warned": 1, "stopped": 0}

    async with AsyncSessionLocal() as s:
        warnings = (await s.execute(
            select(SandboxEvent).where(
                SandboxEvent.event_type == "sandbox.volume_quota_warning")
        )).scalars().all()
        assert len(warnings) == 1
        assert warnings[0].payload.get("quota_bytes") == quota
        summary = await runtime.check_volume_quotas(
            s, [{"sandbox_id": sandbox.id, "volume_bytes": warn_usage}])
        await s.commit()
        assert summary == {"warned": 0, "stopped": 0}

    async with AsyncSessionLocal() as s:
        summary = await runtime.check_volume_quotas(
            s, [{"sandbox_id": sandbox.id, "volume_bytes": quota + 1}])
        await s.commit()
        assert summary["stopped"] == 1
        row = await s.get(SandboxInstance, sandbox.id)
        assert row.desired_state == "stopped"
        exceeded = (await s.execute(
            select(SandboxEvent).where(
                SandboxEvent.event_type == "sandbox.volume_quota_exceeded")
        )).scalars().all()
        assert len(exceeded) == 1
        stop_jobs = (await s.execute(
            select(SandboxJob).where(
                SandboxJob.sandbox_id == sandbox.id, SandboxJob.job_type == "stop")
        )).scalars().all()
        assert len(stop_jobs) == 1

    # 重复上报不得重复停止 / 重复事件
    async with AsyncSessionLocal() as s:
        summary = await runtime.check_volume_quotas(
            s, [{"sandbox_id": sandbox.id, "volume_bytes": quota + 1}])
        await s.commit()
        assert summary == {"warned": 0, "stopped": 0}


async def test_quota_denied_emits_resource_event(monkeypatch):
    """配额拒绝发布 resource.denied，前端可见（任务书 §12.3）。"""
    monkeypatch.setattr(settings, "SANDBOX_MAX_ACTIVE_PER_USER", 1)
    user = await _user("u-deny")
    _p1, w1 = await _project_with_workspace(user, "配额项目1")
    _p2, w2 = await _project_with_workspace(user, "配额项目2")
    await _runner(name="runner-deny")
    async with AsyncSessionLocal() as s:
        await runtime.provision_workspace(s, w1)
        await s.commit()
    async with AsyncSessionLocal() as s:
        with pytest.raises(SandboxQuotaError):
            await runtime.provision_workspace(s, w2)
        await s.rollback()
    async with AsyncSessionLocal() as s:
        events = (await s.execute(
            select(SandboxEvent).where(SandboxEvent.event_type == "resource.denied")
        )).scalars().all()
        assert len(events) == 1
        assert events[0].project_id == _p2.id
        assert events[0].payload.get("reason") == "quota"


async def test_completion_evidence_reads_sandbox_workspace(monkeypatch):
    """项目会话验收证据必须从 Sandbox /workspace 读取，避免误报“没有写入记录”。"""
    from app.services.agent_runtime import completion
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.agent_runtime.events import TOOL_COMPLETED, TOOL_STARTED
    from app.services.sandbox import agent_client

    user = await _user("u-evidence")
    project, _workspace, sandbox = await _ready_for_session(user)
    run_id = "run-sbx-evidence"
    raw = "# 调研报告\n\n正文内容\n".encode()
    digest = hashlib.sha256(raw).hexdigest()

    async def _read_raw(sandbox_id, path):
        assert sandbox_id == sandbox.id
        assert path == "/workspace/software_report.md"
        return {"path": path, "size": len(raw),
                "content_b64": base64.b64encode(raw).decode()}

    monkeypatch.setattr(agent_client, "read_raw", _read_raw)
    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    async with AsyncSessionLocal() as s:
        s.add_all([
            AgentRunEvent(run_id=run_id, sequence=1, event_type=TOOL_STARTED, payload={
                "tool": "write_file", "tool_call_id": "call-w1", "internal": True,
                "input": {"file_path": "/workspace/software_report.md"}}),
            AgentRunEvent(run_id=run_id, sequence=2, event_type=TOOL_COMPLETED, payload={
                "tool": "write_file", "tool_call_id": "call-w1", "internal": True,
                "output": "written"}),
        ])
        await s.commit()
    try:
        ctx = RuntimeContext(
            RunScope(user.id), run_id, "thread-evidence", 1,
            session_id=1, project_id=project.id,
        )
        evidence, artifacts, external = await completion._collect_evidence(ctx)
        assert external is False
        assert any(item.tool_name == "write_file" and item.path == "software_report.md"
                   for item in evidence)
        assert {"path": "software_report.md", "name": "software_report.md",
                "size": len(raw), "sha256": digest} in artifacts
        assert completion.delivery_path_checks(
            "报告已保存 /workspace/software_report.md，全文如下……", evidence, artifacts) == []
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
            await s.commit()


async def test_stopped_service_can_restart_with_updated_command(monkeypatch):
    """同名同端口服务停止后可用新命令重启（修正 --host 等启动参数）。"""
    from app.services.sandbox import agent_client

    user = await _user("u-svc-update")
    _project, workspace, _sandbox = await _ready_for_session(user)
    calls: list[str] = []

    async def _start(_sandbox_id, config):
        calls.append(config["command"])
        return {"observed_state": "starting", "process_id": "111"}

    monkeypatch.setattr(agent_client, "start_service", _start)
    async with AsyncSessionLocal() as s:
        workspace_row = await s.get(Workspace, workspace.id)
        sandbox_row = await runtime.active_sandbox(s, workspace.id)
        service = await service_manager.create_service(
            s, workspace_row, sandbox_row, user,
            name="frontend", command="npm run dev", port=5173,
        )
        await s.commit()
        assert service.observed_state == "starting"
        service.desired_state = "stopped"
        service.observed_state = "stopped"
        await s.commit()
        updated = await service_manager.create_service(
            s, workspace_row, sandbox_row, user,
            name="frontend", command="npm run dev -- --host 0.0.0.0", port=5173,
        )
        await s.commit()
    assert updated.id == service.id
    assert updated.command.endswith("--host 0.0.0.0")
    assert calls == ["npm run dev", "npm run dev -- --host 0.0.0.0"]


async def test_stop_all_services_marks_stopped_and_cleans_orphans(monkeypatch):
    """停止全部服务：数据库状态收口，并调用 Agent 清理脱离进程组的残留进程。"""
    from app.services.sandbox import agent_client

    user = await _user("u-svc-stopall")
    _project, workspace, _sandbox = await _ready_for_session(user)
    calls: list[int] = []

    async def _start(_sandbox_id, _config):
        return {"observed_state": "starting", "process_id": "111"}

    async def _stop_all(sandbox_id):
        calls.append(int(sandbox_id))
        return {"services": [], "killed_pids": [123, 456]}

    monkeypatch.setattr(agent_client, "start_service", _start)
    monkeypatch.setattr(agent_client, "stop_all_services", _stop_all)
    async with AsyncSessionLocal() as s:
        workspace_row = await s.get(Workspace, workspace.id)
        sandbox_row = await runtime.active_sandbox(s, workspace.id)
        await service_manager.create_service(
            s, workspace_row, sandbox_row, user,
            name="frontend", command="npm run dev", port=5173,
        )
        await service_manager.create_service(
            s, workspace_row, sandbox_row, user,
            name="backend", command="npm run dev", port=3001,
        )
        await s.commit()
        services, killed = await service_manager.stop_all_services(s, workspace_row, user)
        await s.commit()

    assert killed == [123, 456]
    assert len(calls) == 1
    assert {svc.desired_state for svc in services} == {"stopped"}
    assert {svc.observed_state for svc in services} == {"stopped"}


async def test_directory_listing_for_lazy_tree(monkeypatch):
    """按目录懒加载：返回直接子项（目录+文件），排除平台内部目录，不按总数截断。"""
    from app.services.sandbox import agent_client

    user = await _user("u-tree")
    project, _workspace, _sandbox = await _ready_for_session(user)
    async with AsyncSessionLocal() as s:
        session = AgentSession(
            agent_id=1, user_id=user.id, title="树", messages=[], project_id=project.id,
        )
        s.add(session)
        await s.commit()
        await s.refresh(session)

    async def _list_dir(_sandbox_id, path):
        assert path == "/workspace"
        return {"entries": [
            {"name": "frontend", "path": "/workspace/frontend", "is_dir": True},
            {"name": "node_modules", "path": "/workspace/node_modules", "is_dir": True},
            {"name": "report.md", "path": "/workspace/report.md", "is_dir": False,
             "size": 12, "modified_at": "2026-11-27T00:00:00+00:00"},
            {"name": ".sandbox", "path": "/workspace/.sandbox", "is_dir": True},
            {"name": "tool-outputs", "path": "/workspace/tool-outputs", "is_dir": True},
        ]}

    monkeypatch.setattr(agent_client, "list_dir", _list_dir)
    async with AsyncSessionLocal() as s:
        entries = await session_files.list_directory(s, session, "")
        await s.commit()
    assert [entry["name"] for entry in entries] == ["frontend", "node_modules", "report.md"]
    assert entries[-1]["size"] == 12
    assert entries[-1]["mtime"] > 0


async def test_attachment_refs_read_from_sandbox_workspace(monkeypatch):
    """项目会话附件指纹从 Sandbox 读取（否则 run 创建会误报附件不存在）。"""
    from app.services.agent_runtime.task_router import resolve_attachment_refs_async
    from app.services.sandbox import agent_client

    user = await _user("u-attach")
    project, _workspace, _sandbox = await _ready_for_session(user)
    note = "附件正文\n".encode()
    extracted = "附件正文\n".encode()

    async def _read_raw(_sandbox_id, path):
        payload = extracted if path.endswith(".extracted.txt") else note
        return {"path": path, "size": len(payload),
                "content_b64": base64.b64encode(payload).decode()}

    monkeypatch.setattr(agent_client, "read_raw", _read_raw)
    async with AsyncSessionLocal() as s:
        session = AgentSession(
            agent_id=1, user_id=user.id, title="附件会话",
            messages=[], project_id=project.id,
        )
        s.add(session)
        await s.commit()
        await s.refresh(session)
    refs = await resolve_attachment_refs_async(
        [{"path": "uploads/note.txt",
          "text_path": "uploads/note.txt.extracted.txt"}],
        user_id=user.id, agent_id=1, session_id=session.id,
    )
    assert refs[0]["path"] == "uploads/note.txt"
    assert refs[0]["size"] == len(note)
    assert refs[0]["sha256"] == hashlib.sha256(note).hexdigest()
    assert refs[0]["text_path"] == "uploads/note.txt.extracted.txt"
    assert refs[0]["text_sha256"] == hashlib.sha256(extracted).hexdigest()


async def test_cleanup_orphan_networks_and_subnet_allocation(monkeypatch):
    """孤儿 Sandbox 网络自动清理；配置子网池时按块分配，避免地址池耗尽。"""
    from app.sandbox_runner.executor import RunnerExecutor

    def _created(seconds_ago: float) -> str:
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    class FakeEngine:
        def __init__(self):
            self.removed: list[str] = []
            self.disconnected: list[tuple[str, str]] = []

        async def list_containers(self, _labels):
            return [{"Labels": {"platform.network": "gap-net-1-g1"}}]

        async def list_networks(self, _labels):
            return [
                {"Name": "gap-net-1-g1", "Created": _created(3600)},
                {"Name": "gap-net-2-g1", "Created": _created(3600)},
                {"Name": "gap-net-3-g1", "Created": _created(3600)},
                {"Name": "gap-net-4-g1", "Created": _created(5)},
                {"Name": "gap-net-5-g1", "Created": _created(3600),
                 "IPAM": {"Config": [{"Subnet": "10.210.0.0/24"}]}},
            ]

        async def inspect_network(self, name):
            if name == "gap-net-2-g1":
                return {"Containers": {"runner-1": {"Name": "runner"}}}
            if name == "gap-net-3-g1":
                return {"Containers": {"other": {"Name": "other"}}}
            return {"Containers": {}}

        async def disconnect_network(self, network, container):
            self.disconnected.append((network, container))

        async def remove_network(self, name):
            self.removed.append(name)

    monkeypatch.setattr(settings, "SANDBOX_RUNNER_CONTAINER", "runner-1")
    monkeypatch.setattr(settings, "SANDBOX_NETWORK_SUBNET_POOL", "10.210.0.0/16")
    monkeypatch.setattr(settings, "SANDBOX_NETWORK_SUBNET_PREFIX", 24)
    engine = FakeEngine()
    executor = RunnerExecutor(engine)
    removed = await executor.cleanup_orphan_networks()
    assert removed == 2
    assert engine.removed == ["gap-net-2-g1", "gap-net-5-g1"]
    assert engine.disconnected == [("gap-net-2-g1", "runner-1")]
    assert await executor._allocate_subnet() == "10.210.1.0/24"
