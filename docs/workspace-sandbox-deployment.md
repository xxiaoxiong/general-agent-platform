# Workspace Sandbox Runtime 部署与运维手册

> 目标：每个 Project 拥有长期持久化 Workspace；Agent 在隔离 Docker Sandbox 中执行命令、
> 读写项目文件、运行 Service；用户在对话工作区 Preview 看到真实页面；单节点先行，
> 多 Runner 可扩展。架构与模型见 `docs/architecture/workspace-sandbox-runtime.md`，
> 性能基准见 `docs/workspace-sandbox-performance.md`。

## 1. 组件与安全边界

| 组件 | 部署形态 | Docker Socket | 职责 |
| --- | --- | --- | --- |
| backend | 现有 Compose 服务 | 否 | Project / Workspace / Job / 调度 / Service Registry / Preview Grant |
| sandbox-runner | 独立 Compose 服务（profile `sandbox`） | **是（唯一）** | 注册 / 心跳 / 领取 Job / Docker Provider / 对账 / Gateway |
| preview-proxy | 独立 Compose 服务 | 否 | Preview Grant 校验、子域路由、HTTP/WebSocket 转发 |
| workspace-sandbox 镜像 | Runner 动态创建容器 | 否 | workspace-agent（命令 / 文件 / Service） |
| project_verifier | 既有服务 | 否 | 短时离线工程验收（与 Sandbox 完全独立） |

- Backend / Preview Proxy / Sandbox 均不挂载 Docker Socket。
- 每个 Sandbox 使用独立 bridge network，只有 Runner 以网关身份加入；
  Sandbox 不加入 Compose `default` 网络，无法解析 `mysql/redis/backend` 等服务名。
- Preview Token 只存 SHA256；绑定 Sandbox Generation + Port，可过期/撤销。

## 2. 前置条件

- Docker Engine / Docker Desktop（支持 `docker compose`）。
- Runner 需要访问 `/var/run/docker.sock`；生产建议独立 Linux 主机。
- 宿主机资源建议：≥ 4 vCPU / 8GB RAM / 50GB 磁盘（可配置保留量）。
- 生产 Preview 需要独立域名（`PREVIEW_BASE_DOMAIN`）与 TLS；本地默认 `*.localhost`。
- Backend `8000`、Frontend `80`、Preview `8081` 默认只绑定 `127.0.0.1`，避免 Sandbox
  经宿主网关访问平台管理面；如需局域网访问，请通过受控反向代理（Caddy/Nginx）暴露。

> Docker Desktop（Windows/macOS）可以运行 Runner 容器并创建 Sandbox 容器；
> 但宿主机 LAN 隔离、磁盘硬配额与网络策略能力弱于 Linux，生产请使用 Linux。

## 3. 构建与启用

```bash
# 1) 构建 Sandbox 基础镜像（只需在升级时重建）
docker build -t general-agent-platform/workspace-sandbox:1.0.0 sandbox
# 或：docker compose -f docker/docker-compose.yml --profile sandbox-image build

# 2) 配置 docker/.env（gitignored）
#   SANDBOX_ENABLED=true
#   AGENT_SANDBOX_MODE=external          # 向模型开放执行类工具
#   SANDBOX_RUNNER_TOKEN=<openssl rand -hex 32>

# 3) 构建并启动（sandbox profile 会拉起 Runner）
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml --profile sandbox up -d

# 4) 校验
curl -s http://localhost:8000/api/health
docker logs -f ai_plat_sandbox_runner     # 应看到 “Sandbox Runner 注册成功”
curl -s http://localhost:8081/health      # preview-proxy
```

- `SANDBOX_ENABLED=false` 是总回滚开关：Sandbox API 返回 503、前端隐藏 Preview/Services、
  Agent 工具面不注入 Sandbox 工具、文件后端保持旧实现。
- 关闭 Sandbox 不会自动删除任何 Workspace Volume。
- `AGENT_SANDBOX_MODE` 必须为 `external` 才会向模型开放 `execute_command` /
  `start_service` 等执行类工具（fail-closed）。

### 3.1 关键配置（`app/core/config.py`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `SANDBOX_ENABLED` | false | 总开关 |
| `SANDBOX_RUNNER_TOKEN` | 空 | 启用时必填（≥16 位），Backend/Runner/Preview Proxy 共用 |
| `SANDBOX_ROLLOUT_USER_IDS` / `_PROJECT_IDS` | 空 | 灰度白名单（逗号分隔）；空=全部。不在名单内返回 503/404，工具与文件 API 同步拒绝 |
| `SANDBOX_RUNNER_CONTROL_URL` | `http://sandbox-runner:9100` | Backend → Runner Gateway |
| `SANDBOX_IMAGE` | `general-agent-platform/workspace-sandbox:1.0.0` | 固定镜像标签 |
| `SANDBOX_DEFAULT_PROFILE` | standard | 资源档位 |
| `SANDBOX_MAX_ACTIVE_PER_USER / _TENANT` | 2 / 20 | 活动 Sandbox 配额（0=不限） |
| `SANDBOX_IDLE_TIMEOUT` | 1800 | 空闲自动停止秒数（0=不回收） |
| `SANDBOX_NETWORK_POLICY` | egress | `none` 创建 internal 网络（完全断网） |
| `SANDBOX_VOLUME_QUOTA_MODE` | soft | 硬配额需宿主文件系统支持（见 §5） |
| `SANDBOX_CPU_RESERVE / MEMORY_RESERVE / DISK_RESERVE` | 2 核 / 2048MB / 20GB | Runner 可分配 = 总量 - 保留 |
| `SANDBOX_RUNNER_DISK_GB` | 100 | 节点磁盘记账总量 |
| `SANDBOX_JOB_LEASE_SECONDS` | 90 | Job 租约；过期由 Reconciler 回收 |
| `SANDBOX_COMMAND_TIMEOUT` | 600 | 单命令最长运行时间 |
| `SANDBOX_SYNC_COMMAND_WAIT` | 30 | 同步等待上限，超过转异步 Operation |
| `PREVIEW_BASE_DOMAIN` | localhost | Preview 子域后缀 |
| `PREVIEW_PUBLIC_BASE_URL` | 空 | 生产填写 `https://preview.example.com` |
| `PREVIEW_PROXY_HOST_PORT` | 8081 | 浏览器访问端口 |
| `PREVIEW_TOKEN_TTL` | 3600 | Preview Grant 有效期（秒） |
| `PREVIEW_MAX_CONNECTIONS` | 16 | 单 Grant 并发连接上限 |
| `PREVIEW_RATE_LIMIT_PER_MINUTE` | 600 | 单 Grant 每分钟请求上限（0=不限） |

## 4. 使用流程

1. 用户在对话顶部选择/新建项目（或新会话自动挂接「默认项目」）。
2. 打开右侧面板 → 「预览」/「服务」Tab；首次打开会自动初始化/启动 Sandbox。
3. Agent 使用工具：`execute_command`、`get_command_status`、`get_command_output`、
   `cancel_command`、`start_service`、`list_services`、`get_service_status`、
   `get_service_logs`、`stop_service`、`restart_service`、`get_preview_url`。
4. 服务健康后 Preview 区域展示真实页面；刷新页面会重新签发 Preview Grant。
5. Agent Run 结束不会停止 Sandbox / Service；空闲超时才停止（只释放 CPU/内存）。

### 4.1 文件一致性

- 项目会话的 `/workspace/` 由 `SandboxFilesBackend` 路由到 Sandbox 内的同一份文件；
  `read_file / write_file / edit_file / ls / glob / grep` 与 `execute_command` 看到同一份文件。
- 附件上传写入 `uploads/`（Sandbox 内），文件面板、预览、ZIP 下载均从 Sandbox 读取。
- 未归属项目的存量会话继续使用旧宿主机文件后端，互不影响。
- **写租约覆盖所有写路径**：Agent 工具、文件后端、用户上传/删除附件都先获取
  Workspace 写租约（owner=`run:{run_id}` 或 `user:{id}:{purpose}:{uuid}`），
  同一 Workspace 串行写；租约被占用时上传返回 `409 workspace_write_busy`。
- 每次工具/文件 API 调用都会重新校验 Project ACL；成员被移除后，
  已在运行的会话也不能继续读写该 Workspace。

### 4.2 Sandbox 内 workspace-agent 协议

workspace-agent 只监听 Sandbox 网络内的 `:19999`，使用 Runner 派生 Token
（`Authorization: Bearer <sha256(runner_token:sandbox_id:generation)>`），
由 Runner Gateway 转发，不对外暴露。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程 / 工作区可写 / 服务数 |
| POST | `/exec` | 启动命令，返回 operation（`wait_seconds` 内同步返回输出） |
| GET | `/exec/{id}` | 查询状态 / 输出 / 退出码 |
| POST | `/exec/{id}/cancel` | 杀死整个进程组 |
| GET | `/files/list` | 目录列表（仅 `/workspace`） |
| POST | `/files/read` | 按行窗口读取（文本/base64） |
| POST | `/files/write` | 写入（支持 `encoding=base64`） |
| POST | `/files/edit` | 精确字符串替换 |
| POST | `/files/delete` | 删除文件/目录 |
| POST | `/files/grep` | 字面量搜索 |
| POST | `/files/glob` | glob 匹配 |
| POST | `/files/raw` | 原始字节（base64，≤8MB） |
| GET | `/files/usage` | Volume 实际占用（du） |
| POST | `/services` | 启动长期服务 |
| GET | `/services` | 列出服务与状态 |
| GET | `/services/{name}` | 单服务状态 |
| POST | `/services/{name}/stop` | 停止服务（进程组） |
| POST | `/services/{name}/restart` | 重启服务 |
| GET | `/services/{name}/logs` | 日志尾部（后端脱敏） |

## 5. 存储、配额与清理

- Workspace Volume 名：`gap-ws-{sha256(project_id)[:24]}`，由服务端生成；
  固定挂载到容器 `/workspace`；容器删除不会删除 Volume。
- 资源档位（`sandbox_resource_profiles`）：
  - small：0.25 核请求 / 0.5 核上限 / 256MB 请求 / 512MB 上限 / 2GB 磁盘；
  - standard：0.5 / 1 核 / 512MB / 1GB / 5GB；
  - large：1 / 2 核 / 1GB / 2GB / 10GB。
- 磁盘配额：
  - `SANDBOX_VOLUME_QUOTA_MODE=soft`（默认）：记录 `disk_quota_bytes` 并在 Dashboard 监控，
    **不是**文件系统硬限制；
  - `hard`：仅当 Docker Storage Driver 与宿主文件系统支持（如 XFS project quota /
    Btrfs qgroup）时才有效，部署前必须实测并写入本手册；
  - 不得把 `du` 统计宣称为硬配额。
- 停止 Sandbox：释放 CPU / 内存预留，保留磁盘预留与 Volume 数据。
- 删除项目：默认保留 Volume（`purge_volume=false`）；显式
  `DELETE /api/v2/projects/{id}?purge_volume=true` 才在 Runner 删除作业中清理 Volume。
- 回滚 Backend 代码不会自动删除 Volume。

### 5.1 备份

```bash
# 备份单个 Workspace Volume（停止写入后执行）
docker run --rm -v gap-ws-<id>:/data -v "$PWD":/backup alpine \
  tar czf /backup/gap-ws-<id>.tgz -C /data .

# 恢复（目标 Volume 必须已存在；先停止 Sandbox）
docker run --rm -v gap-ws-<id>:/data -v "$PWD":/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/gap-ws-<id>.tgz -C /data"

# 数据库备份（项目 / Workspace / Service / Preview 元数据）
docker exec ai_plat_mysql mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" ai_plat \
  projects project_members workspaces sandbox_instances sandbox_jobs \
  sandbox_resource_reservations workspace_services preview_grants \
  sandbox_command_operations sandbox_events > sandbox_meta.sql
```

恢复顺序：数据库元数据 → Volume → 启动 backend → 在 Preview/Services 面板重新启动环境。
Sandbox 容器本身是可丢弃的，恢复后由 Runner 按 Desired State 重建并自动拉起 Service。

## 6. 故障排查

| 现象 | 排查 |
| --- | --- |
| 前端提示 “Sandbox 未启用” | 检查 `SANDBOX_ENABLED=true`、backend 重启、bootstrap 返回 `sandbox.enabled` |
| 创建环境 429「没有满足资源需求的 Runner」 | `docker logs ai_plat_sandbox_runner`；检查 Runner 是否注册、`SANDBOX_*_RESERVE` 是否过大、用户配额 |
| Job 长时间 queued | `/api/v2/workspaces/admin/capacity`（超管）查看 Runner 状态与预留；确认 Runner 心跳 |
| Job 反复 failed/requeued | 查看 `sandbox_events`（`sandbox.job_requeued`/`failed`）；常见：镜像不存在、Docker 403、Volume 被占用 |
| Sandbox 启动失败 `agent_unavailable` | 镜像内 workspace-agent 是否启动；`docker logs gap-sbx-*`；只读 rootfs 是否缺少 tmpfs |
| Agent 工具报「当前会话未绑定项目」 | 旧会话未迁移；新建对话或在顶部选择项目 |
| 工具被安全策略拒绝（restricted） | 设置 `AGENT_SANDBOX_MODE=external` 并重启 backend |
| Preview 打开 404/empty | Preview Proxy 未启动 / `PREVIEW_PROXY_HOST_PORT` 未映射；`*.localhost` 是否被浏览器解析 |
| Preview 401/410 | Grant 过期/撤销、Sandbox 停止、Service 停止；点「钥匙」重新获取 |
| Preview 白屏但服务 healthy | 检查 Service 是否监听 `0.0.0.0`；Vite 需 `--host 0.0.0.0`；应用是否强制 HTTPS |
| Vite HMR 断连 | 确认 Preview Proxy 与 Runner WebSocket 转发未被反向代理截断；Nginx 需 `proxy_read_timeout 3600s` |
| 容器 OOM | `docker inspect` 查看 `OOMKilled`；调大 profile 内存上限或优化依赖 |
| 磁盘写满 | 检查 Volume 使用量（Dashboard）；清理依赖缓存或扩容；`soft` 模式不会自动阻断 |
| Runner 崩溃后 Job 卡住 | 默认 90s 租约过期后自动重新入队；超过 `max_attempts` 标记 failed，可重新点启动 |
| Runner 重启后预览/命令 502 | Runner 启动与对账周期会自动重新接入所有既有 Sandbox 网络（`platform.network` 标签）；确认日志出现「Runner 已重新接入 N 个 Sandbox 网络」 |
| 容器被外部删除 | Reconciler 检测 `container_missing` 后自动入队 start（10 分钟冷却），并记录 `sandbox.auto_recover_queued`；连续失败会保持 degraded 并留事件 |
| npm/pip 安装报 EACCES | Runner 为 `/home/sandbox/.npm`、`.npm-global`、`.cache`、`.local` 挂载 tmpfs 时必须带 `uid=10001,gid=10001` 且 `exec`（否则非 root 无法写缓存 / 执行 npx 二进制） |
| Vite HMR WebSocket 失败 | Preview Proxy 必须转发 `?token=` 查询参数与 `Sec-WebSocket-Protocol: vite-hmr`；Runner 转发时只保留自定义 Origin，避免重复 Host/Origin/Protocol 头 |
| 服务在 Sandbox 重启后未恢复 | 服务需 `auto_start_on_sandbox_start=true` 且 `desired_state=running`；查看 `sandbox.ready` 事件中的 services |

日志关联字段：`tenant_id / user_id / project_id / workspace_id / sandbox_id / generation /
run_id / job_id`。禁止把 Token、密码、API Key 写入日志。

## 7. 指标与 Dashboard

- Backend `/metrics` 与 Runner `/metrics` 暴露同一注册表的沙箱指标：
  `sandbox_runner_online`、`sandbox_runner_heartbeat_age_seconds`、`sandbox_active_count`、
  `sandbox_queue_depth`、`sandbox_queue_wait_seconds`、`sandbox_provision_duration_seconds`、
  `sandbox_start_duration_seconds`、`sandbox_cpu_reserved`、`sandbox_cpu_usage`、
  `sandbox_memory_reserved_bytes`、`sandbox_memory_usage_bytes`、`sandbox_volume_usage_bytes`、
  `sandbox_job_retry_total`、`sandbox_reconcile_mismatch_total`、`sandbox_oom_total`、
  `workspace_service_restart_total`、`workspace_service_health`、`preview_request_total`。
- Grafana 导入 `docker/grafana/workspace-sandbox-runtime.json`（与现有 Runtime 看板同目录）。
- **预留值与实际值可区分**：`*_reserved` 来自数据库预留账本；`sandbox_cpu_usage` /
  `sandbox_memory_usage_bytes` / `sandbox_volume_usage_bytes` 由 Runner 在每轮对账中
  采样（Docker stats + workspace `du -sb`，限流采样，失败不影响业务）。

### 7.1 Vite HMR / WebSocket 验证

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -e SANDBOX_DOCKER_VITE=1 \
  -e DATABASE_URL=sqlite+aiosqlite:////tmp/sandbox_vite.db \
  -e DATABASE_SYNC_URL=sqlite:////tmp/sandbox_vite.db \
  sandbox-runner python -m pytest tests/test_sandbox_docker_vite.py -q -s
```

测试会在 Sandbox 内安装并启动 Vite，连接 `vite-hmr` WebSocket，修改源码后断言
收到 `update` / `full-reload` 消息。Preview Proxy 链路验证见交付报告。

## 8. 安全清单

- [ ] 生产 `SANDBOX_RUNNER_TOKEN` ≥ 32 位随机值，经 Secret 注入，不提交仓库。
- [ ] Backend / Preview Proxy / Sandbox 均未挂载 `/var/run/docker.sock`。
- [ ] Sandbox 容器 `privileged=false`、`cap_drop=ALL`、`no-new-privileges`、只读 rootfs、
      CPU/内存/PID 限制、非 root 用户。
- [ ] Sandbox 不加入平台网络；无法解析数据库 / Redis / Runner / 其他项目容器。
- [ ] 生产 Preview 使用独立 HTTPS 域名，未向 Preview Origin 下发平台 Cookie。
- [ ] 宿主机防火墙阻断 Sandbox 网段访问 LAN 管理地址与云元数据（`169.254.169.254`）。
- [ ] 普通用户不能修改资源档位、配额、镜像、Runner。
- [ ] Backend / Frontend / Preview Proxy 端口默认只绑定 `127.0.0.1`；对外访问经受控反向代理。
- [ ] Project 成员移除后 Preview Grant 立即失效（后端 `revoke_grants_for_user`）。

## 9. 已知限制（第一阶段）

- 单 Runner 单节点；多 Runner 调度接口已具备（注册 / 心跳 / 容量 / 排空），但未做跨节点
  Volume 迁移与镜像预热。
- 未实现 GPU Sandbox、Kubernetes / gVisor / Kata / Firecracker、Warm Pool、Hibernate。
- 磁盘硬配额依赖宿主文件系统能力；默认 soft 模式。
- Docker bridge 无法按容器阻断宿主机 LAN / 云元数据；生产需宿主防火墙或 Egress Proxy。
- Preview 使用 Host 重写为 `localhost:{port}` 兼容 Vite allowedHosts；应用若依赖绝对
  `Host` 生成 URL，请使用 `X-Forwarded-Host`。
- 未实现完整在线 IDE 与多 Agent 并行修改同一工作树；写租约保证同一 Workspace 串行写。
- 未实现「管理员关闭外部分享、仅登录用户可看预览」的预览登录态模式；当前授权边界是
  可过期/可撤销的 Preview Grant（默认 1 小时）。
- Preview 仅支持 HTTP Web App；WebSocket 已支持（Vite HMR 实测通过）。
