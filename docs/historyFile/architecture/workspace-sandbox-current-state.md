# Workspace Sandbox Runtime —— 现状审查报告（Phase 0）

> 审查基线：`main` @ `f115bba`（任务书基线 `f11a5f1` 之后的测评能力强化提交）。
> 审查方式：仓库实际代码 + 迁移链 + Compose + 前端组件逐文件核对，未凭任务书推断。
> 结论先行：**当前不存在 Project 实体**；文件工作区按 `user/agent/session` 组织在 Backend
> 宿主机目录；Backend 不持有 Docker Socket；`project_verifier` 是短时离线验收执行器。

## 1. Project / Workspace / Session / Agent 的真实关系

| 实体 | 位置 | 关系与说明 |
| --- | --- | --- |
| `Agent` | `backend/app/models/agent.py:10` | 智能体定义，`owner_id -> users.id`；无 Project 归属字段 |
| `AgentVersion` | `backend/app/models/agent.py:49` | 不可变发布快照，Run 绑定版本 |
| `AgentSession` | `backend/app/models/agent.py:71` | 对话会话，`agent_id`、`user_id`；`active_run_id` + `active_run_lease_expires_at` 为会话级执行权 |
| `AgentRun` | `backend/app/models/agent.py:141` | 一次执行，`agent_id / agent_version_id / session_id / user_id` |
| `User / Role / Permission / UserRole / RolePermission` | `backend/app/models/user.py` | 平台级 RBAC；**没有** Project 级成员表 |

关键结论：

- 仓库**没有** `Project`、`Workspace`、`Tenant` 实体；`RunScope.tenant_id` 恒为 `None`
  （`backend/app/services/agent_runtime/context.py:19`），租户等价于用户作用域。
- 前端 `WorkspaceHome` 只是页面概念，不能等同于后端 Project
  （任务书 §1.1 明确禁止）。
- 因此本任务需要**最小新增** `Project`（含项目成员）与 `Workspace`，并通过
  `agent_sessions.project_id`（可空）把新会话接入项目级 Workspace；
  未归属项目的存量会话继续走旧文件后端（任务书 §18 允许）。

## 2. 当前文件后端的作用域、存储路径与读写入口

- 构造入口：`build_backend(user_id, agent_id, session_id)`（`backend/app/services/agent_runtime/backends.py:39`）。
- 组合方式：`CompositeBackend`
  - 默认 `StateBackend`：会话内虚拟文件，随 checkpoint 保存；
  - `/workspace/` → `WorkspaceFiles`（`FilesystemBackend`），根目录
    `AGENT_FILES_ROOT/{user_id}/{agent_id}/{session_id}`（`:58-61`）；
  - `/memory/` → `StoreBackend`，namespace `("user_memory", user_id)`。
- 路径函数：`workspace_root(user_id, agent_id, session_id)`（`:166`）。
- 读写入口：DeepAgents 文件工具（`read_file / write_file / edit_file / ls / glob / grep / delete`）
  经 `ToolAdapterFactory` 执行；工具账本、事件与路径守卫见
  `backend/app/services/agent_runtime/tools/adapter.py`、`tools/policy.py`。
- 保护规则：`uploads/`、`team-projects/`、`.run-artifacts/` 与 `.docx` 由平台管理，
  Agent 不可直接改写（`:65-140`）。
- 结论：当前文件系统是 **Backend 宿主机目录**，Sandbox 容器无法共享；
  Phase 2 必须把项目会话的 `/workspace/` 路由改为 Sandbox Volume 的受控 API，
  且文件工具与命令工具必须看到同一份文件（任务书 §9.3）。

## 3. Run Worker 的 Lease / Heartbeat / Fencing / Checkpoint

- Worker：`backend/app/services/agent_runtime/worker.py`；领取与状态迁移在
  `backend/app/services/agent_runtime/repositories/`。
- `AgentRun` 上的协调字段（`models/agent.py:176-202`）：
  `worker_id`、`lease_expires_at`、`heartbeat_at`、`attempt`、`lease_version`。
- 机制：
  - 领取：条件 UPDATE（`status=pending` 或租约过期）并 `attempt += 1`、`lease_version += 1`；
  - 心跳：周期性续租 `lease_expires_at`；
  - Fencing：所有状态/账本写入带 `worker_id + lease_version` 条件，失去执行权抛
    `RunLeaseLost`，禁止继续产生副作用；
  - Checkpoint：`CheckpointerFactory`（`agent_runtime/persistence.py`）支持
    `memory / sqlite / postgres`，生产强制 PostgreSQL（`config.validate_startup`）。
- 结论：Sandbox Job 的 Lease / Fencing 可以直接复用同一套模式，但必须独立于 Run Worker
  （Runner 进程与 Uvicorn 进程分离）。

## 4. 数据库类型、迁移工具与事务能力

- 主库：`DATABASE_URL`（默认 `mysql+aiomysql`），同步迁移 URL `DATABASE_SYNC_URL`（`pymysql`）；
  测试使用 SQLite（`aiosqlite`）；PostgreSQL 仅用于 Checkpointer / Store。
- 迁移：Alembic，`backend/alembic/versions/`，当前 head = `e9f0a1b2c3d4`
  （`add_evaluation_campaign`），base = `9ce3d801b702`。
- `init_db()` 用 `Base.metadata.create_all` 只自动补新表；改既有表必须写 Alembic 迁移。
- 事务：API/Service 使用 `AsyncSession`；Run 领取/账本使用条件 UPDATE + 事务提交。
- 结论：Sandbox Job 队列、资源预留、幂等键均可用关系库实现，无需新增消息中间件
  （任务书 §6.4）。

## 5. Docker Compose 网络、Volume 与 Backend 部署方式

`docker/docker-compose.yml`：

- 服务：`mysql`、`redis`、`checkpoint_pg`、`searxng`、`backend`、`project_verifier`、`asr`、`frontend`。
- `backend`（`:88-176`）：`uvicorn --workers 2`，**未挂载 docker.sock**；挂载
  `app_data`、`../log`、`project_verifier_queue`；网络 `default + langfuse(external)`。
- `project_verifier`（`:178-204`）：`network_mode: none`、只读 rootfs、cap_drop ALL、
  共享队列卷；只做离线工程验收。
- Volume 名固定为 `ai-generalplat_*`（项目改名遗留，禁止清理）。
- 结论：新增 `sandbox-runner`（唯一持 Socket）与 `preview-proxy` 两个 Compose 服务；
  `workspace-sandbox` 镜像由 Runner 动态创建容器，不作常驻服务；`project_verifier` 保持不变。

## 6. 前端右侧工作区组件结构

- `WorkspaceHome.vue`：对话主区；右上角 `FolderOpen` 按钮切换 `filesPanelOpen`；
  右侧渲染 `WorkspaceFilesPanel`（文件树 + 内联预览）或 `FilePreviewPanel`（单文件预览）。
- `WorkspaceFilesPanel.vue`：props `files / loading / error / truncated / downloading / preview`，
  数据来自 `GET /api/v2/conversations/{id}/file-list`（`chatState.workspaceVersion` 防抖刷新）。
- 结论：新增 Files / Preview / Services 三 Tab 侧栏，Preview 用真实 `iframe`，
  不复用文件预览组件（任务书 §10.3 明确要求）。

## 7. 任务书与现有代码的差异及兼容方案

| 任务书要求 | 现状 | 兼容方案 |
| --- | --- | --- |
| Project 1:1 Workspace | 无 Project | 最小新增 `projects / project_members / workspaces`；`agent_sessions.project_id` 可空接入 |
| Volume 持久化 `/workspace` | 宿主机目录 `data/agent_files/...` | 新增 Docker Named Volume；项目会话的文件工具改走 Sandbox 文件 API；旧会话保持旧后端 |
| Backend 不持 Docker Socket | 已满足 | 新增独立 `sandbox-runner` 持 Socket；Backend 只写 Job/预留 |
| Runner 独立部署 | 无 | `python -m app.sandbox_runner`（复用 backend 镜像，独立 Compose 服务） |
| SandboxBackendProtocol 复用 | 有 `build_backend` 扩展点 | 新增 `SandboxFilesBackend` 实现 `BackendProtocol` 异步接口，按项目会话路由 |
| `AGENT_SANDBOX_MODE=external` | 已有开关（`tools/policy.py`） | 新工具风险登记为 EXECUTE，仅 external 模式放行 |
| project_verifier 不变 | 已有 | 不共享 Volume / 队列 / 生命周期 |
| 前端 Preview iframe | 只有文件预览 | 新增 `Preview` / `Services` 面板与新 `src/api/sandbox.ts` |
| 多用户隔离 | 平台 RBAC + 资源所属人校验 | 新增 Project 成员角色（viewer/operator/admin）+ 全部 API 归属校验 |
| Preview 子域名 + Token | 无 | `preview-proxy` 独立服务；本地 `{token}.localhost:8081`，生产配置独立域名 + TLS |

## 8. 本次不改动的边界

- `DeepAgents / LangGraph` 内核、Durable Run Worker、Checkpoint、`project_verifier`
  的生命周期与队列。
- 既有 `/api/v1`、`/api/v2` 契约与历史迁移（仅新增迁移，不修改历史 revision）。
- `docker-compose.yml` 中固定 Volume 名称（`ai-generalplat_*`）。
