"""Workspace Sandbox Runtime 数据模型（任务书 §5）。

对象关系：
  Project 1:1 Workspace；Workspace 1:N Sandbox Generation / Agent Run / Service；
  Runner 1:N Sandbox；Sandbox Job 指向 Workspace / Sandbox / Runner。

设计约束：
- Desired State 与 Observed State 分离；
- 所有资源预留有数据库权威记录，不只存在 Runner 内存；
- Job 领取用条件 UPDATE + Fencing Token，至少一次投递、执行幂等；
- Volume ID 由服务端生成（``gap-ws-{opaque}``），不接受用户指定。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, false, func, true,
)

from app.models.base import Base

# ── Workspace / Sandbox 状态口径（任务书 §5.1 / §5.2）──
WORKSPACE_STATUSES = ("provisioning", "ready", "degraded", "deleting", "deleted")
SANDBOX_DESIRED_STATES = ("running", "stopped", "deleted")
SANDBOX_OBSERVED_STATES = (
    "queued", "provisioning", "starting", "ready", "stopping", "stopped",
    "failed", "recovering",
)
RUNNER_STATUSES = ("online", "offline", "draining")
JOB_TYPES = ("provision", "start", "stop", "restart", "delete", "reconcile")
JOB_STATUSES = ("queued", "leased", "running", "succeeded", "failed", "cancelled")
JOB_QUEUE_KINDS = {
    # 交互任务优先于批量构建（任务书 §6.4）
    "provision": "provision",
    "start": "lifecycle",
    "stop": "lifecycle",
    "restart": "lifecycle",
    "reconcile": "recovery",
    "delete": "cleanup",
}
RESERVATION_STATUSES = ("reserved", "active", "releasing", "released")
SERVICE_DESIRED_STATES = ("running", "stopped")
SERVICE_OBSERVED_STATES = (
    "created", "starting", "healthy", "unhealthy", "exited", "failed",
    "stopping", "stopped",
)
PROJECT_ROLES = ("viewer", "operator", "admin")


class Project(Base):
    """项目：Workspace 的长期归属边界（任务书 §1.1 / §5.1）。"""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, default="")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = Column(String(64), nullable=True, index=True)
    status = Column(String(24), default="active", nullable=False, server_default="active")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_projects_owner_status", "owner_id", "status"),
    )


class ProjectMember(Base):
    """项目成员（viewer / operator / admin）。

    现有平台只有平台级 RBAC，没有 Project 级 ACL；此处按任务书 §11.1 补最小
    项目成员模型，owner 隐式 admin，不另建与平台 RBAC 冲突的全局权限体系。
    """

    __tablename__ = "project_members"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(16), default="operator", nullable=False, server_default="operator")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )


class SandboxResourceProfile(Base):
    """资源档位（任务书 §6.1）：不允许 Agent 自由决定 CPU / RAM / Disk。"""

    __tablename__ = "sandbox_resource_profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(48), nullable=False, unique=True)
    display_name = Column(String(80), default="")
    cpu_request_millis = Column(Integer, nullable=False, default=500)
    cpu_limit_millis = Column(Integer, nullable=False, default=1000)
    memory_request_bytes = Column(BigInteger, nullable=False, default=512 * 1024 * 1024)
    memory_limit_bytes = Column(BigInteger, nullable=False, default=1024 * 1024 * 1024)
    disk_quota_bytes = Column(BigInteger, nullable=False, default=5 * 1024 * 1024 * 1024)
    pid_limit = Column(Integer, nullable=False, default=256)
    max_processes = Column(Integer, nullable=False, default=128)
    max_services = Column(Integer, nullable=False, default=4)
    max_ports = Column(Integer, nullable=False, default=4)
    idle_timeout_seconds = Column(Integer, nullable=False, default=1800)
    network_policy = Column(String(24), default="egress", nullable=False, server_default="egress")
    is_default = Column(Boolean, nullable=False, default=False, server_default=false())
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())


class Workspace(Base):
    """Project 对应的持久化工作空间（1:1，任务书 §5.1）。"""

    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, unique=True, index=True)
    tenant_id = Column(String(64), nullable=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(24), default="provisioning", nullable=False, server_default="provisioning")
    volume_id = Column(String(128), nullable=False, unique=True)
    volume_driver = Column(String(32), nullable=False, default="local", server_default="local")
    storage_quota_bytes = Column(BigInteger, nullable=False, default=5 * 1024 * 1024 * 1024)
    resource_profile_id = Column(Integer, ForeignKey("sandbox_resource_profiles.id"), nullable=True)
    # 当前活动 Sandbox（不设外键：Sandbox 行创建时 Workspace 已存在，避免循环依赖）
    current_sandbox_id = Column(Integer, nullable=True)
    # 写租约（任务书 §3.2）：同一 Workspace 默认只允许一个写 Run
    write_lease_owner = Column(String(128), nullable=True)
    write_lease_token = Column(Integer, nullable=False, default=0, server_default="0")
    write_lease_expires_at = Column(DateTime, nullable=True)
    write_lease_heartbeat_at = Column(DateTime, nullable=True)
    last_active_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())
    deleted_at = Column(DateTime, nullable=True)


class SandboxRunnerNode(Base):
    """Sandbox Runner 注册与资源账本（任务书 §5.3 / §6.2）。"""

    __tablename__ = "sandbox_runners"

    id = Column(Integer, primary_key=True, index=True)
    node_name = Column(String(128), nullable=False, unique=True)
    status = Column(String(16), default="online", nullable=False, server_default="online")
    runtime_provider = Column(String(32), default="docker", nullable=False, server_default="docker")
    runtime_version = Column(String(64), default="")
    cpu_total_millis = Column(Integer, nullable=False, default=0)
    cpu_allocatable_millis = Column(Integer, nullable=False, default=0)
    cpu_reserved_millis = Column(Integer, nullable=False, default=0, server_default="0")
    memory_total_bytes = Column(BigInteger, nullable=False, default=0)
    memory_allocatable_bytes = Column(BigInteger, nullable=False, default=0)
    memory_reserved_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    disk_total_bytes = Column(BigInteger, nullable=False, default=0)
    disk_allocatable_bytes = Column(BigInteger, nullable=False, default=0)
    disk_reserved_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    active_sandbox_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_heartbeat_at = Column(DateTime, nullable=True)
    capabilities_json = Column(JSON, default=dict)
    version = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())


class SandboxInstance(Base):
    """Sandbox Generation（任务书 §5.2）：容器可重建，Generation 单调递增。"""

    __tablename__ = "sandbox_instances"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    generation = Column(Integer, nullable=False, default=1)
    runner_id = Column(Integer, ForeignKey("sandbox_runners.id"), nullable=True, index=True)
    container_id = Column(String(128), nullable=True, index=True)
    container_name = Column(String(128), nullable=True, unique=True)
    container_ip = Column(String(64), nullable=True)
    image_digest = Column(String(160), nullable=True)
    runtime_provider = Column(String(32), default="docker", nullable=False, server_default="docker")
    desired_state = Column(String(16), default="running", nullable=False, server_default="running")
    observed_state = Column(String(24), default="queued", nullable=False, server_default="queued")
    # 每次 start/stop/restart 请求自增，用于生命周期 Job 的幂等键
    lifecycle_version = Column(Integer, nullable=False, default=0, server_default="0")
    cpu_request_millis = Column(Integer, nullable=False, default=500)
    cpu_limit_millis = Column(Integer, nullable=False, default=1000)
    memory_request_bytes = Column(BigInteger, nullable=False, default=512 * 1024 * 1024)
    memory_limit_bytes = Column(BigInteger, nullable=False, default=1024 * 1024 * 1024)
    disk_quota_bytes = Column(BigInteger, nullable=False, default=5 * 1024 * 1024 * 1024)
    pid_limit = Column(Integer, nullable=False, default=256)
    last_heartbeat_at = Column(DateTime, nullable=True)
    last_observed_at = Column(DateTime, nullable=True)
    failure_code = Column(String(64), nullable=True)
    failure_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "generation", name="uq_sandbox_ws_generation"),
        Index("ix_sandbox_ws_observed", "workspace_id", "observed_state"),
    )


class SandboxJob(Base):
    """基础设施任务队列（任务书 §5.4）：至少一次投递，执行必须幂等。"""

    __tablename__ = "sandbox_jobs"

    id = Column(String(32), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    sandbox_id = Column(Integer, ForeignKey("sandbox_instances.id"), nullable=True, index=True)
    target_runner_id = Column(Integer, ForeignKey("sandbox_runners.id"), nullable=True, index=True)
    job_type = Column(String(24), nullable=False, index=True)
    status = Column(String(16), default="queued", nullable=False, server_default="queued", index=True)
    priority = Column(Integer, nullable=False, default=100, server_default="100")
    idempotency_key = Column(String(160), nullable=False, unique=True)
    attempt = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    lease_owner = Column(String(128), nullable=True)
    lease_token = Column(Integer, nullable=False, default=0, server_default="0")
    lease_expires_at = Column(DateTime, nullable=True)
    payload_json = Column(JSON, default=dict)
    result_json = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())

    __table_args__ = (
        # 队列扫描：status + priority + created_at（Aging 由 priority 递减逼近）
        Index("ix_sandbox_jobs_queue", "status", "priority", "created_at"),
        Index("ix_sandbox_jobs_lease", "status", "lease_expires_at"),
    )


class SandboxResourceReservation(Base):
    """Runner 上的资源预留（任务书 §5.5 / §6.3）。"""

    __tablename__ = "sandbox_resource_reservations"

    id = Column(Integer, primary_key=True, index=True)
    runner_id = Column(Integer, ForeignKey("sandbox_runners.id"), nullable=False, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    sandbox_id = Column(Integer, nullable=True, index=True)
    cpu_reserved_millis = Column(Integer, nullable=False, default=0)
    memory_reserved_bytes = Column(BigInteger, nullable=False, default=0)
    disk_reserved_bytes = Column(BigInteger, nullable=False, default=0)
    status = Column(String(16), default="reserved", nullable=False, server_default="reserved")
    reservation_token = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    released_at = Column(DateTime, nullable=True)


class WorkspaceService(Base):
    """Sandbox 内长期运行的服务（任务书 §5.6）。"""

    __tablename__ = "workspace_services"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    sandbox_id = Column(Integer, ForeignKey("sandbox_instances.id"), nullable=True, index=True)
    name = Column(String(80), nullable=False)
    command = Column(Text, nullable=False)
    working_directory = Column(String(255), nullable=False, default="/workspace", server_default="/workspace")
    port = Column(Integer, nullable=False)
    protocol = Column(String(16), nullable=False, default="http", server_default="http")
    health_check_type = Column(String(16), nullable=False, default="http", server_default="http")
    health_check_path = Column(String(255), nullable=False, default="/", server_default="/")
    health_check_timeout = Column(Integer, nullable=False, default=60, server_default="60")
    desired_state = Column(String(16), nullable=False, default="running", server_default="running")
    observed_state = Column(String(24), nullable=False, default="created", server_default="created")
    auto_restart = Column(Boolean, nullable=False, default=True, server_default=true())
    auto_start_on_sandbox_start = Column(Boolean, nullable=False, default=True, server_default=true())
    process_id = Column(String(64), nullable=True)
    exit_code = Column(Integer, nullable=True)
    last_started_at = Column(DateTime, nullable=True)
    last_exited_at = Column(DateTime, nullable=True)
    log_reference = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_workspace_service_name"),
        UniqueConstraint("workspace_id", "port", name="uq_workspace_service_port"),
    )


class PreviewGrant(Base):
    """Preview 授权（任务书 §5.7）：只保存 Token Hash，绑定 Sandbox + Port。"""

    __tablename__ = "preview_grants"

    id = Column(String(32), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    sandbox_id = Column(Integer, ForeignKey("sandbox_instances.id"), nullable=False, index=True)
    service_id = Column(Integer, ForeignKey("workspace_services.id"), nullable=False, index=True)
    port = Column(Integer, nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())
    last_accessed_at = Column(DateTime, nullable=True)


class SandboxCommandOperation(Base):
    """命令操作（任务书 §8.4）：短命令同步返回，长命令异步查询/取消。"""

    __tablename__ = "sandbox_command_operations"

    id = Column(String(32), primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    sandbox_id = Column(Integer, ForeignKey("sandbox_instances.id"), nullable=True, index=True)
    sandbox_generation = Column(Integer, nullable=False, default=0)
    run_id = Column(String(36), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    command = Column(Text, nullable=False)
    working_directory = Column(String(255), nullable=False, default="/workspace")
    status = Column(String(16), nullable=False, default="running", server_default="running")
    exit_code = Column(Integer, nullable=True)
    stdout_text = Column(Text, nullable=True)
    stderr_text = Column(Text, nullable=True)
    truncated = Column(Boolean, nullable=False, default=False, server_default=false())
    timeout_seconds = Column(Integer, nullable=False, default=600)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())


class SandboxEvent(Base):
    """Workspace / Sandbox / Service / Preview 审计事件（任务书 §5.8 / §12.3）。"""

    __tablename__ = "sandbox_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False, index=True)
    tenant_id = Column(String(64), nullable=True)
    user_id = Column(Integer, nullable=True, index=True)
    project_id = Column(Integer, nullable=True, index=True)
    workspace_id = Column(Integer, nullable=True, index=True)
    sandbox_id = Column(Integer, nullable=True, index=True)
    generation = Column(Integer, nullable=True)
    run_id = Column(String(36), nullable=True, index=True)
    job_id = Column(String(32), nullable=True, index=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_sandbox_events_ws_id", "workspace_id", "id"),
    )
