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

## 3. 验证记录（真实执行）

| 验证 | 命令 / 方式 | 结果 |
| --- | --- | --- |
| 后端全量回归 | `cd backend && python -m pytest -q` | **1057 passed, 12 skipped, 102.01s** |
| Sandbox 专项（模型/调度/Lease/ACL/预览/工具/文件后端） | `pytest tests/test_sandbox_runtime.py tests/test_sandbox_security.py tests/test_sandbox_api.py -q` | **20 passed** |
| 真实 Docker 集成 | 在 Runner 容器内 `SANDBOX_DOCKER_TEST=1 pytest tests/test_sandbox_docker_integration.py` | **1 passed**（创建容器/网络/Volume、命令、文件一致性、只读 rootfs、网络隔离、Service 健康 + HTTP、删除容器后新 Generation 文件保留、幂等创建） |
| 真实端到端（服务层） | 容器内脚本：项目 → Provision → 命令（uid=10001）→ Service → Preview | `exec succeeded`、`service healthy`、子域预览 200、兜底 200、坏 Token 401 |
| 浏览器 E2E | `npm run test:e2e:sandbox`（真实 Edge + 已部署栈） | **1 passed 10.8s**：项目创建 → Sandbox Ready → Service healthy → iframe 真实页面 → 刷新后预览 → 切换项目不串显 → Services 面板；截图见 `docs/assets/` |
| 前端类型检查/构建 | `cd frontend && npm run build` | 通过 |
| Compose 校验 | `docker compose --profile sandbox --profile sandbox-image config -q` | 通过 |
| SQLite 迁移 | `alembic upgrade head` + `downgrade base` | 通过 |
| MySQL 迁移 | 容器 entrypoint `alembic upgrade head` 启动成功，Sandbox 全链路写入正常 | 通过（未单独执行 MySQL downgrade） |
| PostgreSQL 迁移 | 临时 PG16 容器：`upgrade head` + `downgrade base` | 通过 |

## 4. 安全验证要点

- 容器安全：`privileged=false`、`CapDrop=ALL`、`no-new-privileges`、`ReadonlyRootfs=true`、CPU/内存/PID 限制、非 root（uid 10001）均由集成测试 inspect 校验。
- 网络隔离：Sandbox 无法解析 `backend / mysql / redis / checkpoint_pg`（真实命令测试）；每个 Sandbox 独立 bridge，仅 Runner 加入。
- 数据面：Backend / Preview Proxy / Sandbox 均未挂载 Docker Socket；Runner 是唯一 Socket 持有者。
- 访问控制：跨用户项目 404；viewer 不能执行；成员移除后其 Preview Grant 立即撤销；坏 Token 401；过期/撤销/服务停止 401/403/410。
- 工具：未登记风险工具 fail-closed；restricted 模式拒绝执行类工具；命令/服务启动 HITL。
- 路径：文件工具仅允许 `/workspace`，拒绝宿主机风格路径与符号链接逃逸。
- 凭据：workspace-agent Token 由 Runner 派生注入；子进程环境移除该 Token；服务日志与命令输出脱敏。

## 5. 未完成 / 未验证事项（如实说明）

1. **多 Runner 实际调度未做双节点真机验证**：注册、心跳、容量、目标 Runner、排空接口已实现，单节点验收通过。
2. **磁盘硬配额未实测**：默认 `soft` 模式；`hard` 依赖 XFS project quota / Btrfs qgroup，本机环境未验证。
3. **MySQL downgrade 未执行**：仅验证升级与服务运行；SQLite / PostgreSQL 均已验证双向迁移。
4. **性能指标未做基准压测**：Warm Sandbox Ready P50/P95、命令执行 P50/P95、Vite 启动耗时未形成压测报告（当前仅记录单次端到端耗时）。
5. **Preview UI 状态刷新使用 4s 轮询**：后端已提供 `GET /api/v2/workspaces/{id}/events` SSE；前端当前用轮询保证状态及时可见，未接入 SSE 推送。
6. **未实现**：GPU Sandbox、Kubernetes / gVisor / Kata / Firecracker、Warm Pool、Hibernate、跨节点 Volume 迁移、完整在线 IDE、Terminal Tab、多 Agent 并行修改同一工作树。
7. **Docker Desktop 环境限制**：宿主机 LAN / 云元数据隔离需宿主防火墙；生产建议 Linux + 独立 Runner 主机。
8. **Playwright 仅覆盖单浏览器（Edge）+ 单项目流程**：未覆盖 Firefox / WebKit 与多用户并行浏览器会话。

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

## 7. 使用入口

- 对话页面顶部选择/新建项目；右侧面板 `文件 / 预览 / 服务`。
- 首次打开预览自动初始化/启动 Sandbox；服务健康后展示真实页面。
- 运维：`docs/workspace-sandbox-deployment.md`；指标 `/metrics`；Grafana 导入 `docker/grafana/workspace-sandbox-runtime.json`。
