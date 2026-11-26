# Workspace Sandbox Runtime 交付报告

> 任务书：`docs/General-Agent-Platform-Workspace-Sandbox-Runtime-Implementation-Taskbook.md`
> 实施日期：2026-11-26 / 2026-11-27
> 结论：Phase 0 – Phase 4 已完成实现、真实 Docker 集成测试与浏览器 E2E；
> 未完成项与已知限制在 §5 如实列出。

## 1. 交付物清单

| 类别 | 路径 |
| --- | --- |
| 现状审查（Phase 0） | `docs/architecture/workspace-sandbox-current-state.md` |
| 架构文档（ER / 状态机 / 时序 / 安全边界） | `docs/architecture/workspace-sandbox-runtime.md` |
| 部署运维手册（备份 / 恢复 / 故障排查 / 安全清单） | `docs/workspace-sandbox-deployment.md` |
| 性能基准报告（环境 / 方法 / 数据） | `docs/workspace-sandbox-performance.md`、`backend/scripts/sandbox_benchmark.py` |
| 数据模型 | `backend/app/models/sandbox.py`（Project / ProjectMember / Workspace / SandboxInstance / SandboxJob / Runner / Reservation / Service / PreviewGrant / CommandOperation / Event / ResourceProfile） |
| 数据库迁移 | `backend/alembic/versions/f0a1b2c3d4e5_add_workspace_sandbox_runtime.py` |
| 控制面服务 | `backend/app/services/sandbox/`（runtime / scheduler / jobs / projects / acl / preview / service_manager / agent_client / write_lease / reconciler / session_files / profiles / bootstrap） |
| Sandbox Runner | `backend/app/sandbox_runner/`（engine = Docker Provider、executor = Job 执行、gateway = 受控转发、api = 注册/心跳/领取） |
| Preview Proxy | `backend/app/preview_proxy/`（Grant 校验、子域/路径 Token、HTTP/WS） |
| workspace-agent 镜像 | `sandbox/Dockerfile` + `sandbox/workspace_agent.py` |
| Agent 工具 | `backend/app/services/tools/sandbox_tools.py`（11 个工具 + 风险登记 + HITL） |
| Sandbox 文件后端 | `backend/app/services/agent_runtime/sandbox_backend.py` |
| 用户 API | `backend/app/api/sandbox/`（projects / workspaces / services / files / internal） |
| 前端 | `frontend/src/api/sandbox.ts`、`features/preview/SandboxPreviewPane.vue`、`SandboxServicesPane.vue`、`WorkspaceFilesPanel.vue`（三 Tab）、`WorkspaceHome.vue`（项目选择） |
| Compose | `docker/docker-compose.yml`（sandbox-runner / preview-proxy / workspace-sandbox-image profile） |
| 指标 / Dashboard | `backend/app/observability/metrics.py`、`docker/grafana/workspace-sandbox-runtime.json` |
| 测试 | `backend/tests/test_sandbox_runtime.py`、`test_sandbox_security.py`、`test_sandbox_api.py`、`test_sandbox_docker_integration.py`、`frontend/e2e/sandbox-preview.spec.ts` |
| 截图证据 | `docs/assets/workspace-sandbox-preview.png`、`workspace-sandbox-services.png` |

## 2. 分阶段变更清单

### Phase 0：仓库审查与架构基线
- 逐文件核对现有 Project / Workspace / Session / RBAC、文件后端、Run Worker Lease/Fencing、数据库与迁移、Compose、前端右侧面板。
- 输出现状审查与目标架构文档；明确「仓库无 Project 实体」→ 最小新增 Project + Workspace + `agent_sessions.project_id` 可空列，存量会话继续旧文件后端。
- 明确 `project_verifier` 与长期 Sandbox 完全隔离，不共享 Volume / 队列 / 生命周期。

### Phase 1：持久化 Workspace + 单节点 Sandbox
- Project 1:1 Workspace；Volume 名 `gap-ws-{sha256(project_id)[:24]}` 服务端生成。
- Sandbox Generation 单调递增；`(workspace_id, generation)` 唯一；Desired / Observed State 分离。
- 资源档位 small / standard / large；Runner 注册 / 心跳 / 容量；调度过滤 + 行锁原子预留；停止释放 CPU/内存、保留磁盘；删除幂等释放。
- Job 队列：priority + Aging、条件 UPDATE 领取、Lease / Fencing Token、失败重试、租约过期回收、幂等键。
- Reconciler：Runner 离线、租约回收、空闲回收、Desired/Observed 不一致告警、指标刷新。
- Project ACL（owner 隐式 admin + viewer/operator/admin 成员）、用户/租户配额。
- 独立 Runner（唯一 Docker Socket）；Docker Provider 通过 Engine API 生成全部 HostConfig；容器平台标签对账；未知容器只告警不接管。
- Compose：`sandbox-runner`（profile sandbox，socket 挂载）、`preview-proxy`、`workspace-sandbox-image`（profile sandbox-image）。

### Phase 2：workspace-agent + Agent 工具
- 独立基础镜像（Node 20 + Python 3 + 构建工具），非 root（uid 10001），只读 rootfs + tmpfs 可运行。
- workspace-agent（std 库）：命令 Operation（超时、输出截断、进程组取消）、文件读写/list/grep/glob/raw、Service 启停/健康检查/日志轮转/自动重启。
- Agent 工具：`execute_command`、`get_command_status`、`get_command_output`、`cancel_command`、`start_service`、`list_services`、`get_service_status`、`get_service_logs`、`stop_service`、`restart_service`、`get_preview_url`。
- 工具风险显式登记（未登记 fail-closed）；执行类仅 `AGENT_SANDBOX_MODE=external` 放行；命令/服务启动进入 HITL。
- `SandboxFilesBackend`：项目会话的 DeepAgents 文件工具路由到 Sandbox `/workspace`；宿主路径拒绝；命令与文件同一份文件。
- Workspace 写租约（CAS + 过期 + Heartbeat + Fencing Token）：写操作串行；Run 结束释放。
- 附件上传、file-list、file-preview、file-bytes、ZIP 下载全部支持 Sandbox 分支。

### Phase 3：Service Manager + Preview
- Service Registry：Desired/Observed、健康检查、自动恢复、项目内名称/端口唯一、配额。
- Preview Grant：Token 只存 SHA256、绑定 Sandbox Generation + Port、TTL、撤销、成员移除立即失效。
- Preview Proxy：`{token}.localhost` 子域 + `/p/{token}/` 兜底；HTTP 流式 + WebSocket（Vite HMR）；安全响应头；连接数限制；不接受任意 upstream。
- 前端：Files / Preview / Services 三 Tab；真实 iframe；刷新页面重新签发 Grant；服务状态/日志/启停；切换项目不串显；项目选择与新建。

### Phase 4：多用户调度与可靠性
- 用户/租户配额（429）；Runner 排空 / 离线；Job 重试与租约回收；空闲回收；Service 自动恢复。
- Prometheus 指标 18 项；Grafana 看板；审计事件 `sandbox_events`（生命周期 / Job / ACL / Preview / 资源）。
- 多 Runner 注册、容量记账、目标 Runner 路由已具备（未做跨节点 Volume 迁移）。

## 2.1 完成度审计补强（第二轮）

对照任务书逐条审计后补齐的缺口：

- **ACL 全链路**：Agent 工具、`SandboxFilesBackend`、附件上传/删除/list/preview 每次调用都
  重新校验 Project 角色；成员移除后正在运行的会话立即失去读写权限（新增测试）。
- **用户写入租约**：用户上传/删除附件与 Agent 写操作共用 Workspace 写租约，
  并发写入返回 `409 workspace_write_busy`，避免用户编辑与 Agent 互相覆盖。
- **配额并发**：Provision 前锁定用户行，串行化同一用户的并发创建，避免突破活动 Sandbox 上限。
- **Generation 隔离**：新 Generation、stop/restart/delete 时旧 `SandboxCommandOperation`
  标记 `interrupted`，不假设命令自动继续。
- **自动恢复**：Reconciler 对 `container_missing` 自动入队 start（10 分钟冷却）；
  长时间无 Runner 上报的 Ready Sandbox 入队 reconcile 探查真实容器。
- **实际资源用量**：Runner 周期采样 Docker stats 与 `du -sb /workspace`，
  回传 Backend 写入 `sandbox_cpu_usage` / `sandbox_memory_usage_bytes` /
  `sandbox_volume_usage_bytes`（与预留值分离）；OOM 转换只计数一次并记审计事件。
- **Runner 重启恢复**：Runner 启动与每轮对账重新接入所有 `platform.network`，
  修复 Runner 重建后既有 Sandbox 的 Gateway 502（集成测试覆盖断网→重连）。
- **依赖安装修复**：Sandbox 用户目录 tmpfs 显式 `uid/gid=10001` 且 `exec`，
  修复 npm/pip 缓存 EACCES 与 npx 二进制 Permission denied。
- **Vite HMR**：Preview Proxy WebSocket 使用去掉 `/p/{token}` 前缀后的真实路径，
  Runner 去重 Host/Origin/Sec-WebSocket-Protocol 头；Vite 8 `?token=` 由页面客户端自动附加并透传。
- **预览限流与端口收敛**：单个 Preview Grant 增加每分钟请求上限与并发连接上限；
  Backend / Frontend / Preview Proxy 的宿主端口默认只绑定 `127.0.0.1`，避免 Sandbox
  经宿主网关访问平台管理面。
- **安全回归测试**：Docker Socket 未挂载、平台服务名不可解析、取消命令无孤儿进程
  已加入真实 Docker 集成测试。
- **登录态预览**：`PREVIEW_REQUIRE_LOGIN=true` 时，Preview Proxy 校验短期 preview token
  （绑定 Grant + User）并实时复核 Project ACL；首次文档请求下发同源 HttpOnly Cookie，
  子资源与 WebSocket 复用；匿名/他人 Token 403。
- **灰度开关**：`SANDBOX_ROLLOUT_USER_IDS` / `SANDBOX_ROLLOUT_PROJECT_IDS` 按用户/项目灰度。
- **事件驱动 UI**：前端订阅 `GET /api/v2/workspaces/{id}/events` SSE（Bearer 认证、
  `after_id` 断点续传、指数退避重连）；Services / Preview 面板收到
  `service.* / sandbox.* / preview.* / workspace.*` 事件即刷新；SSE 正常时轮询降为
  30s 兜底，SSE 不可用时恢复 4s 轮询。
- **Service 事件与指标**：状态同步发布 `service.healthy` / `service.unhealthy` /
  `service.exited`；`workspace_service_health` gauge 与 `workspace_service_restart_total`
  （手动/自动重启）已实际写入。
- **资源事件**：配额拒绝发布 `resource.denied`、节点容量不足发布 `resource.waiting`；
  失败路径用独立会话提交审计事件，避免主事务回滚吞掉事件。
- **磁盘软配额策略**：Runner 上报单个 Sandbox 的 `/workspace` 实际占用；达到
  `SANDBOX_VOLUME_QUOTA_WARN_PERCENT` 发布 `sandbox.volume_quota_warning`，
  达到配额上限发布 `sandbox.volume_quota_exceeded` 并停止 Sandbox
  （只释放计算资源，Volume 保留；同一 Generation 不重复告警/停止）。

## 3. 验证记录（真实执行）

| 验证 | 命令 / 方式 | 结果 |
| --- | --- | --- |
| 后端全量回归 | `cd backend && python -m pytest -q` | **1071 passed, 13 skipped, 102.48s** |
| Sandbox 专项（模型/调度/Lease/ACL/预览/工具/文件后端/写租约/自动恢复/限流/登录态/事件/软配额/对账/服务配额） | `pytest tests/test_sandbox_runtime.py tests/test_sandbox_security.py tests/test_sandbox_api.py -q` | **34 passed** |
| 真实 Docker 集成 | 在 Runner 容器内 `SANDBOX_DOCKER_TEST=1 pytest tests/test_sandbox_docker_integration.py` | **1 passed 40.03s**（容器安全基线、Runner 断网重连、Docker Socket 未挂载、取消命令无孤儿进程、命令、文件一致性、只读 rootfs、网络隔离含平台服务名、Service 健康 + HTTP、删除容器后新 Generation 文件保留、幂等创建） |
| Vite HMR / WebSocket | 在 Runner 容器内 `SANDBOX_DOCKER_VITE=1 pytest tests/test_sandbox_docker_vite.py -q -s` | **1 passed 10.81s**（Vite 安装、dev server、`vite-hmr` WS、修改源码收到 `full-reload`）；启动→Healthy + 首页 200 = **2.018s** |
| 真实端到端（服务层） | 容器内脚本：项目 → Provision → 命令（uid=10001）→ Service → Preview | `exec succeeded`、`service healthy`、子域预览 200、兜底 200、坏 Token 401 |
| 浏览器 E2E | `npm run test:e2e:sandbox`（真实 Edge + 已部署栈） | **1 passed 14.6s**：项目创建 → Sandbox Ready → Service healthy → iframe 真实页面 → 刷新后预览 → 切换项目不串显 → Services 面板；截图见 `docs/assets/` |
| 登录态 Preview（`PREVIEW_REQUIRE_LOGIN=true`） | 真实栈脚本：匿名 / 他人 Token / 本人 Token / Cookie | 匿名 403、他人 Token 403、本人 Token 200、首次响应下发 Cookie 且后续仅 Cookie 200 |
| 重启恢复（真实栈故障注入） | 项目 → Sandbox Ready → Service healthy → 预览 200；`docker restart backend` / `docker restart sandbox-runner`；Sandbox stop → start；删除项目 | 后端重启后 Sandbox 继续运行、Service healthy、预览 200；Runner 重启后 Gateway 重新接入、预览 200；stop→start 同 Generation 文件保留、Service 按 Desired State 自动恢复 healthy、预览 200；删除项目后容器与 Volume 均清理 |
| 事件流（部署栈） | `GET /api/v2/workspaces/{id}/events`（Bearer） | `workspace.provisioning` / `workspace.created` 帧带 project/workspace 标识；非成员 404；keepalive 正常 |
| 性能基准 | `BENCH_ITERATIONS=3 python scripts/sandbox_benchmark.py`（Backend 容器内） | Provision→Ready P50 13.07s / P95 13.07s；命令 P50 0.124s；Service→Healthy P50 1.68s；Preview 首次访问 P50 0.040s（详见性能报告） |
| 前端类型检查/构建 | `cd frontend && npm run build` | 通过 |
| Compose 校验 | `docker compose --profile sandbox --profile sandbox-image config -q` | 通过 |
| SQLite 迁移 | `alembic upgrade head` + `downgrade base` | 通过 |
| MySQL 迁移 | 容器 entrypoint `alembic upgrade head` 启动成功，Sandbox 全链路写入正常 | 通过（未单独执行 MySQL downgrade） |
| PostgreSQL 迁移 | 临时 PG16 容器：`upgrade head` + `downgrade base` | 通过 |

## 4. 安全验证要点

- 容器安全：`privileged=false`、`CapDrop=ALL`、`no-new-privileges`、`ReadonlyRootfs=true`、CPU/内存/PID 限制、非 root（uid 10001）均由集成测试 inspect 校验。
- 网络隔离：Sandbox 无法解析 `backend / mysql / redis / checkpoint_pg`（真实命令测试）；每个 Sandbox 独立 bridge，仅 Runner 加入。
- 数据面：Backend / Preview Proxy / Sandbox 均未挂载 Docker Socket；Runner 是唯一 Socket 持有者。
- 访问控制：跨用户项目 404；viewer 不能执行；成员移除后其 Preview Grant 与 Agent 工具/文件访问立即失效；坏 Token 401；过期/撤销/服务停止 401/403/410；登录态模式匿名/他人 Token 403。
- 并发：用户上传/删除与 Agent 写入共用写租约；配额创建按用户串行化；Generation 切换中断旧命令。
- 可靠性：Runner 重启重新接入所有 Sandbox 网络；容器缺失自动 reconcile/恢复；实际 CPU/内存/Volume 用量与预留值分开记录。
- WebSocket：Preview Proxy → Runner → Vite 的 `vite-hmr` 握手与 `full-reload` 消息实测通过。
- 工具：未登记风险工具 fail-closed；restricted 模式拒绝执行类工具；命令/服务启动 HITL。
- 路径：文件工具仅允许 `/workspace`，拒绝宿主机风格路径与符号链接逃逸。
- 凭据：workspace-agent Token 由 Runner 派生注入；子进程环境移除该 Token；服务日志与命令输出脱敏。

## 5. 未完成 / 未验证事项（如实说明）

1. **多 Runner 实际调度未做双节点真机验证**：注册、心跳、容量、目标 Runner、排空接口已实现，单节点验收通过。
2. **磁盘硬配额未实测**：默认 `soft` 模式（占用监控 + 85% 预警 + 超限停止）；
   `hard` 依赖 XFS project quota / Btrfs qgroup，本机环境未验证。
3. **MySQL downgrade 未执行**：仅验证升级与服务运行；SQLite / PostgreSQL 均已验证双向迁移。
4. **性能基准样本有限**：已完成 3 轮真实 Docker 基准（环境/方法/数据见 `docs/workspace-sandbox-performance.md`），P95 等同于最大值；生产需在目标硬件扩大采样并覆盖并发创建、大型依赖安装、PID/磁盘压满等场景。
5. **SSE 事件推送已接入，但断线期间仍依赖轮询兜底**：Services / Preview 面板订阅
   Workspace SSE（重连 + `after_id` 续传）；SSE 不可用时回退 4s 轮询。
   多副本 Backend 部署时，事件流受各自进程内连接影响，但事件本身已持久化在
   `sandbox_events`，重连后可按 `after_id` 补齐。
6. **未实现**：GPU Sandbox、Kubernetes / gVisor / Kata / Firecracker、Warm Pool、Hibernate、跨节点 Volume 迁移、完整在线 IDE、Terminal Tab、多 Agent 并行修改同一工作树。
7. **Docker Desktop 环境限制**：宿主机 LAN / 云元数据隔离需宿主防火墙；生产建议 Linux + 独立 Runner 主机。
8. **Playwright 仅覆盖单浏览器（Edge）+ 单项目流程**：未覆盖 Firefox / WebKit 与多用户并行浏览器会话。
9. **租户级配额并发**：用户级 Provision 已串行化；租户级配额跨用户并发仍为“检查后创建”，极端并发下可能短暂超出，后续可用租户锁/配额表强化。
10. **部分故障注入未做真实破坏性演练**：Backend / Runner 重启、Sandbox stop→start、项目删除清理已实测；
    Runner 领取 Job 后被杀、Docker create 成功后 DB 更新前被杀、Docker Daemon 重启、
    Sandbox 进程崩溃、Preview Proxy 重启未做真实破坏性演练（幂等创建、租约回收、
    标签对账、自动恢复已有单元/集成测试覆盖；Preview Proxy 本身无本地状态）。
11. **资源压力矩阵未做极限破坏性压测**：PID fork bomb、磁盘写满、100+ 并发创建、
    大型 `npm install` 期间的生命周期操作未逐条真实压测；PID/CPU/内存限制已由容器
    inspect 与单测校验，预留/队列/配额并发已有单测覆盖。

## 6. 已知风险与规避

| 风险 | 规避 |
| --- | --- |
| Docker bridge 无法按容器阻断宿主 LAN | 生产宿主防火墙 / Egress Proxy；文档 §8 |
| 工作区写租约是粗粒度串行 | 第一阶段设计；后续评估 Git Worktree 并行 |
| 服务端口冲突 | DB 唯一约束 + 预检查；项目内端口唯一 |
| Preview Token 子域大小写 | Token 统一小写生成；Proxy 保留原始大小写匹配 |
| 大请求体/上传 | Runner Gateway 缓冲上限 20MB；Preview 单连接限制 |
| 多 Uvicorn Worker 并发播种档位 | 唯一键/死锁重试，失败不阻断启动 |
| 空闲回收误停 | 有活动 Agent Run 时跳过；只停计算资源，Volume 保留 |
| Runner 重启后 Gateway 失联 | 启动/对账周期重新接入 `platform.network`；集成测试覆盖 |
| 容器被外部删除 | Reconciler 入队 reconcile，`container_missing` 自动 start（10 分钟冷却） |
| 用户与 Agent 同时写文件 | 统一 Workspace 写租约，串行化并返回 409 |

## 7. 使用入口

- 对话页面顶部选择/新建项目；右侧面板 `文件 / 预览 / 服务`。
- 首次打开预览自动初始化/启动 Sandbox；服务健康后展示真实页面。
- 运维：`docs/workspace-sandbox-deployment.md`；指标 `/metrics`；Grafana 导入 `docker/grafana/workspace-sandbox-runtime.json`。
