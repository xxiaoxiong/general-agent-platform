"""Workspace Sandbox：ACL / Preview Grant / 工具策略 / 文件后端映射。"""
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
from app.services.sandbox import (
    acl, jobs, preview as preview_service, projects, runtime,
)
from app.services.sandbox.common import (
    SandboxAccessDenied, SandboxNotFoundError, utcnow,
)
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
        await s.execute(delete(User).where(User.username.like("sbxsec-%")))
        await s.execute(delete(SandboxResourceProfile))
        await s.commit()
        await ensure_default_profiles(s)
        await s.commit()
    yield
    await engine.dispose()


async def _user(name: str, superuser: bool = False) -> User:
    async with AsyncSessionLocal() as s:
        user = User(
            username=f"sbxsec-{name}", email=f"sbxsec-{name}@test.dev",
            hashed_password="x", is_active=True, is_superuser=superuser,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _ready_sandbox(user: User):
    async with AsyncSessionLocal() as s:
        project, workspace = await projects.create_project(s, user, f"项目-{user.id}")
        runner = await runtime.register_runner(s, {
            "node_name": f"runner-{user.id}",
            "cpu_total_millis": 8000, "cpu_allocatable_millis": 8000,
            "memory_total_bytes": 16 * 1024 ** 3,
            "memory_allocatable_bytes": 16 * 1024 ** 3,
            "disk_total_bytes": 100 * 1024 ** 3,
            "disk_allocatable_bytes": 100 * 1024 ** 3,
        })
        sandbox = await runtime.provision_workspace(s, workspace)
        await s.commit()
        claimed = await jobs.claim_jobs(s, runner)
        await s.commit()
        job = claimed[0]
        done = await jobs.complete_job(s, job.id, runner, job.lease_token, {
            "container_id": "c1", "container_ip": "10.0.0.2",
            "observed_state": "ready",
        })
        await runtime.apply_job_complete(s, done, done.result_json or {})
        await s.commit()
        await s.refresh(workspace)
        await s.refresh(sandbox)
        return project, workspace, sandbox


async def _make_service(workspace: Workspace, sandbox: SandboxInstance, name="web", port=5173):
    async with AsyncSessionLocal() as s:
        service = WorkspaceService(
            workspace_id=workspace.id, sandbox_id=sandbox.id, name=name,
            command="npm run dev", working_directory="/workspace", port=port,
            desired_state="running", observed_state="healthy",
        )
        s.add(service)
        await s.commit()
        await s.refresh(service)
        return service


async def test_project_acl_roles_and_member_removal():
    owner = await _user("owner")
    outsider = await _user("outsider")
    project, _workspace = await _project_pair(owner)
    async with AsyncSessionLocal() as s:
        with pytest.raises(SandboxNotFoundError):
            await acl.require_project_access(s, project.id, outsider)
        await projects.add_member(s, project.id, owner, outsider.id, "viewer")
        await s.commit()
        _p, role = await acl.require_project_access(s, project.id, outsider)
        assert role == "viewer"
        with pytest.raises(SandboxAccessDenied):
            await acl.require_project_access(s, project.id, outsider, "operator")
        await projects.add_member(s, project.id, owner, outsider.id, "operator")
        await s.commit()
        _p, role = await acl.require_project_access(s, project.id, outsider, "operator")
        assert role == "operator"
        with pytest.raises(SandboxAccessDenied):
            await acl.require_project_access(s, project.id, outsider, "admin")
        await projects.remove_member(s, project.id, owner, outsider.id)
        await s.commit()
        with pytest.raises(SandboxNotFoundError):
            await acl.require_project_access(s, project.id, outsider)


async def _project_pair(user: User):
    async with AsyncSessionLocal() as s:
        project, workspace = await projects.create_project(s, user, f"ACL-{user.id}")
        await s.commit()
        await s.refresh(project)
        await s.refresh(workspace)
        return project, workspace


async def test_preview_grant_expiry_revoke_and_service_stop():
    user = await _user("preview")
    project, workspace, sandbox = await _ready_sandbox(user)
    service = await _make_service(workspace, sandbox)
    async with AsyncSessionLocal() as s:
        service = await s.get(WorkspaceService, service.id)
        workspace = await s.get(Workspace, workspace.id)
        grant, token = await preview_service.create_grant(s, workspace, service, user)
        await s.commit()
        assert token and grant.token_hash
        urls = preview_service.build_preview_urls(token)
        assert token in urls["url"] and token in urls["fallback_url"]
        _grant, _sandbox, _service = await preview_service.resolve_grant(s, token)
        await s.commit()
        # 过期
        grant.expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()
        with pytest.raises(preview_service.PreviewDenied) as exc:
            await preview_service.resolve_grant(s, token)
        assert exc.value.reason == "grant_expired"
        # 撤销
        grant.expires_at = None
        grant.revoked_at = utcnow()
        await s.commit()
        with pytest.raises(preview_service.PreviewDenied) as revoked:
            await preview_service.resolve_grant(s, token)
        assert revoked.value.reason == "grant_revoked"
        # 服务停止后拒绝
        grant.revoked_at = None
        service.desired_state = "stopped"
        service.observed_state = "stopped"
        await s.commit()
        with pytest.raises(preview_service.PreviewDenied) as stopped:
            await preview_service.resolve_grant(s, token)
        assert stopped.value.reason in ("service_stopped", "service_unavailable")


async def test_member_removal_revokes_their_grants():
    owner = await _user("owner2")
    member = await _user("member2")
    project, workspace, sandbox = await _ready_sandbox(owner)
    service = await _make_service(workspace, sandbox, name="api", port=8000)
    async with AsyncSessionLocal() as s:
        await projects.add_member(s, project.id, owner, member.id, "operator")
        await s.commit()
        grant, _token = await preview_service.create_grant(s, workspace, service, member)
        await s.commit()
        await projects.remove_member(s, project.id, owner, member.id)
        await s.commit()
        await s.refresh(grant)
        assert grant.revoked_at is not None


async def test_tool_policy_risk_registration_and_exec_gate():
    from app.services.agent_runtime.tools.policy import (
        ToolPolicy, ToolPolicyError, get_tool_risk,
    )

    for name in ("execute_command", "start_service", "list_services", "get_preview_url"):
        assert get_tool_risk(name) is not None
    assert get_tool_risk("execute_command").requires_approval is True
    restricted = ToolPolicy(allowed_tools={"execute_command"}, sandbox_mode="restricted")
    with pytest.raises(ToolPolicyError):
        restricted.check_exec_allowed("execute_command")
    external = ToolPolicy(allowed_tools={"execute_command"}, sandbox_mode="external")
    external.check_exec_allowed("execute_command")
    # 未登记工具 fail-closed
    with pytest.raises(ToolPolicyError):
        ToolPolicy(allowed_tools={"unknown_tool"}).check_tool_allowed("unknown_tool")


class _Ctx:
    def __init__(self, user_id: int, run_id: str = "run-1", project_id: int = 1):
        self.scope = type("Scope", (), {"user_id": user_id})()
        self.run_id = run_id
        self.project_id = project_id
        self.agent_config = {}


async def test_execute_command_tool_persists_operation(monkeypatch):
    from app.services.tools import sandbox_tools
    from app.services.tools.result import STATUS_SUCCEEDED

    user = await _user("tool")
    project, workspace, sandbox = await _ready_sandbox(user)

    async def fake_resolve(project_id, *, user_id=None, min_role="operator",
                           wait_seconds=25.0):
        return sandbox_tools.SandboxRef(sandbox.id, sandbox.generation, workspace.id, project.id)

    async def fake_exec(sandbox_id, command, **kwargs):
        return {
            "operation_id": "op-123", "status": "succeeded", "exit_code": 0,
            "stdout": "hello\n", "stderr": "", "truncated": False,
        }

    monkeypatch.setattr(sandbox_tools, "resolve_sandbox", fake_resolve)
    monkeypatch.setattr(sandbox_tools.agent_client, "exec_command", fake_exec)
    ctx = _Ctx(user.id, run_id="run-tool", project_id=project.id)
    result = await sandbox_tools.handle_execute_command(ctx, {"command": "echo hello"})
    assert result.status == STATUS_SUCCEEDED
    assert "hello" in str(result.output)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(SandboxCommandOperation).where(SandboxCommandOperation.id == "op-123")
        )).scalar_one_or_none()
        assert row is not None
        assert row.status == "succeeded"
        assert row.run_id == "run-tool"


async def test_tool_executor_routes_context_handlers(monkeypatch):
    from app.services.tools import ToolExecutor
    from app.services.tools import __dict__ as tools_module  # noqa: F401
    import app.services.tools as tools_pkg
    from app.services.tools.result import ToolResult, STATUS_SUCCEEDED

    captured = {}

    async def fake_handler(ctx, params):
        captured["ctx"] = ctx
        captured["params"] = params
        return ToolResult.succeeded("ok")

    monkeypatch.setitem(tools_pkg._CONTEXT_HANDLERS, "list_services", fake_handler)
    ctx = _Ctx(1)
    result = await ToolExecutor(None, {}, ctx).execute("list_services", {})
    normalized = result
    assert getattr(normalized, "status", None) == STATUS_SUCCEEDED
    assert captured["ctx"] is ctx


async def test_preview_rate_limit(monkeypatch):
    from app.preview_proxy.app import _rate_allowed

    monkeypatch.setattr(settings, "PREVIEW_RATE_LIMIT_PER_MINUTE", 2)
    grant_id = "rate-test-grant"
    assert await _rate_allowed(grant_id) is True
    assert await _rate_allowed(grant_id) is True
    assert await _rate_allowed(grant_id) is False


async def test_preview_login_token_binds_grant_and_project_acl():
    from app.preview_proxy.app import _preview_user_allowed
    from app.services.sandbox.preview import create_preview_access_token

    owner = await _user("preview-owner")
    outsider = await _user("preview-outsider")
    _project, workspace = await _project_pair(owner)
    target = {"grant_id": "grant-login-1", "workspace_id": workspace.id}
    token = create_preview_access_token(target["grant_id"], owner.id, 600)
    assert await _preview_user_allowed(target, token) is True
    outsider_token = create_preview_access_token(target["grant_id"], outsider.id, 600)
    assert await _preview_user_allowed(target, outsider_token) is False
    assert await _preview_user_allowed(
        {"grant_id": "other-grant", "workspace_id": workspace.id}, token) is False
    assert await _preview_user_allowed(target, "invalid-token") is False


async def test_sandbox_files_backend_maps_workspace_and_blocks_host_paths(monkeypatch):
    from app.services.agent_runtime.sandbox_backend import SandboxFilesBackend

    backend = SandboxFilesBackend(project_id=1, run_id="run-1", user_id=1)

    async def fake_sandbox(min_role="viewer"):
        return type("Ref", (), {"sandbox_id": 9, "generation": 1,
                                "workspace_id": 3, "project_id": 1})()

    async def no_lease(ref):
        return None

    writes = {}

    async def fake_write(sandbox_id, path, content, **kwargs):
        writes[path] = content
        return {"path": path, "size": len(content)}

    async def fake_read(sandbox_id, path, **kwargs):
        return {
            "path": path, "encoding": "utf-8", "content": "line1\nline2",
            "total_lines": 2, "start_line": 1, "end_line": 2, "next_offset": None,
        }

    async def fake_list(sandbox_id, path="/workspace"):
        return {"path": path, "entries": [
            {"path": "/workspace/a.py", "name": "a.py", "is_dir": False,
             "size": 10, "modified_at": "2026-01-01T00:00:00+00:00"},
        ], "truncated": False}

    monkeypatch.setattr(backend, "_sandbox", fake_sandbox)
    monkeypatch.setattr(backend, "_lease_error", no_lease)
    import app.services.agent_runtime.sandbox_backend as sb

    monkeypatch.setattr(sb.agent_client, "write_file", fake_write)
    monkeypatch.setattr(sb.agent_client, "read_file", fake_read)
    monkeypatch.setattr(sb.agent_client, "list_dir", fake_list)

    write_result = await backend.awrite("/a.py", "print('x')")
    assert write_result.error is None
    assert writes["/workspace/a.py"] == "print('x')"
    read_result = await backend.aread("/a.py")
    assert read_result.error is None
    ls_result = await backend.als("/")
    assert ls_result.entries and ls_result.entries[0]["path"] == "/workspace/a.py"
    blocked = await backend.awrite("/Users/alice/report.md", "x")
    assert blocked.error and "用户电脑" in blocked.error
