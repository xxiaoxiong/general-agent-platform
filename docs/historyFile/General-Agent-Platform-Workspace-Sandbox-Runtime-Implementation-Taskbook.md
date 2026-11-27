# General Agent Platform --- Workspace Sandbox Runtime 架构设计与实施任务书

> **交付对象：** Codex / Trae\
> **仓库：** `xxiaoxiong/general-agent-platform`\
> **代码基线：** 2026-11-26 检查快照
> `f11a5f1`。执行前必须重新拉取最新分支并审查实际代码。\
> **已确定决策：** Workspace 以 Project 为单位；第一阶段采用 Docker。\
> **实施策略：**
> 单节点先跑通，多用户隔离和资源配额从第一阶段实现；保留未来多 Runner /
> 多节点扩展能力。

------------------------------------------------------------------------

## 0. 总体任务目标

将 General Agent Platform 升级为具备**项目级持久化开发工作空间**的 Agent
平台：

1.  每个 Project 对应一个长期 Workspace。
2.  Agent 能在隔离 Docker Sandbox
    中执行命令、读写项目文件、安装依赖、运行测试。
3.  Agent 能启动、查看、停止、重启长期运行的前后端 Service。
4.  服务启动后，平台生成受授权的 Preview URL，并在对话工作区的 Preview
    区域通过 iframe 展示真实页面。
5.  Agent Run 结束、用户刷新页面或 Backend
    重启，不应导致项目文件丢失；Sandbox 恢复后能按策略恢复 Service。
6.  多用户并发使用时，项目文件、容器、服务、预览入口和资源配额严格隔离。
7.  Sandbox Runner
    故障或重启后，系统能够重新发现容器、对账状态并恢复任务，避免重复创建和资源账目错乱。

**目标不是"Agent 可以执行 docker
run"，而是"每个项目拥有安全、持久、可调度、可恢复的运行环境，Agent
可以操作它，用户可以在内置浏览器中查看它"。**

------------------------------------------------------------------------

## 1. 已确定的架构决策（不得擅自改变）

### 1.1 项目级 Workspace

-   一个 Project 对应一个逻辑 Workspace，第一阶段为 1:1。
-   同一个 Project 下的多个 Session / Agent Run 共享 Workspace。
-   Workspace
    是项目文件和依赖的持久化边界，不等同于一次对话，也不等同于一个容器。
-   如果仓库已有 Project 领域实体，必须复用；如果没有，先检查现有
    Project / Workspace / Conversation 关系，再以最小迁移方式补充。
-   不得将前端 `WorkspaceHome` 页面概念直接等同于后端 Project 数据模型。

### 1.2 Docker 运行时

-   第一阶段使用 Docker Engine。
-   定义 `SandboxProvider` / `ContainerRuntime` 抽象，第一阶段实现
    Docker Provider。
-   后续可扩展 Rootless Docker、gVisor、Kata 或
    Firecracker，但本次不要求实现。
-   Docker Socket 只能由独立 Sandbox Runner 访问；Backend、Agent Run
    Worker、Preview Proxy 和用户 Sandbox 均不得挂载 Docker Socket。

### 1.3 单节点先行，多节点可扩展

-   第一阶段部署一个 Sandbox Runner。
-   Job 协议、Runner 注册、资源预留和路由必须支持未来多个 Runner。
-   第一阶段不全面迁移 Kubernetes。
-   单节点不等于单用户：Project
    ACL、用户隔离、资源配额和并发控制必须第一阶段具备。

### 1.4 保留现有 Agent 核心

-   保留 DeepAgents + LangGraph + Durable Run Worker。
-   不用 OpenHands 替换 Agent Runtime。
-   不把 Docker 生命周期管理塞进 FastAPI Uvicorn Worker。
-   优先复用现有 `SandboxBackendProtocol` 扩展点，经审查后实现 Sandbox
    Adapter。
-   现有 `project_verifier`
    是短时工程验收执行器，继续独立存在，不得改造成长期开发 Sandbox。

------------------------------------------------------------------------

## 2. 开工前的仓库审查

先
`git fetch`，检查当前分支、最新提交、未提交改动和测试状态。必须阅读实际代码，不得只凭本任务书推断目录。

重点检查：

-   `backend/app/services/agent_runtime/worker.py`
-   `backend/app/services/agent_runtime/factory.py`
-   `backend/app/services/agent_runtime/backends.py`
-   `backend/app/services/agent_runtime/tools/policy.py`
-   `backend/app/services/agent_runtime/repositories.py`
-   `backend/app/services/agent_runtime/persistence.py`
-   `backend/app/main.py`
-   当前 Project / Workspace / Session / User / Tenant / RBAC ORM
    模型与迁移
-   `docker/docker-compose.yml`
-   `docker/project-verifier.Dockerfile`
-   `backend/app/services/agent_runtime/project_verifier_worker.py`
-   `frontend/src/views/workspace/WorkspaceHome.vue`
-   `frontend/src/features/preview/WorkspaceFilesPanel.vue`
-   `frontend/src/features/preview/FilePreviewPanel.vue`
-   当前 API client、事件推送、权限校验和主题组件

先输出 `docs/architecture/workspace-sandbox-current-state.md`，说明：

1.  当前 Project、Workspace、Session、Agent 的真实关系；
2.  当前文件后端的作用域、存储路径和读写入口；
3.  Run Worker 的 Lease、Heartbeat、Fencing、Checkpoint 行为；
4.  应用数据库类型、迁移工具和事务能力；
5.  Docker Compose 网络、Volume 和 Backend 部署方式；
6.  前端右侧工作区组件结构；
7.  本任务书与现有代码不一致的地方及兼容方案。

**禁止在未检查现有模型和迁移的情况下重复创建 Project、User、RBAC 或
Session 表。**

------------------------------------------------------------------------

## 3. 核心领域模型与生命周期

必须区分：

  --------------------------------------------------------------------------------------
  对象                    定义                                   生命周期
  ----------------------- -------------------------------------- -----------------------
  Project                 用户创建或拥有的项目                   长期

  Workspace               Project 对应的持久化文件和依赖空间     长期

  Sandbox                 Workspace 当前使用的 Docker 执行环境   可启动、停止、重建

  Sandbox Runner          计算节点上的容器管理进程               节点生命周期

  Agent Run               一次 Agent 任务                        有限，可恢复

  Command Operation       一次短命令或异步命令                   有限

  Service                 Sandbox 内长期运行的进程，如 Vite /    按需持续
                          FastAPI                                

  Preview                 对某个 Service 端口的授权入口          随 Service / 授权变化

  Sandbox Job             创建、启动、停止、恢复等基础设施任务   有限、可重试

  Resource Reservation    Sandbox 在 Runner 上的资源预留         随运行状态变化
  --------------------------------------------------------------------------------------

关系：

``` text
Project 1 ─── 1 Workspace
Workspace 1 ─ N Sandbox Generations
Workspace 1 ─ N Agent Runs
Workspace 1 ─ N Services
Runner 1 ──── N Sandbox Instances
Sandbox Job ── 指向 Workspace / Sandbox / Runner
```

### 3.1 生命周期约束

-   Workspace 文件和项目数据必须独立于容器生命周期。
-   Sandbox 容器可以删除和重建，但 Workspace Volume 不得随容器删除。
-   Sandbox 停止时，容器内进程会停止；不得声称进程内存状态仍然存在。
-   Sandbox 恢复后，按数据库保存的 Service Desired State
    重新启动配置为自动恢复的服务。
-   Agent Run 结束不自动停止 Sandbox 或 Service。
-   删除 Session 不得删除 Project Workspace。
-   Project 删除必须经过授权和明确的数据删除流程。

### 3.2 Workspace 写入并发

第一阶段采用保守策略：

-   同一 Workspace 默认只允许一个会修改文件系统的 Agent Run 持有写租约。
-   同一 Workspace 的其他写任务排队，不能并发覆盖文件。
-   只读操作可按权限并行，但构建、依赖安装等命令应视为潜在写操作。
-   Lease 必须有过期时间、Heartbeat 和 Fencing Token。
-   Agent Run 崩溃后，确认旧写入者失去执行权，才能释放或接管租约。
-   文件编辑器直接写入时也必须遵守写租约，或采用文件版本 / 乐观锁。
-   第一阶段不实现多个 Agent 并行修改同一工作树；后续如需并行，优先评估
    Git Worktree。

------------------------------------------------------------------------

## 4. 目标架构与服务边界

采用 Control Plane / Compute Plane 分离：

``` text
Frontend
  Chat / Project Files / Preview iframe / Services / Logs
                    │
                    ▼
Control Plane（现有 Backend 扩展）
  Auth / Project ACL / Workspace API
  Sandbox Manager / Resource Scheduler / Service Registry
  Preview Authorization / Events / Audit
  Existing Agent Run Worker
    DeepAgents + LangGraph + Checkpoint
    SandboxBackend Adapter
                    │ Internal Runner API
                    ▼
Sandbox Runner（独立进程）
  Docker Provider / Resource Reporter / Reconciler
                    │ Docker Engine API
                    ▼
Compute Node
  Sandbox A: workspace-agent + /workspace Volume + Vite/FastAPI
  Sandbox B: workspace-agent + /workspace Volume
                    ▲
                    │ 受控端口转发
Preview Proxy（独立服务）
  Auth / Route / WebSocket / Rate Limit
```

### 4.1 Control Plane

负责认证、Project ACL、Workspace 元数据、Sandbox Desired
State、资源准入和预留、Job、Service 状态、Preview 授权、事件和审计。

**不直接调用 Docker SDK，不持有 Docker Socket。**

### 4.2 Sandbox Runner

独立部署，负责 Runner 注册、Heartbeat、领取 Job、调用 Docker
Provider、管理 Volume 和容器、上报真实状态、容器标签对账及恢复。

Runner 是高权限基础设施组件，不执行用户代码。用户代码只在 Sandbox
中运行。

### 4.3 Sandbox 内部 workspace-agent

提供命令执行和取消、文件读写、目录列表、PTY（可分阶段实现）、Service
生命周期、日志、进程组管理、端口和健康检查。

只接受可信 Runner / Gateway 的认证请求，不向公网暴露管理端口，不访问
Docker Socket。

### 4.4 Preview Proxy

校验 Preview Grant，将 Preview ID 映射到已登记的 Sandbox + Port，转发
HTTP / WebSocket，支持 Vite HMR，设置安全响应头、连接数和超时限制。

**不接受用户提交任意 upstream URL，避免成为 SSRF 代理。**

------------------------------------------------------------------------

## 5. 数据模型要求

先复用现有 ORM、迁移和命名规范。以下为逻辑字段，不要求照搬命名。

### 5.1 Workspace

``` text
id
project_id                 UNIQUE
tenant_id
owner_user_id
status                     provisioning / ready / degraded / deleting / deleted
volume_id
volume_driver
storage_quota_bytes
resource_profile_id
current_sandbox_id
created_at / updated_at / deleted_at
version
```

-   `project_id` 唯一，第一阶段 Project : Workspace = 1:1。
-   权限默认继承 Project ACL，不重复建设冲突的成员系统。
-   Volume ID 由服务端生成，不允许用户指定任意 Docker Volume。
-   删除采用软删除 + 延迟物理清理。

### 5.2 Sandbox Instance

``` text
id
workspace_id
generation
runner_id
container_id
container_name
image_digest
runtime_provider
desired_state              running / stopped / deleted
observed_state             queued / provisioning / starting / ready /
                           stopping / stopped / failed / recovering
cpu_request_millis / cpu_limit_millis
memory_request_bytes / memory_limit_bytes
disk_quota_bytes
pid_limit
last_heartbeat_at / last_observed_at
failure_code / failure_message
created_at / updated_at
```

-   `(workspace_id, generation)` 唯一。
-   每个 Workspace 同时最多一个活动 Generation。
-   容器必须带平台标签：`platform.workspace_id`、`platform.sandbox_id`、`platform.generation`、`platform.managed=true`。
-   不得只凭容器名称识别归属。
-   Desired State 和 Observed State 必须分开。

### 5.3 Sandbox Runner

``` text
id / node_name / status
runtime_provider / runtime_version
cpu_total / cpu_allocatable / cpu_reserved
memory_total / memory_allocatable / memory_reserved
disk_total / disk_allocatable / disk_reserved
active_sandbox_count
last_heartbeat_at
capabilities_json
version
```

真实使用量和资源预留量分开记录。

### 5.4 Sandbox Job

``` text
id
workspace_id / sandbox_id / target_runner_id
job_type                   provision / start / stop / restart / delete / reconcile
status                     queued / leased / running / succeeded / failed / cancelled
priority
idempotency_key            UNIQUE
attempt / max_attempts
lease_owner / lease_token / lease_expires_at
payload_json               仅服务端白名单字段
result_json
error_code / error_message
created_at / started_at / finished_at
```

-   Job 至少一次投递，执行必须幂等。
-   通过数据库事务或等价原子更新领取 Job。
-   Runner 周期 Heartbeat。
-   Lease 过期后允许重新领取，旧 Fencing Token 必须失效。
-   不允许模型或普通用户传入任意 Docker
    HostConfig、宿主机路径或任意镜像。

### 5.5 Resource Reservation

``` text
id / runner_id / workspace_id / sandbox_id
cpu_reserved / memory_reserved / disk_reserved
status                     reserved / active / releasing / released
reservation_token
created_at / released_at
```

-   调度前原子预留。
-   容器启动成功后转为 active。
-   Sandbox 停止后释放 CPU / RAM 计算资源，但 Workspace Volume
    磁盘配额仍保留。
-   防止重复释放、重复预留和负数资源计数。

### 5.6 Workspace Service

``` text
id / workspace_id / sandbox_id
name / command / working_directory
port / protocol
health_check_type / health_check_path / timeout
desired_state              running / stopped
observed_state             created / starting / healthy / unhealthy /
                           exited / failed / stopping / stopped
auto_restart
auto_start_on_sandbox_start
process_id / exit_code
last_started_at / last_exited_at
log_reference
created_at / updated_at
```

Service Desired State 必须持久化；Sandbox
重建后按自动恢复配置启动。环境变量和日志不得泄露凭据。

### 5.7 Preview Grant

``` text
id / workspace_id / sandbox_id / service_id / port
token_hash
expires_at / revoked_at
created_by_user_id
created_at / last_accessed_at
```

Token 只保存 Hash；授权绑定 Workspace、Sandbox Generation 和
Port；支持过期和撤销；Preview Token 不能用于命令执行或文件管理。

### 5.8 事件与审计

记录 Workspace 创建/删除、Job 领取/失败/重试、Sandbox 生命周期、Runner
心跳丢失、Service 启停/崩溃、Preview 授权、资源不足、OOM 和 ACL 变化。

日志关联
`tenant_id / user_id / project_id / workspace_id / sandbox_id / generation / run_id / job_id`。禁止记录
Token、密码、API Key 和敏感环境变量。

------------------------------------------------------------------------

## 6. 资源模型与 Sandbox 调度（P0）

### 6.1 Resource Profile

不允许 Agent 自由决定 CPU / RAM / Disk。由平台提供
`small / standard / large` 等资源档位，具体数值由管理员配置：

``` text
cpu_request / cpu_limit
memory_request / memory_limit
disk_quota
pid_limit
max_processes / max_services / max_ports
idle_timeout
network_policy
```

普通开发 Sandbox 默认不分配 GPU。GPU Sandbox 后续单独设计。

### 6.2 节点可分配资源

``` text
allocatable =
    host_total
  - operating_system_reserved
  - platform_reserved
  - inference_or_other_service_reserved
  - safety_margin

available =
    allocatable
  - sum(active_and_reserved_sandbox_requests)
```

-   OS、Backend、数据库、模型推理等宿主机保留资源可配置。
-   CPU 按 request 预留，Docker CPU quota 对应 limit。
-   内存设置硬限制，默认不允许无控制超卖。
-   Disk 有独立配额和监控。
-   PID 设置硬上限。
-   资源预留必须有数据库权威记录，不能只存在 Runner 内存中。

### 6.3 原子调度流程

1.  校验 Project ACL 和用户/租户配额。
2.  解析 Resource Profile；禁止模型传 Docker HostConfig。
3.  过滤 Offline / Draining / 能力不匹配 / 资源不足的 Runner。
4.  对候选 Runner 按剩余 CPU、内存、磁盘、镜像缓存和活动 Sandbox
    数评分。
5.  在数据库事务中锁定 Runner 容量记录或使用等价 CAS，原子预留资源。
6.  创建 Sandbox Instance 和 Provision Job。
7.  Runner 领取 Job 并执行 Docker 操作。
8.  成功后更新 Observed State 和 Reservation。
9.  失败时幂等释放预留或进入可重试状态。
10. 定期对账真实容器和数据库记录。

第一阶段即使只有一个 Runner，也必须实现原子预留。

### 6.4 队列和公平性

第一阶段可用现有关系数据库实现 Job Queue，不必新增 Kafka。至少区分：

-   `provision`：创建环境；
-   `lifecycle`：启动、停止、重启；
-   `recovery`：故障恢复；
-   `cleanup`：清理资源。

命令和构建任务由 Sandbox 内部 Operation 管理，不要与容器创建 Job
混在一个无限队列中。

多用户策略：

-   每用户最大活动 Sandbox 数；
-   每租户 CPU / RAM / Disk 配额；
-   每用户并发创建数；
-   交互任务优先于批量构建；
-   等待时间 Aging，避免低优先级请求长期饥饿；
-   资源不足时排队或拒绝，不得无限创建。

### 6.5 磁盘配额

Docker Named Volume 并不保证所有 Storage Driver
都支持硬容量上限。必须检查目标 Linux 文件系统和 Docker Storage Driver：

-   支持 XFS project quota / Btrfs qgroup 等硬配额时，实施真实硬限制。
-   不支持硬配额时，明确标记软限制，持续监控并设置预警/停止策略。
-   不得把目录大小统计宣称为硬配额。
-   部署文档说明支持的文件系统、驱动和限制。
-   磁盘满时不能拖垮 Docker Root、数据库和宿主机其他服务。

------------------------------------------------------------------------

## 7. Docker Provider 与容器安全

### 7.1 Provider 抽象

定义稳定 `SandboxProvider` 接口，至少包含：

``` python
create_sandbox(...)
start_sandbox(...)
stop_sandbox(...)
restart_sandbox(...)
remove_sandbox(...)
inspect_sandbox(...)
list_managed_sandboxes(...)
get_resource_usage(...)
execute_operation(...)
stream_logs(...)
create_workspace_volume(...)
```

实际签名遵循项目异步规范。业务层只依赖接口；Agent Tool 和 API Route
不得直接调用 Docker SDK。

### 7.2 Workspace Volume

-   使用 Docker Named Volume 或经审查的持久化 Volume Provider。
-   Volume 名称由服务端生成，例如 `gap-ws-{opaque_id}`。
-   Sandbox 内固定挂载到 `/workspace`。
-   容器删除时不得自动删除 Workspace Volume。
-   不使用用户可控的宿主机 Bind Mount Path。
-   不挂载 `/`、Docker Socket、数据库目录、模型目录或其他用户目录。
-   文件访问通过 workspace-agent / Runner 受控 API，Backend
    不直接访问宿主机路径。
-   提供备份、恢复和项目删除流程。

### 7.3 Sandbox 安全配置

每个 Sandbox 显式设置：

-   非 root 用户；
-   `privileged=false`；
-   `cap_drop=ALL`；
-   `no-new-privileges`；
-   seccomp 默认策略；
-   AppArmor（宿主机支持时）；
-   CPU quota、Memory limit、PID limit；
-   `read_only` root filesystem；
-   `/tmp` 和缓存目录使用受限 tmpfs / 可写目录；
-   Workspace Volume 仅挂载到 `/workspace`；
-   合理 ulimit；
-   Docker 日志轮转；
-   默认不映射宿主机端口；
-   默认不分配设备和 GPU。

基础镜像必须能在非 root、只读 rootfs
下运行。不得为了让依赖安装成功而启用 privileged 或放开所有 Capability。

### 7.4 网络隔离

-   每个 Sandbox 使用独立或等价隔离的网络边界。
-   Sandbox 不得访问 Docker API、Runner 管理
    API、数据库、Redis、内部模型服务管理端口和其他用户容器。
-   必要公网出站通过受控 Egress 策略；阻断宿主机管理地址、RFC1918
    内网、Link-local、云元数据和平台内部网段，除非管理员显式白名单。
-   防止 DNS Rebinding 和 SSRF 绕过。
-   不将容器端口直接发布到公网。
-   Preview Proxy 只能访问登记的 Sandbox + Port。
-   仅创建 Docker bridge network
    不足以证明隔离完成，必须有实际网络测试。

### 7.5 Runner 安全

-   只有 Runner 可以访问 Docker Socket。
-   Runner 只接受经过认证的内部 Job。
-   Runner 不接受用户任意镜像、挂载路径、Capabilities、Network、Devices
    或 HostConfig。
-   Image、Volume、Network、Mount、SecurityOpt 均由服务端策略生成。
-   Runner 管理 API 只监听管理网络。
-   生产环境优先 mTLS 或等价短期服务凭据。
-   Backend、Preview Proxy、Agent Worker 和 Sandbox 均不挂载 Docker
    Socket。

------------------------------------------------------------------------

## 8. Runner、Job Lease 与故障恢复

### 8.1 Runner 独立部署

Runner 启动后注册节点信息和 Runtime 能力，周期上报
Heartbeat、资源和活动容器；领取分配给自己的 Job；执行期间续租；使用
Fencing Token 确认仍持有执行权；上报结果和真实容器状态。

优雅关闭时停止领取新 Job，不得擅自删除 Workspace Volume。

### 8.2 Job 幂等

创建容器前按平台标签检查对应 Sandbox Generation：

-   已存在且配置一致：接管并更新状态，不重复创建。
-   已存在但配置不一致：进入 Reconciliation / Failed，不能静默覆盖。
-   不存在：创建并写入平台标签。
-   Docker 创建成功但 DB 提交失败：Job 重试时通过标签重新发现容器。
-   删除 Job 重复执行必须安全返回。
-   相同 `idempotency_key` 不得创建多个 Sandbox。

### 8.3 Desired / Observed / Reconciler

数据库保存 Desired State，Runner 采集 Docker Observed State：

-   Desired=running、Observed=missing：检查是否需要重建。
-   Desired=running、Observed=stopped：按策略启动。
-   Desired=stopped、Observed=running：执行停止。
-   DB 有记录、容器不存在：进入恢复或重建。
-   容器存在、DB 无记录：按平台标签隔离并告警，不自动接管未知容器。
-   Runner Offline：停止分配新 Job，已分配任务进入超时/恢复流程。
-   Lease 过期：允许新 Runner 接管，旧 Fencing Token 不得更新状态。

不能只依赖 Docker `restart: unless-stopped`。

### 8.4 长命令与 Agent Run

-   Agent Run 仍由现有 Durable Run Worker 管理。
-   Sandbox Service 不依附 Agent Run 的 Shell 生命周期。
-   短命令同步返回，必须设置超时和最大输出长度。
-   长命令使用 `Command Operation`：返回
    `operation_id`，支持查询、读取输出和取消。
-   取消必须杀死对应进程组，避免留下孤儿进程。
-   Operation 记录 Workspace、Sandbox Generation、Run
    ID、时间、超时和退出码。
-   Sandbox Generation 变化后，旧 Operation 标记 interrupted /
    unknown，不得假设命令自动继续。
-   Agent 重试前必须检查副作用。
-   不允许数小时命令阻塞无 Heartbeat 的 HTTP 请求。

### 8.5 Service 长期运行

-   Service 由 Sandbox 内 Process Manager 管理。
-   `start_service` 返回 Service ID 和初始状态，不等待进程永久运行。
-   健康检查通过后才变为 `healthy`。
-   Service Desired State 保存在 Control Plane。
-   Sandbox 恢复后按配置重新启动服务。
-   服务崩溃按 `auto_restart` 和重试上限处理，避免无限重启。
-   stdout / stderr 轮转并设置容量上限。
-   停止服务时清理整个进程组。
-   Agent Run 结束不能自动停止服务。

### 8.6 空闲回收

-   提供可配置 `idle_timeout`，不能硬编码。
-   活跃 Agent Run、Terminal、最近 Preview 访问和用户显式 Keep Alive
    可刷新活动时间。
-   到期先通知前端，再按策略停止 Sandbox。
-   停止只释放 CPU / RAM，不删除 Workspace Volume。
-   用户重新打开项目时排队启动 Sandbox，并恢复配置为自动启动的 Service。
-   有活动 Agent Run 时不得自动停止 Sandbox。

------------------------------------------------------------------------

## 9. Agent 工具与 Runtime 集成

### 9.1 工具集

至少提供：

  Tool                   作用
  ---------------------- ----------------------------------------------
  `execute_command`      执行短命令，返回 stdout / stderr / exit code
  `get_command_status`   查询异步命令状态
  `get_command_output`   获取命令输出
  `cancel_command`       取消命令并清理进程组
  `start_service`        启动长期服务
  `list_services`        列出项目服务
  `get_service_status`   查询状态和健康检查
  `get_service_logs`     读取服务日志
  `stop_service`         停止服务
  `restart_service`      重启服务
  `get_preview_url`      获取服务预览地址

文件工具继续提供 read / write / list /
search，但必须保证文件工具和命令工具操作同一个
`/workspace`，不能出现两套文件系统。

### 9.2 Tool Context

-   `tenant_id`、`user_id`、`project_id`、`workspace_id`、`sandbox_id`
    从已认证 Run Context 注入。
-   不允许模型通过工具参数指定其他用户 Workspace / Sandbox ID。
-   每次调用重新校验 Project ACL、Workspace Lease 和 Sandbox 状态。
-   不暴露 Docker HostConfig、Volume Path、Runner ID、宿主机地址。
-   Tool Result 返回明确状态、退出码、截断信息和 Operation ID。
-   错误区分权限不足、资源排队、Sandbox
    未启动、命令超时、进程退出和内部故障。

### 9.3 SandboxBackend Adapter

优先复用现有
`SandboxBackendProtocol`。如果协议只覆盖简单执行，扩展为稳定 Adapter：

-   Agent Tool Call 映射到 Sandbox Manager；
-   从 Run Context 获取调用者和 Project；
-   获取当前 Sandbox Generation；
-   Sandbox 未启动时按策略启动或排队；
-   调用 workspace-agent；
-   返回结构化 Tool Result；
-   将命令、服务和日志事件关联到 Agent Run。

DeepAgents 不得直接导入 Docker SDK。

------------------------------------------------------------------------

## 10. Service 启动与 Preview

### 10.1 Service 接口

`start_service` 至少支持：

``` json
{
  "name": "frontend",
  "command": "npm run dev -- --host 0.0.0.0",
  "working_directory": "/workspace",
  "port": 5173,
  "protocol": "http",
  "health_check": {
    "type": "http",
    "path": "/",
    "timeout_seconds": 60
  },
  "auto_restart": true,
  "auto_start_on_sandbox_start": true
}
```

-   命令在 Sandbox 内执行。
-   端口登记到 Service Registry。
-   健康检查通过才标记 Ready。
-   同一 Workspace 内不能重复占用同一端口。
-   Service 名称在 Workspace 内唯一。
-   支持 Frontend 5173 + Backend 8000 等多服务。
-   第一阶段 Preview 支持 HTTP Web App。
-   Agent 可从日志和健康检查判断启动失败原因。

### 10.2 Preview 路由

建议使用独立 Preview 域名：

``` text
https://{opaque-preview-id}.preview.example.com/
```

路由：

``` text
Browser iframe
  → Preview Domain
  → Preview Proxy
  → Validate Preview Grant
  → Runner / Sandbox Gateway
  → Registered Sandbox + Port
```

要求：

-   只代理已登记的 Sandbox + Port，不接受任意 upstream URL。
-   支持 HTTP、WebSocket、Vite HMR。
-   正确处理 Host、X-Forwarded-\*、Upgrade、Connection。
-   Grant 绑定 Project / Workspace / Sandbox Generation / Port /
    Expiry。
-   Token 可撤销、过期。
-   Token 不能访问终端、文件或 Daemon 管理接口。
-   Preview 页面与平台控制台使用不同 Origin / 子域名。
-   不向 Preview Origin 发送平台认证 Cookie。
-   设置 CSP、Referrer-Policy、X-Content-Type-Options 等响应头。
-   日志不得记录原始 Token。
-   管理员可关闭外部分享，仅允许登录用户访问。

本地开发可使用 localhost / 测试域名；生产必须配置独立 Preview 域名和
TLS。

### 10.3 前端 UI

在现有右侧 Workspace 区域增加：

``` text
┌────────────────────────────────────────────┐
│ Files │ Preview │ Services │ Terminal(opt) │
├────────────────────────────────────────────┤
│ Service: frontend  ● Ready   Port: 5173    │
│                                            │
│               Preview iframe               │
│                                            │
│ [Refresh] [Open in new tab] [Copy URL]     │
└────────────────────────────────────────────┘
```

必须支持：

-   Agent 启动服务后收到事件并展示状态。
-   Preview / Files / Services 手动切换。
-   Loading、Ready、Unhealthy、Exited、Expired、Error 状态。
-   多服务 Preview Tab。
-   刷新、新窗口打开、复制 URL。
-   iframe 响应式布局。
-   页面刷新后重新获取 Preview Grant，不要求重启服务。
-   切换 Project 时不串显上一个项目。
-   Preview 失效时刷新授权，不出现无提示白屏。
-   不把 iframe 当普通文件预览组件复用。
-   前端不得自行拼接容器 IP 或端口。

------------------------------------------------------------------------

## 11. 多用户与配额

### 11.1 权限继承

-   Workspace 默认继承 Project 的 owner / member / role ACL。
-   不另建与现有 RBAC 冲突的权限模型。
-   Project 删除、成员移除或角色变更时，同步更新 Workspace 权限。
-   共享权限至少区分查看、使用/执行、管理。
-   Preview 权限与命令执行权限分离。

### 11.2 强制隔离

User A 不得读取 User B 文件、执行其命令、查看 Service 日志、访问
Preview、查询 Sandbox 状态或通过路径穿越访问 Volume。

Sandbox 不得通过网络访问其他 Sandbox、Backend 管理
API、数据库、Redis、Runner 管理端口或宿主机管理地址。

### 11.3 配额

至少支持：

-   Tenant 级 CPU / RAM / Disk 总配额；
-   User 级最大活动 Sandbox 数；
-   Project 级最大 Service 数和 Preview Port 数；
-   Sandbox 级 CPU / RAM / PID / Disk 限制；
-   命令最大运行时长和输出量；
-   Service 日志最大保留量；
-   Preview 并发连接和速率限制。

普通用户和模型不能提高配额。

------------------------------------------------------------------------

## 12. API 与内部协议

遵循现有 API 命名、版本和错误响应规范。以下为建议语义，实际路径可调整。

### 12.1 用户侧 API

``` text
GET    /api/projects/{project_id}/workspace
POST   /api/projects/{project_id}/workspace/provision

GET    /api/workspaces/{workspace_id}/sandbox
POST   /api/workspaces/{workspace_id}/sandbox/start
POST   /api/workspaces/{workspace_id}/sandbox/stop
POST   /api/workspaces/{workspace_id}/sandbox/restart

POST   /api/workspaces/{workspace_id}/services
GET    /api/workspaces/{workspace_id}/services
GET    /api/workspaces/{workspace_id}/services/{service_id}
POST   /api/workspaces/{workspace_id}/services/{service_id}/stop
POST   /api/workspaces/{workspace_id}/services/{service_id}/restart
GET    /api/workspaces/{workspace_id}/services/{service_id}/logs

POST   /api/workspaces/{workspace_id}/services/{service_id}/preview-grants
DELETE /api/workspaces/{workspace_id}/preview-grants/{grant_id}
```

命令执行 API 可只供 Agent Adapter 使用，不要求直接暴露给浏览器。

### 12.2 Runner 内部 API

``` text
POST /internal/runners/register
POST /internal/runners/{runner_id}/heartbeat
POST /internal/runners/{runner_id}/jobs/claim
POST /internal/jobs/{job_id}/heartbeat
POST /internal/jobs/{job_id}/complete
POST /internal/jobs/{job_id}/fail
POST /internal/runners/{runner_id}/sandboxes/report
```

必须有服务身份认证和 Fencing Token，普通用户 Token 不能调用。

### 12.3 事件

复用现有 SSE / WebSocket 机制：

``` text
workspace.provisioning / workspace.ready
sandbox.queued / sandbox.provisioning / sandbox.starting
sandbox.ready / sandbox.stopping / sandbox.stopped / sandbox.failed
service.starting / service.healthy / service.unhealthy / service.exited
preview.ready / preview.expired
resource.waiting / resource.denied
```

事件必须带 Project / Workspace / Sandbox / Generation
标识；前端只能订阅当前用户有权限的项目事件。

------------------------------------------------------------------------

## 13. 部署、镜像与配置

### 13.1 Compose 服务

按当前 Compose 结构新增：

-   `sandbox-runner`：独立 Runner，唯一允许访问 Docker Socket
    的平台服务；
-   `workspace-sandbox` 镜像：由 Runner 动态创建，不一定是常驻 Compose
    服务；
-   `preview-proxy`：统一预览入口；
-   必要时 `sandbox-gateway`：Preview Proxy 到 Sandbox 的受控转发。

保留 `project_verifier`，不与长期 Sandbox 共用 Volume、队列和生命周期。

### 13.2 Docker Socket

-   只挂载给 Runner。
-   Runner 不向外暴露 Docker API。
-   Backend、Preview Proxy、Sandbox 不挂载 Socket。
-   可使用 Socket Proxy 限制 Docker API 操作面。
-   生产优先独立 Linux Runner 主机；开发环境可用 Docker
    Desktop，但必须验证网络和 Volume 行为。

### 13.3 Sandbox 镜像

提供可版本化的基础镜像：

-   非 root 默认用户；
-   Bash / 基础开发工具；
-   Python / Node 运行环境按模板或镜像标签管理；
-   `workspace-agent`；
-   进程管理和日志工具；
-   CA 证书、时区和基础网络工具；
-   不内置平台生产密钥；
-   固定版本 / Digest，支持升级和回滚。

不要每次创建 Sandbox
都重新安装系统依赖。通过基础镜像和项目依赖缓存降低启动时间。

### 13.4 配置项

至少包含：

``` text
SANDBOX_ENABLED
SANDBOX_PROVIDER=docker
SANDBOX_RUNNER_ID
SANDBOX_RUNNER_HEARTBEAT_INTERVAL
SANDBOX_JOB_LEASE_SECONDS
SANDBOX_RECONCILE_INTERVAL
SANDBOX_DEFAULT_PROFILE
SANDBOX_MAX_ACTIVE_PER_USER
SANDBOX_MAX_ACTIVE_PER_TENANT
SANDBOX_IDLE_TIMEOUT
SANDBOX_IMAGE
SANDBOX_NETWORK_POLICY
SANDBOX_VOLUME_QUOTA_MODE
SANDBOX_CPU_RESERVE
SANDBOX_MEMORY_RESERVE
SANDBOX_DISK_RESERVE
PREVIEW_BASE_DOMAIN
PREVIEW_PUBLIC_BASE_URL
PREVIEW_TOKEN_TTL
PREVIEW_MAX_CONNECTIONS
```

配置必须有默认值、校验和部署文档。敏感配置不得提交仓库。

------------------------------------------------------------------------

## 14. 可观测性

新增 Prometheus 指标，遵循现有命名规范：

-   `sandbox_runner_online`
-   `sandbox_runner_heartbeat_age_seconds`
-   `sandbox_active_count`
-   `sandbox_queue_depth`
-   `sandbox_queue_wait_seconds`
-   `sandbox_provision_duration_seconds`
-   `sandbox_start_duration_seconds`
-   `sandbox_cpu_reserved` / `sandbox_cpu_usage`
-   `sandbox_memory_reserved_bytes` / `sandbox_memory_usage_bytes`
-   `sandbox_volume_usage_bytes`
-   `sandbox_job_retry_total`
-   `sandbox_reconcile_mismatch_total`
-   `sandbox_oom_total`
-   `workspace_service_restart_total`
-   `workspace_service_health`
-   `preview_request_total`
-   `preview_request_denied_total`

Dashboard 必须能查看 Runner 在线状态、资源总量/预留/实际使用、Sandbox
数、队列、Job 失败、服务状态和资源告警。日志可按 Project / Workspace /
Sandbox / Run / Job 关联。禁止把 Token 或密钥放进日志和指标标签。

------------------------------------------------------------------------

## 15. 分阶段实施顺序

### Phase 0：仓库审查与架构基线

交付：

-   `docs/architecture/workspace-sandbox-current-state.md`
-   `docs/architecture/workspace-sandbox-runtime.md`
-   ER 图、状态机、Runner / Job / Lease 时序图；
-   安全边界和网络拓扑；
-   旧文件工作区迁移兼容方案。

验收：不重复建设 Project / ACL / Session；明确现有 Agent Runtime
如何接入。

### Phase 1：持久化 Workspace + 单节点 Sandbox

实现：

-   Workspace 与 Project 1:1；
-   Workspace 元数据和 Volume；
-   Docker Provider；
-   独立 Sandbox Runner；
-   Sandbox 创建、启动、停止、重启、删除；
-   Resource Profile；
-   CPU / Memory / PID 限制；
-   Disk Quota 能力检测；
-   Runner Heartbeat；
-   Job Lease、Fencing、幂等创建；
-   Desired / Observed State；
-   基础 Reconciler；
-   Project ACL；
-   Compose 和部署文档。

验收：

-   同一 Project 多次创建不会产生多个 Workspace。
-   Sandbox 重建后项目文件仍存在。
-   Runner 重启后能重新发现已有容器。
-   Backend 无 Docker Socket。
-   资源不足时排队或拒绝。
-   用户 A 无法访问用户 B 的 Sandbox。
-   Project Verifier 不受影响。

### Phase 2：workspace-agent + Agent 工具集成

实现：

-   Sandbox 内 `workspace-agent`；
-   `execute_command`；
-   文件读写和目录操作接入同一个 `/workspace`；
-   命令超时、取消、输出截断；
-   Command Operation 查询；
-   `SandboxBackendProtocol` Adapter；
-   Run Context 权限注入；
-   Workspace 写租约；
-   Agent Run 结束不自动销毁 Sandbox。

验收：Agent
能在容器内创建文件、安装依赖、运行测试；文件工具和命令工具看到同一份文件；取消命令无孤儿进程；不能访问其他
Workspace；Backend 不直接调用 Docker SDK。

### Phase 3：Service Manager + Preview

实现：

-   Service 启停、状态、日志和重启；
-   Desired / Observed State；
-   HTTP Health Check；
-   多端口服务；
-   Preview Grant；
-   Preview Proxy；
-   WebSocket / Vite HMR；
-   右侧 Preview iframe；
-   状态事件推送；
-   Token 过期和撤销。

验收：Vue/Vite、FastAPI、前后端双服务能运行；Agent Run 完成后 Preview
仍可访问；刷新页面能恢复；Token 不能访问终端、Daemon 或其他项目。

### Phase 4：多用户资源调度与可靠性

实现：

-   Tenant / User 配额；
-   Runner 资源上报；
-   原子资源预留；
-   Node Filtering / Scoring；
-   Job 公平性和 Aging；
-   多 Runner 注册和任务路由；
-   Runner Offline / Recovery；
-   Reconciler 增强；
-   OOM、磁盘不足处理；
-   Sandbox 空闲回收；
-   Service 自动恢复；
-   审计和资源 Dashboard。

验收：并发创建时预留正确；单用户不能耗尽全局资源；Runner
故障可恢复；不重复创建；容器重建后文件不丢；Reservation 账目一致。

### Phase 5：强隔离与多节点扩展（本次不要求完整实现）

预留 Rootless Docker、gVisor、Kata / Firecracker、多节点 Volume /
Snapshot、Warm Pool、Hibernate / Resume、GPU Sandbox 和 Kubernetes
Provider。第一阶段只要求接口可扩展。

------------------------------------------------------------------------

## 16. 测试与验收矩阵

必须有真实 Docker 集成测试和 Playwright 浏览器测试，不能只用 Mock。

### 16.1 基础功能

1.  新建 Project 后创建 Workspace。
2.  同一 Project 多次请求返回同一 Workspace。
3.  Sandbox 创建、启动、停止、重启、删除。
4.  Python 脚本执行。
5.  Node 项目依赖安装。
6.  文件读写和目录操作。
7.  Sandbox 删除后 Workspace 文件仍存在。
8.  新 Generation 挂载原 Workspace Volume。
9.  Agent Run 完成后 Sandbox 保持运行。
10. Project 删除后按策略清理 Volume。

### 16.2 Service / Preview

1.  Vue / Vite 启动并预览。
2.  FastAPI 启动并访问。
3.  前后端双服务同时运行。
4.  健康检查通过后才显示 Preview Ready。
5.  Vite HMR WebSocket 正常。
6.  服务崩溃后可查看日志。
7.  Stop Service 后 Preview 失效。
8.  Token 过期、撤销后拒绝访问。
9.  多端口 Preview 切换正确。
10. 页面刷新后重新获取 Grant。
11. 切换 Project 时不串显上一个项目。

### 16.3 多用户

1.  User A 不能读取 User B 文件。
2.  User A 不能执行 User B 命令。
3.  User A 不能读取 User B Service 日志。
4.  User A 不能访问 User B Preview。
5.  ACL 撤销后已签发 Grant 失效。
6.  不同用户可使用相同容器内部端口。
7.  同一用户多个 Project 互相隔离。
8.  路径穿越、IDOR、参数篡改被拒绝。
9.  Workspace 写租约阻止两个 Agent Run 同时写入。
10. 成员移除后授权及时失效。

### 16.4 资源压力

1.  多用户同时创建 Sandbox。
2.  CPU request 超额时排队。
3.  内存 request 超额时排队或拒绝。
4.  容器达到内存 limit 时正确记录。
5.  PID fork bomb 被限制。
6.  Workspace 达到磁盘 quota 时不拖垮宿主机。
7.  大型 `npm install` 不阻塞 Sandbox 生命周期操作。
8.  用户达到配额后不能继续创建。
9.  Runner 恢复资源后队列继续处理。
10. Reservation 不出现负数、重复释放或超额分配。

### 16.5 长程与故障注入

1.  Agent Run 长任务期间 Heartbeat 正常。
2.  Backend 重启，Sandbox 继续运行或被正确恢复。
3.  Runner 在领取 Job 后被杀死。
4.  Runner 在 Docker create 成功、数据库更新前被杀死。
5.  Lease 过期后旧 Runner 再次上报。
6.  Docker Daemon 重启。
7.  Sandbox 进程崩溃。
8.  容器重建后 Volume 数据完整。
9.  Service 按 Desired State 恢复。
10. 重复提交 Job 不产生重复容器。
11. Preview Proxy 重启后恢复路由。
12. 删除项目不误删其他项目 Volume。

### 16.6 网络安全

1.  Sandbox 无法访问 Docker Socket。
2.  Sandbox 无法访问 Runner 管理 API。
3.  Sandbox 无法访问其他 Sandbox。
4.  Sandbox 无法访问数据库 / Redis 管理端口。
5.  Sandbox 无法访问宿主机管理地址和云元数据。
6.  Egress 允许必要公网依赖下载。
7.  Preview Proxy 不能代理任意 URL。
8.  Host Header / 路径 / 端口参数不能访问其他项目。
9.  Preview Origin 不携带平台认证 Cookie。
10. WebSocket 和 iframe 不绕过 Preview Grant。

### 16.7 自动化要求

-   Backend 单元测试：模型、ACL、状态机、调度、Lease。
-   DB 集成测试：并发领取、Reservation、幂等键、Fencing。
-   Docker 集成测试：Volume、容器生命周期、资源限制、网络。
-   workspace-agent 测试：命令、进程组、Service、日志。
-   Playwright E2E：Project 创建、Agent
    启动服务、Preview、刷新、切换项目。
-   安全回归：IDOR、SSRF、Token 撤销、网络隔离。

不能因为本机没有 Docker 就永久跳过 Docker 集成测试；应提供可启动的测试
Compose Profile。

------------------------------------------------------------------------

## 17. 非功能指标

以下为首轮工程目标，需在目标硬件上实测，不得作为未经测试的性能承诺：

-   Warm Sandbox 创建 / Ready：记录 P50、P95，先控制在可接受的几十秒内。
-   简单命令执行：记录 P50、P95。
-   Vite 启动至 Preview Ready：记录完整耗时。
-   Runner 心跳丢失检测：阈值可配置。
-   Job 重试：不得重复创建容器。
-   多用户资源调度：不得超额预留。
-   Preview 代理：支持持续 WebSocket 和 HMR。
-   服务日志：容量上限、轮转和权限控制。
-   资源指标：CPU / RAM / Disk / PID 的预留值与实际值可区分。

必须提交压测环境、数据和结果，不能只写"性能良好"。

------------------------------------------------------------------------

## 18. 兼容性、迁移与回滚

当前文件后端可能按 `user / agent / session` 组织文件。迁移到项目级
Workspace 时：

-   先确认现有文件归属和复用关系。
-   不得把多个 Session 目录无条件合并，避免同名文件覆盖。
-   没有 Project 归属的旧会话继续使用旧后端，或提供显式迁移流程。
-   新 Project 创建后才启用 Project Workspace。
-   提供迁移预览、冲突报告和失败回滚。
-   保留现有 Agent Run Checkpoint 和 Session 数据。
-   `project_verifier` 生命周期不变。
-   通过 Feature Flag 按用户或 Project 灰度开启。
-   回滚 Backend 代码时不能自动删除 Workspace Volume。
-   数据库迁移必须可审查，并提供回滚或向前修复脚本。

------------------------------------------------------------------------

## 19. 本次明确不做

-   全面重写 DeepAgents / LangGraph。
-   替换现有 Durable Run Worker。
-   直接引入 OpenHands / Daytona 全套系统。
-   第一阶段部署 Kubernetes。
-   第一阶段实现 Firecracker / Kata。
-   GPU Sandbox。
-   多 Agent 并行修改同一工作树。
-   完整在线 IDE 编辑器。
-   完整生产部署平台 / 公网发布系统。
-   任意容器镜像由普通用户上传执行。
-   允许 Sandbox 访问宿主机 Docker API。
-   将 Project Verifier 改成长期 Sandbox。

------------------------------------------------------------------------

## 20. Codex / Trae 执行要求与交付物

### 执行要求

1.  审查最新代码、分支和测试状态。
2.  先完成 Phase 0，再开始实现。
3.  按 Phase 1 → 2 → 3 → 4 顺序实施，每阶段独立提交。
4.  不得为了快速跑通跳过 ACL、资源限制、Lease、Fencing、幂等和网络安全。
5.  发现任务书与代码冲突时，先说明冲突和兼容方案，不得静默改变架构决策。
6.  不得用 Mock 代替 Docker / Browser 集成测试。
7.  不得只完成后端 API 而不完成前端真实预览闭环。
8.  不得把"容器创建成功"作为全部验收标准。
9.  所有新增配置、迁移、镜像和部署方式必须有文档。
10. 每阶段结束输出变更清单、测试结果、未完成项和已知风险。

### 最终交付物

-   当前架构审查报告；
-   Workspace Sandbox Runtime 架构文档；
-   ER 图、状态机和关键时序图；
-   数据库迁移；
-   Sandbox Manager；
-   Sandbox Runner；
-   Docker Provider；
-   workspace-agent 镜像和协议；
-   SandboxBackend Adapter；
-   Agent 工具集；
-   Service Manager；
-   Preview Proxy；
-   前端 Preview / Services UI；
-   Compose 部署配置；
-   安全配置和网络策略；
-   Prometheus 指标与 Dashboard；
-   单元、集成、Docker、Playwright 和安全测试；
-   部署、运维、备份、恢复和故障排查文档。

### Definition of Done

只有全部满足才算完成：

1.  每个 Project 有唯一持久化 Workspace。
2.  Agent 能在该 Project 的 Docker Sandbox 中执行命令。
3.  文件工具和命令工具操作同一份项目文件。
4.  Agent 能启动、查看、停止和重启 Service。
5.  用户能在对话工作区 Preview 区域看到真实运行页面。
6.  Vite HMR / WebSocket 正常。
7.  Agent Run 完成不影响仍需运行的 Service。
8.  Backend / Runner 重启后项目文件不丢失。
9.  Runner 能按标签和 Generation 对账容器。
10. 多用户项目、文件、命令、Service 和 Preview 严格隔离。
11. CPU / RAM / PID / Disk 有限制和资源预留。
12. 超额请求排队或拒绝，不无限创建容器。
13. Job 有 Lease、Heartbeat、Fencing、幂等和恢复。
14. Preview Token 可过期、撤销，且无 Sandbox 管理权限。
15. 现有 Agent Runtime、Session、Checkpoint 和 Project Verifier
    回归测试通过。
16. Docker 集成、Playwright E2E 和关键安全测试通过。
17. 有可重复的部署、备份、恢复和故障排查文档。

------------------------------------------------------------------------

## 21. 官方参考资料

### Agent Workspace / Sandbox

-   OpenHands Architecture:
    https://github.com/OpenHands/OpenHands/blob/main/docs/architecture.md
-   OpenHands SDK Architecture:
    https://github.com/OpenHands/docs/blob/main/sdk/arch/overview.mdx
-   OpenHands SDK: https://github.com/OpenHands/software-agent-sdk
-   Daytona Architecture: https://www.daytona.io/docs/en/architecture/
-   Daytona Persistence: https://www.daytona.io/docs/en/persistence/
-   Daytona Preview: https://www.daytona.io/docs/en/preview/
-   E2B Runtime: https://github.com/e2b-dev/runtime
-   E2B Runtime Architecture:
    https://github.com/e2b-dev/runtime/blob/main/docs/ARCHITECTURE.md
-   CodeSandbox SDK: https://codesandbox.stream/docs/sdk
-   CodeSandbox Tasks:
    https://codesandbox.stream/docs/learn/vm-sandboxes/task
-   Coder Architecture:
    https://coder.com/docs/install/plan/architecture/

### Docker / 安全 / 调度

-   Docker Resource Constraints:
    https://docs.docker.com/engine/containers/resource_constraints/
-   Docker Security: https://docs.docker.com/engine/security/
-   Docker Rootless: https://docs.docker.com/engine/security/rootless/
-   Docker Networking: https://docs.docker.com/engine/network/
-   gVisor: https://gvisor.dev/docs/
-   Kata Containers: https://github.com/kata-containers/kata-containers
-   Kubernetes Resource Management:
    https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
-   Kubernetes Scheduler:
    https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/

------------------------------------------------------------------------

**最终架构基线：项目级 Workspace + Docker Sandbox + 独立 Sandbox
Runner + 持久化 Volume + Service Manager + Preview Proxy +
多用户资源配额与故障恢复。**
