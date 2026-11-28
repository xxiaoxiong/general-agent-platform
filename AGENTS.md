# AGENTS.md — AI 编码代理协作指南

本文件面向在本仓库（General Agent Platform）中工作的 AI 编码代理，描述架构约定、常用命令与已知陷阱。动手改代码前请先通读「已知陷阱」一节。

## 项目概述

可私有化部署的企业级通用 Agent 平台：多模型对话、Agent 智能体（LangGraph 执行内核 + Durable Execution）、企业知识库（RAG）、可视化流程编排、提示词工程、应用市场，以及 Langfuse 全链路可观测与 Agent 自进化闭环。

- 代码注释、提交信息、文档统一使用**中文**（标识符用英文）。
- 后端 Python 3.11 + FastAPI + SQLAlchemy(async)，前端 Vue 3 + TypeScript + Element Plus。

## 目录结构

```
backend/
  app/api/            # 路由按产品模块分包（URL 前缀保持 /api/v1、/api/v2 不变）
    identity/         # auth / users / audit
    model/            # models / prompts / asr
    knowledge/        # knowledge / databases
    workflow/         # workflows / app_market
    assistant/        # assistants / capabilities
    chat/             # conversations / files
    run/              # Run 域（core 生命周期 / admin 核对与返工 / review 文档审阅）
    document/         # documents / document-profiles
    workspace/        # workspace / artifacts
    ops/              # admin / evaluation / evolution / skills / mcp
    common/           # schemas / serializers（无路由）
  app/core/           # config.py（全量配置，启动期校验）、database.py、security、redis
  app/models/         # SQLAlchemy 模型（agent.py / evolution.py 等）
  app/schemas/        # Pydantic 模式（v1 模块）
  app/services/       # 业务逻辑
    agent_runtime/    # Agent 执行内核（service/ 包 = core + lifecycle/stream/approval/
                      #   delivery/config 五个 Mixin + _common，导入路径兼容层见 service/__init__）
      team/           # Agent Team Runtime（Member Runtime / Task Board / Message Bus /
                      #   Artifact Store / Team Tools / LeaderRunner / Collaboration）
    knowledge/        # 知识库解析/切分/Embedding/Qdrant/检索（parsers/ embedding/
                      #   chunking/ retrieval/ ingestion/ evaluation/ acl/ security/ storage）
    assistant/        # 助手域（capability / versions / task_contract / verification / model_router）
    audit/            # 审计（core / capture / context / http）
    documents/        # 文档智能
    evolution/        # 自进化闭环（metrics / judge / evolution_advisor / dev_briefing）
    evaluation/       # Agent 测评
    inference/        # 模型连接池、请求队列
    mcp/              # MCP 客户端
    sandbox/          # Workspace Sandbox 控制面（manager/runtime/scheduler/jobs/preview/
                      #   service_manager/agent_client/write_lease/reconciler/session_files）
    tools/            # 工具注册表与执行器（含 sandbox_tools.py）
    conversation_memory/ # 历史会话检索（planner/retrieval/context_builder/indexer/
                      #   worker/repository/tools/service；Turn+Segment 索引，ACL 服务端强制）
    work_orchestrator/ # 工作任务编排层（schemas/planner/validator/registry/
                      #   graph/adapters/integration；跨 DeepAgents/Document/Team/Project）
  app/sandbox_runner/ # 独立 Sandbox Runner 进程（Docker Provider / Job 执行 / Gateway）
  app/knowledge_worker/ # 独立 Knowledge Ingestion Worker（解析/OCR/Embedding/Qdrant 写入）
  app/preview_proxy/  # 独立 Preview Proxy（Preview Grant 校验 + HTTP/WS 转发）
  app/browser_service/ # 独立内置浏览器（无头 Chromium + CDP 帧流 + 输入转发）
  app/observability/  # langfuse_tracing.py、otel_tracing.py、metrics.py
  alembic/            # 数据库迁移
  tests/              # pytest 测试套件
frontend/
  src/api/            # API 封装按模块一文件（auth/models/.../conversations/runs/artifacts/
                      #   sandbox 等，index.ts 仅作薄 barrel re-export）
  src/views/          # 页面组件（workspace/、system/ 等按业务分子文件夹）
  src/features/       # 业务功能组件（run/ documents/ preview/）
sandbox/              # workspace-sandbox 基础镜像（Dockerfile + workspace_agent.py）
docker/               # docker-compose.yml + Dockerfile + entrypoint + grafana
docs/                 # 需求设计、任务书、阶段交付说明（历史文档，勿改写）
```

## 常用命令

```bash
# 后端测试（必须在 backend/ 目录下执行，否则 ModuleNotFoundError: app）
cd backend && python -m pytest -q                 # 全量回归（~1190 passed, 13 skipped；必须单进程前台）
cd backend && python -m pytest tests/test_xxx.py -q   # 单文件
cd backend && python -m pytest tests/test_work_orchestrator_graph.py tests/test_work_planner.py tests/test_work_plan_schema.py tests/test_work_orchestrator_integration.py -q  # Work Orchestrator
cd backend && python -m pytest tests/test_conversation_memory.py tests/test_conversation_memory_api.py -q  # Conversation Memory
cd backend && python -m pytest tests/test_goal_runtime.py tests/test_goal_mode_steering.py -q  # Goal Runtime + Deep Research
cd backend && python -m pytest tests/test_team_runtime.py tests/test_team_graph.py tests/test_team_project_runtime.py -q  # Agent Team Runtime
cd backend && python -m scripts.conversation_memory_eval --report ../docs/conversation-memory-eval-report.md  # 历史检索质量评测
cd backend && python -m scripts.goal_research_eval --report ../docs/goal-research-eval.md  # Goal/Deep Research 质量与成本评测
cd backend && python -m pytest tests/test_knowledge_rag.py -q  # Knowledge RAG（真实解析/Embedding/Qdrant）
python backend/tests/e2e/team_mock_model.py --port 9999  # 确定性团队 E2E 端点（OpenAI 兼容）

# 知识库迁移与评测（backend/ 目录）
cd backend && python -m scripts.migrate_legacy_knowledge          # 旧索引体检
cd backend && python -m scripts.migrate_legacy_knowledge --apply --run
cd backend && python -m scripts.knowledge_eval --kb-id 1 --dataset golden.json --mode hybrid

# 前端（vue-tsc 类型检查 + 构建）
cd frontend && npm run build
cd frontend && npm run dev

# Docker（compose 项目名 general-agent-platform）
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f backend

# Workspace Sandbox（项目级持久化开发环境）
docker build -t general-agent-platform/workspace-sandbox:1.0.0 sandbox
docker compose -f docker/docker-compose.yml --profile sandbox up -d
# 真实 Docker 集成测试（在挂载 socket 的 Runner 容器内执行）
docker compose -f docker/docker-compose.yml run --rm -e SANDBOX_DOCKER_TEST=1 \
  -e DATABASE_URL=sqlite+aiosqlite:////tmp/it.db -e DATABASE_SYNC_URL=sqlite:////tmp/it.db \
  sandbox-runner python -m pytest tests/test_sandbox_docker_integration.py -q
# 真实 Vite HMR / WebSocket（需 npm 网络，可设 SANDBOX_NPM_REGISTRY）
docker compose -f docker/docker-compose.yml run --rm -e SANDBOX_DOCKER_VITE=1 \
  -e DATABASE_URL=sqlite+aiosqlite:////tmp/vite.db -e DATABASE_SYNC_URL=sqlite:////tmp/vite.db \
  sandbox-runner python -m pytest tests/test_sandbox_docker_vite.py -q
# 性能基准（Backend 容器内，输出环境/样本/P50/P95）
docker cp backend/scripts/sandbox_benchmark.py ai_plat_backend:/tmp/sandbox_benchmark.py
docker exec -e PYTHONPATH=/app -w /app -e BENCH_ITERATIONS=3 ai_plat_backend \
  python /tmp/sandbox_benchmark.py

# 数据库迁移
cd backend && python -m alembic upgrade head
```

## 架构约定

- **异步优先**：API 与 service 层使用 `AsyncSession`（`app/core/database.py`）；阻塞调用（Langfuse flush 等）用 `asyncio.to_thread` 包裹。
- **API 模块化**：路由按产品模块分包（`app/api/<模块>`）；URL 前缀 `/api/v1`（传统平台模块）与 `/api/v2`（智能体工作台+运维）为历史契约保持不变。Run 生命周期唯一归属 `/api/v2/runs`（旧 `/api/v1/agents/*` 端点已删除，勿恢复）。
- **配置集中**：所有配置项在 `app/core/config.py` 的 `Settings`，启动期 `validate_startup()` 直接拒绝非法配置（如生产环境 `AGENT_CHECKPOINT_DSN=memory`）。新增配置只加在这里 + `.env.example`。
- **建表走 `init_db()`**（`Base.metadata.create_all`）：只自动补**新表**，不加新字段；改已有表结构必须写 Alembic 迁移。
- **权限**：v2 evolution 等管理端点要求超管（`require_superuser`）；业务端点按 RBAC。
- **可观测**：Agent Run 上报 Langfuse trace（确定性 `trace_id = create_trace_id(seed=f"run:{run_id}")`，15-span 树）；trace URL 必须为 `{base}/project/{project_id}/traces/{trace_id}`（缺 projectId 会 404）。评估结果回写 Langfuse scores。
- **自进化闭环**：`evolution/metrics.py` 从 `agent_tool_executions` / `agent_approvals` / `agent_run_events` 算确定性指标；`judge.py` 做六维 LLM 裁判；`evolution_advisor.py` 生成建议并支持应用/回滚/验证；`dev_briefing.py` 汇总**纯客观数据**供开发迭代决策。
- **Durable Execution**：LangGraph state 由共享 PostgreSQL Checkpointer（`checkpoint_pg` 服务）持久化，多 Worker / 重启可恢复。
- **Work Orchestrator**：复合任务由 `services/work_orchestrator` 规划、校验和跨引擎调度；简单任务不额外调用 Planner。一个用户任务仍只有一个 `AgentRun` / `RunWorker` / EventBus；编排状态在 `work:{run_id}` Checkpoint，步骤执行器在 `work:{run_id}:step:{step_id}`；计划快照在 `input_payload.execution_context.work_plan`；`work.plan.*` / `work.step.*` 事件独立于 DeepAgents 的 `plan.updated`。
- **Conversation Memory**：原始会话 `agent_sessions.messages` 是唯一事实来源；派生索引分 `turn` / `segment` 两级，写回事务入队 Durable Job，独立 `ConversationIndexWorker` 执行；自动预取在语义决策后、`build_agent` 前，Planner 失败/超时降级为无历史上下文；历史内容作为独立不可信资料块注入，不授予工具权限；检索 ACL 由服务端 SQL/Qdrant Filter 强制，来源必须回查会话与消息仍存在。
- **Work Orchestrator 执行器**：`executor_profile` 只能由服务端 Registry 解析到真实引擎（DeepAgents / Document Graph / Team Graph / Team Project Graph）；模型不能指定工具名、类名、URL、工作区路径或权限。默认 `AGENT_WORK_ORCHESTRATOR_MODE=shadow`，回滚用 `off`。
- **Agent Team Runtime**：`AGENT_TEAM_RUNTIME_ENABLED=true` 时 Leader/Member 均为完整 DeepAgents Agent；Leader 规划/复核/综合通过 Team Tools 操作 Task Board，Member 在独立 Thread 执行；Task/Member/Message/Artifact 是协作事实来源，LangGraph 只做控制流。`team_run_id = agent_runs.id`；成员 Thread `team:{run}:member:{member_id}`，Leader 阶段 Thread `:synthesis`/`:review`；Task Board 用 revision CAS + lease token，Message Bus 至少一次投递 + 幂等键，Artifact 引用式共享。软件项目团队仍走原 `team_project_graph`；回滚用 `AGENT_TEAM_RUNTIME_ENABLED=false`。
- **Goal Runtime / Deep Research**：`/goal` 是目标控制层，不新建 Agent Runtime。`goal_contracts` 持久化原始目标、稳定验收 ID（AC-xxx）、revision 与进度；`goal_iterations` 记录逐轮结构化结果；Goal Supervisor（`services/goal_runtime/supervisor.py`）在 CompletionMiddleware 中按验收条件决定继续/完成/暂停/受阻，Todo、模型自述与报告标题都不能单独证明完成。研究类目标启用 `services/research`（ResearchPlan/Source/Evidence/Claim + 确定性 Verifier），单轮执行仍由现有 DeepAgents + 研究工具完成。Goal 模式保留 `GOAL_MAX_*` 全局安全预算，不再解除全部限制。

## 测试约定

- 测试文件：`backend/tests/test_*.py`，pytest-asyncio 风格（async 测试函数）。
- 每个测试自建数据、结束清理（参照 `test_evolution_dev_briefing.py` 的 `_CLEAN` 清理元组模式）。
- 改后端逻辑后必须跑全量 `python -m pytest -q`；改前端跑 `npm run build`。
- 新功能必须带测试（服务层 + API 层）。

## 已知陷阱（务必遵守）

1. **pytest 必须在 `backend/` 目录下运行**。在仓库根目录跑会 `ModuleNotFoundError: No module named 'app'`。
2. **MySQL sort buffer 限制**：`agent_runs` 表含大 JSON 列（`input_payload` / `agent_config_snapshot` / `summary` / `final_text`）。禁止 `select(AgentRun).order_by(created_at.desc())` 全行排序（会报 `1038 Out of sort memory`）。正确做法：窄列投影（`select(AgentRun.id, AgentRun.status, ...)`）+ Python 侧 `sorted(..., key=lambda r: r.created_at or datetime.min, reverse=True)`。`agent_approvals`、`evolution_change_logs` 同理。
3. **docker 镜像烘焙代码**：改了 `backend/` 或 `frontend/` 代码后必须 `docker compose build` + `up -d` 才在容器生效，仅改文件不重建镜像无效。
4. **容器内跑脚本**：`docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/xxx.py`（缺 `PYTHONPATH` 会找不到 `app` 包）。
5. **PowerShell 的 `$` 插值**：含 `$2a$...`（bcrypt）等内容的 SQL 会被双引号破坏。写入 `.sql` 文件后用 `Get-Content -Raw | docker exec -i <psql容器> psql` 管道执行。
6. **Langfuse trace 链接必须带 projectId**：`{base}/project/{project_id}/traces/{trace_id}`。另外跨组织访问 trace 会报 `You do not have access to this trace`（多租户隔离），排查链接问题先确认当前登录用户所属组织。
7. **docker 数据卷名已固定**：`docker-compose.yml` 中 `mysql_data` 等卷显式固定为 `ai-generalplat_*` 历史名称（项目改名遗留），**不要"顺手清理"**，否则重建容器丢数据。
8. **开发简报只放客观数据**：`dev_briefing.py` 及「开发简报」Tab 是用户明确要求的纯客观数据收集/整理/呈现（零 LLM 调用）。**不要**往里加聚类、优化建议等 AI 主观判断；方向判断由用户/开发 AI 基于数据自行做出。
9. **Langfuse 密钥经环境变量注入**：`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_PROJECT_ID` 在 `docker/.env`（gitignored），代码里不要硬编码。
10. **端口约定**：前端 80（容器）/ 5173（dev）、后端 8000（容器）/ 8002（dev）、MySQL 3308、Redis 6379、checkpoint_pg 5433、Langfuse 3100。
11. **`agent_runtime/service/` 是导入兼容层**：该包由原 `service.py` 机械拆包而来（core + 五个 Mixin + `_common`），`__init__.py` re-export 全部旧符号，既有 `from app.services.agent_runtime.service import ...` 无需改动。测试 monkeypatch 目标 `service.build_agent` / `service._service` / `service.settings` 必须保持在**包级属性**；各子模块持有独立的 `AsyncSessionLocal` 绑定，替换会话工厂时需逐一补丁（见 `test_document_runtime.py` 的 platform fixture）。新增模块级共享符号放 `_common.py`。
12. **Workspace Sandbox 只有 Runner 能碰 Docker**：`app/sandbox_runner` 是唯一挂载 `/var/run/docker.sock` 的进程；Backend / Preview Proxy / Sandbox 容器都不得挂载 Socket。Sandbox 容器只能由 Runner 的 `DockerEngine`（unix socket HTTP）创建，HostConfig 全部服务端生成，不接受模型/用户传入镜像、挂载或 Capabilities。
13. **启用 Sandbox 需两个开关同时打开**：`SANDBOX_ENABLED=true` 且 `AGENT_SANDBOX_MODE=external`；只开第一个时执行类工具会被 `ToolPolicy` 以 restricted 拒绝（fail-closed）。Runner/Preview Proxy 是独立 Compose 服务，需 `--profile sandbox` 启动（`preview-proxy` 默认启动，Runner 在 profile 内）。
14. **项目会话的文件与命令必须同一份 `/workspace`**：项目会话由 `SandboxFilesBackend` 把 DeepAgents 文件工具路由到 Sandbox；未归属项目的存量会话继续使用 `data/agent_files/{user}/{agent}/{session}`。改文件工具时不要只改一边，附件上传 / file-list / file-preview / file-bytes / files.zip 都有 sandbox 分支（`app/services/sandbox/session_files.py`）。
15. **Sandbox 工具需要 Run 上下文**：`execute_command` / `*_service` 等走 `ToolExecutor` 的 `_CONTEXT_HANDLERS(ctx, params)`；Run 的 `project_id` 只能由 `session.project_id` 推导（`RuntimeContext.project_id`），不得从模型参数读取。写操作先获取 Workspace 写租约（`write_lease.py`，owner=`run:{run_id}`），Run 结束在 `_drive` finally 释放。
16. **本地 Preview 地址是 `{token}.localhost:8081`**：`*.localhost` 由浏览器解析到回环；生产必须 `PREVIEW_PUBLIC_BASE_URL=https://preview.example.com`（启动校验要求 https）。Preview Proxy 只代理 Grant 中登记的 Sandbox + Port，禁止任意 upstream URL；本地若浏览器不解析子域，用 `/p/{token}/` 兜底地址。预览面板默认改用内置浏览器（服务端 Chromium），客户机无需访问该端口；「直连」模式才使用上述地址。
17. **Sandbox 集成测试必须真实 Docker**：`test_sandbox_docker_integration.py` 只在 `SANDBOX_DOCKER_TEST=1` 时运行，且必须在挂载 Docker Socket 的容器内执行（DockerEngine 只支持 unix socket，Windows 宿主直跑不可用）；测试会创建/删除真实容器、网络与 Volume，名称带随机后缀。
18. **Sandbox 用户目录 tmpfs 必须 `uid/gid=10001` 且 `exec`**：Docker tmpfs 默认 root 所有并 `noexec`，否则 npm/pip 缓存 EACCES、`npx`/`vite` 二进制 Permission denied。修改 `executor._container_spec` 的 Tmpfs 时不要丢掉这组参数。
19. **Runner 重启后必须重新接入既有 Sandbox 网络**：Runner 容器重建不会保留动态加入的 `gap-net-*` 网络；`RunnerExecutor.attach_managed_networks()` 在启动与每轮对账执行，否则 Gateway 访问既有 Sandbox 全部 502。改 Runner 生命周期时保留该调用。
20. **Preview WebSocket 路径要去掉 `/p/{token}` 前缀**：Preview Proxy 的 `_bridge_ws` 必须使用 `_extract_token` 返回的 rest（否则 Vite 收到错误路径）；Runner 转发 WS 时不要重复发送 Host/Origin/Sec-WebSocket-Protocol（Vite 会挂起/400）。Vite 8+ 还需原样透传页面客户端自动附加的 `?token=`。
21. **登录态预览依赖首屏 Set-Cookie**：`PREVIEW_REQUIRE_LOGIN=true` 时前端把短期 `access_token` 拼到 iframe URL；Preview Proxy 校验 Grant+User+Project ACL 后必须给文档响应追加 `preview_access` HttpOnly Cookie，子资源与 WebSocket 才能继续访问。修改 `_forward_http` 返回/头过滤时不要丢掉 Set-Cookie。
21. **Sandbox 写路径全部要 ACL + 写租约**：工具/文件后端/附件上传删除每次调用都复核 Project 角色（成员移除立即失效），并走 Workspace 写租约；新增写入口不要绕过 `session_files.acquire_user_write_lease` / `write_lease.acquire_write_lease`。
22. **灰度开关**：`SANDBOX_ROLLOUT_USER_IDS` / `SANDBOX_ROLLOUT_PROJECT_IDS` 非空时仅名单内启用；bootstrap、会话挂项目、工具、ACL 必须统一走 `sandbox_enabled_for()`，不要只判断 `SANDBOX_ENABLED`。
23. **项目会话的验收/续做/冻结/附件指纹必须读 Sandbox**：`completion._collect_evidence`、`prepare_completion_continuation`、`freeze_artifacts_async`、新产物 ZIP、`resolve_attachment_refs_async` 都按会话后端分流（项目会话 → `/workspace`，存量会话 → `data/agent_files/...`）。只改宿主机会话目录会出现“用户看得见文件、验收却报没有写入记录”的假失败。
24. **每个 Sandbox 一个 bridge 网络，必须管理好生命周期**：删除时先断开 Runner 再删网络；Runner 启动/对账周期清理无容器的孤儿网络（仅清理创建超过 10 分钟的网络，防误删 Provision 中的网络）；并发多时配置 `SANDBOX_NETWORK_SUBNET_POOL`，否则 Docker 默认地址池耗尽会报 `all predefined address pools have been fully subnetted`（新环境创建 400）。
25. **Sandbox 长期服务必须监听 `0.0.0.0` 且由 `start_service` 管理**：`execute_command` 结束后 workspace-agent 会清理残留后台进程（`&`/nohup 启动的 dev server 不可用）；健康检查同时探测 IPv4/IPv6 回环与容器非回环 IP，仅监听 localhost 会标记 unhealthy 并写提示；`start_service` 启动前检测端口占用；同名同端口服务停止后可用新命令重新启动（更新原记录）。
26. **内置浏览器是独立 `browser-service` 进程**：`app/browser_service` 用无头 Chromium + CDP screencast，经 `{token}.preview.internal`（Preview Proxy，Grant 校验）访问 Sandbox；不挂 Docker Socket、不映射端口、不加入 Sandbox 网络。`BROWSER_SERVICE_TOKEN` 为空时 fail-closed；镜像必须安装 `uvicorn[standard]`（否则 WebSocket 升级 404）；Nginx `/api/v2/workspaces/browser-sessions/` 必须转发 `Upgrade`，Vite dev 代理需 `ws: true`。会话 JWT 绑定用户/项目/服务，WS 连接每次复核 ACL。
27. **Preview Proxy / Runner Gateway 必须原样转发查询串**：不要用 `request.query_params` 重新编码（空值标记会变成 `key=`）。Vite 的 `?vue&type=style&index=0&lang.css` 被改写后会把样式模块当直接 CSS 返回，浏览器执行 CSS 报 `Unexpected token '*'`、页面白屏。使用 `_forward_query` / `_raw_query` 保留原始 query string。
28. **Knowledge 解析 / OCR / Embedding 只在 `knowledge-worker` 执行**：Backend 请求进程只做控制面与检索查询；不要在 API 里恢复 `BackgroundTasks` 长任务。Job 以 MySQL 为权威队列（Lease/Heartbeat/attempt），Redis 最多做唤醒。
29. **Embedding 模型 / 维度变更必须开新 Generation**：Qdrant Collection 由 `模型+维度` 决定，Payload 带 `index_generation_id`；检索过滤只能由服务端注入，不允许模型/前端传任意 kb/owner 条件。维度不匹配必须显式失败，不得静默写入。
30. **低相关拒答阈值必须按评测集校准**：不要用"本次查询最高分"归一化冒充相关性。`KNOWLEDGE_MIN_DENSE_SCORE` 是绝对余弦阈值；`KNOWLEDGE_MIN_LEXICAL_COVERAGE` 用 IDF 覆盖率过滤只命中"的/是"等常见字的 BM25 噪声。改阈值后跑 `scripts/knowledge_eval.py` / 质量评测页对比 Recall、No-answer Accuracy。
31. **上传必须流式 + Magic Bytes**：禁止恢复 `await file.read()` 全量读内存；Office ZIP 必须检查解压大小/条目数（压缩炸弹）；未支持格式明确拒绝，不能按 UTF-8 文本兜底后标记成功。
32. **解析失败不得冒充成功**：`done` 语义已废弃，只有 Chunk + Qdrant Point 数校验通过才写 `ready/partial`；`empty_extraction` / `source_missing` / `unsupported_format` 必须是 failed 或 migration_required。
33. **删除顺序固定**：先标记 `deleting`（阻止检索命中）→ 删 Qdrant Points → 删 Chunk → 删解析产物/源文件 → 删版本/Job/文档行 → 重算统计。不要先删索引再因事务失败留下"表面正常但无法检索"的知识库。
34. **Qdrant 数据卷名固定**：`ai-generalplat_qdrant_data`，不要重建容器时丢索引；Backend/Worker 都不允许把 Qdrant 端口映射到宿主/公网（`KNOWLEDGE_QDRANT_API_KEY` 生产必须设置）。
35. **Docling 只在 Worker 镜像**：Backend 镜像装 fastembed/rapidocr/pypdfium2，不装 Docling（重型解析依赖）；`KNOWLEDGE_PARSER_PROVIDER=auto` 时 Worker 用 Docling、宿主机/测试用 Native 结构化解析器。新增格式解析不要把所有逻辑堆进 `knowledge_service.py`（旧文件仅保留回退链路）。
36. **知识库 ACL 与 Agent 绑定是两层防线**：所有 API 走 `knowledge_acl.require_base_access`（owner/项目角色/组织只读），Agent Tool 每次检索再用 Run Context 复核用户 ACL；无权限统一 404（防 IDOR 枚举）。
37. **Work Orchestrator 不得新建第二套 Run 生命周期**：步骤复用同一 `AgentRun` / `RunWorker` / Lease / EventBus / ToolPolicy / Ledger；不要在 `work_orchestrator` 里再建 Worker、队列或 Run 状态机。编排图只做调度与验收。
38. **编排图 Thread 与步骤 Thread 必须分离**：编排状态用 `work:{run_id}`；步骤执行器用 `work:{run_id}:step:{step_id}`（重试用 `:attempt:N`）。不要复用 `agent-{id}-session-{sid}`，否则与直接路径串状态。
39. **WorkPlan 快照写入必须带 Lease/Fencing**：运行态写 `agent_runs.input_payload.execution_context.work_plan` 前必须 `verify_running_lease`；暂停态不写快照（Run 状态 + Checkpoint 是权威），不要无保护覆盖整个 `input_payload`。
40. **WorkPlan 事件不得复用 `plan.updated`**：`plan.updated` 是 DeepAgents Todo；计划事件必须走 `work_plan_*` → `work.plan.*` / `work.step.*`，且存储上限不能落回默认 400 字符（否则计划无法回放）。`work_step_id` 只做 nullable 扩展，旧账本读取路径保持兼容。
41. **Conversation Memory 索引必须与消息写回同事务入队**：`_deliver_outbox` 写 Assistant 消息后入队 `conversation_index_jobs`；不要改成请求内同步 Embedding，否则拖慢写回/首 Token。索引 Worker 是独立循环（`CONVERSATION_MEMORY_INDEX_ENABLED`），不要塞进 RunWorker 主轮询。
42. **历史检索的 ACL 只能服务端计算**：`scope_hint`、前端 `scope`、模型工具参数都不是授权；SQL 必须带 `user_id` 或“显式共享 + 项目成员”，Qdrant Filter 由服务端生成，结果还要回查会话/消息存在性与共享未撤销。
43. **低相关拒答阈值必须按评测集校准**：短会话的无关 Dense 相似度常见 0.42~0.55，不能只用单一绝对阈值；现有 `MIN_DENSE_SCORE=0.5` + `DENSE_STRONG_SCORE=0.62` + `DENSE_MARGIN=0.04` 是按 32 条评测集校准的结果，改阈值后必须重跑 `scripts.conversation_memory_eval.py` 并对比 Recall / No-answer Accuracy。
44. **历史内容是不可信资料**：只能作为独立 Context Block 注入，不得伪造成用户消息、不得继承工具授权、不得覆盖系统指令；`history.sources` 事件的来源 ID 必须由服务端校验后才返回给前端。
45. **更换 Embedding 模型/维度必须递增索引代际**：`CONVERSATION_MEMORY_INDEX_VERSION` 参与条目过滤与 Qdrant Collection 名；不要复用旧版本静默混用维度，重建走 `POST /api/v2/memory/reindex`。
46. **成员 Agent 必须显式传 general-purpose 占位**：deepagents 在子代理清单缺少 `general-purpose` 时会自动注入全工具默认子代理，绕过平台白名单；占位必须 `tools=[]` 且带空文件栈 middleware。
47. **FilesystemMiddleware 不能空工具**：DeepAgents 基栈要求 `FilesystemMiddleware` 存在且 `tools` 必含 `read_file`；成员只暴露虚拟 StateBackend 的 `read_file`，不得传入真实 backend 或 `write_file/edit_file/execute`。
48. **Team 表主键作用域**：`team_run_id = agent_runs.id`；`member_id`/`task_id` 只在团队内唯一（`UniqueConstraint(team_run_id, member_id/task_id)`），不要改回全局唯一，否则不同 Run 的 `leader`/`research` 会冲突。
49. **Task Board 不得先查后改**：认领/完成/失败/更新必须用条件 UPDATE（revision + owner + lease_token）的 rowcount 判定；`fail` 不递增 `retry_count`，`retry` 才递增，避免少一次重试。
50. **Leader 阶段 Thread 必须隔离**：规划用成员基础 Thread，复核/综合用 `:review`/`:synthesis`；规划被预算/限流中断且任务已创建时调用 `LeaderRunner.rotate_thread()`，否则综合会续跑未完成规划并输出规划文本。
51. **成员工具面是运行时收窄，不是权限绕过**：Team Tools 仍经风险登记 + ToolPolicy + Tool Ledger，handler 内再次校验 team/member/leader；无消息不暴露 Inbox、正常任务不暴露 `team_task_get`（返工/唤醒按需暴露），不要移除服务端校验。
52. **团队工具必须登记风险**：`team_*` 未在 `_TOOL_RISK_REGISTRY` 登记会被 fail-closed 拒绝；新增团队工具必须在 `team/tools/team_tools.py` 注册并加入 Leader/Member 白名单。
53. **成员用量必须同步父 Run 预算**：`MemberRuntimeManager._flush_usage` 把成员模型/工具/Token 增量累计到父 `RuntimeContext` 并刷新预算镜像；只记 `agent_team_members` 会让团队级预算失真。
54. **真实模型团队 E2E 受供应商限流影响**：连续 10+ 次调用可能 429/空响应循环；用 `AGENT_TEAM_MODEL_MIN_INTERVAL`、并发上限、成员时限与预算缓解；验收链路可用 `backend/tests/e2e/team_mock_model.py` 做确定性 Docker E2E。
55. **外部操作回执只阻止需要重新产生副作用的返工**：完成钩子只有在缺口不属于 `_BOOKKEEPING_CHECKS`（`plan_finished` / `goal_plan_present` / `goal_audit_present` / `answer_complete`）时才以 `external_operation` 停止；补记计划、补写目标核对或续写被截断正文不得因一次已成功的 `execute_command` 被强杀。
56. **执行型声明必须有执行证据**：项目工作区会话中，最终回答声称“已清空/已删除/已写入……”等实际操作但工具账本/事件没有成功命令或文件写入记录时，`action_evidence` 必须判失败并返工；决策 `requires_action=true` 时无论回答措辞都要求执行证据。禁止仅凭模型文本或历史复述判定完成。
57. **会话记忆是默认内置能力**：工作台不提供“记忆”开关，也不展示历史来源卡片；`history.sources` 事件仍保留用于审计/API，但前端不再渲染。不要重新加回开关或把该配置暴露给用户。
58. **文件清空后必须能清理容器服务**：npm/vite 等子进程可能脱离 `Service.stop` 的进程组，形成残留；`POST /api/v2/workspaces/{id}/services/stop-all` 与 Agent 工具 `stop_all_services` 会按 UID 清理所有非 Agent 进程。`Service.to_dict` 在服务工作目录缺失时返回 `unhealthy`；浏览器会话/预览授权只允许 `healthy` 服务，已停止服务必须返回 409 而不是 502。不要只依赖逐个 `stop_service`。
59. **中断续做必须带执行进度快照**：`resolve_completion_resume` 在最近 Assistant 消息缺失时必须回退到会话内最近一次失败/取消/待核对 Run；`handoff` 汇总上次计划、已执行命令、写入文件、服务状态和未决项并注入系统提示，todos 同步恢复。沙箱命令/服务类未决允许续做，外部业务副作用（邮件/Webhook 等）未决必须先人工核对。
60. **needs_reconciliation 必须收口会话**：`_pause_run` 以 `finished=True` 提交暂停说明并释放 Session 执行权；不得只改 Run 状态而不写回 Assistant 消息，否则用户看不到中断原因，下一轮“继续”也没有任何历史上下文。
61. **续做不得被歧义澄清短路**：存在 `execution_context.completion_resume` 时，“继续”不再按歧义澄清处理；决策可保留 ambiguous 标记，但服务端必须执行续做而不是只输出澄清问题。
62. **目标完成只能由 Goal Supervisor 判定**：`goal_contracts.acceptance_criteria` 的必需项全部通过、且过程检查（计划关闭/目标核对/正文完整/研究证据）无失败时才允许 `completed`；Todo 全勾、模型自述“已完成”、报告里有标题都不算证据。未达成必须 `continue` 或如实 `paused/blocked/failed`，禁止伪装成功。
63. **Goal 模式必须保留全局安全预算**：`GOAL_MAX_ITERATIONS` / `GOAL_MAX_MODEL_CALLS` / `GOAL_MAX_TOOL_CALLS` / `GOAL_MAX_SECONDS` 是目标执行的安全边界，不能改回“预算全部置 0”；轮次/停滞/预算耗尽应暂停并保留部分成果，可凭“继续”在新 Run 续做。
64. **目标契约 revision 与 steering 是两件事**：执行中补充要求成功注入消息 ≠ 目标契约已更新；`record_steering_revision` 必须在受理事务内递增 revision、把已通过项标记为待复核并新增语义验收项，消息注入只负责让模型看到要求。
65. **Goal/Research 事件不得复用 `plan.updated`**：使用 `goal_contract_created` / `goal_evaluated` / `goal_paused` 与 `research_plan_*` 独立命名空间（v2：`goal.*` / `research.*`），持久化上限不能落回默认 400 字符，否则验收条件与逐轮结果无法回放。
66. **Deep Research 的状态流转必须确定性**：搜索发现 ≠ 读取、读取 ≠ 相关、相关 ≠ 支持；只有 `research_read_source` 真实读到正文的来源才能登记证据，`quote` 必须能在正文中定位，Claim 核验由服务端 Verifier 输出 `supported/conflicting/insufficient`，模型不能自评通过。
67. **研究工具范围只能由服务端绑定**：`research_*` 工具通过 `ctx.run_id` 解析当前研究计划，模型不得传入 research_id / user_id / 知识库范围；研究工具必须在 `_TOOL_RISK_REGISTRY` 登记（写操作 WRITE、`research_read_source` NETWORK + SSRF），未登记会被 ToolPolicy fail-closed 拒绝。
68. **Goal 与 Team/Document/WorkPlan 组合的验收边界**：Goal+Team 由 `team_task_count/team_completed_count` 与最终回答生成结构化检查，未达成暂停并可在新 Run 续做（暂停判定统一走 `service._common.is_resumable_stop`，不能因 `is_team_run` 把目标暂停降级为失败）；Goal+WorkPlan 通过后由 `complete_goal_contract` 同步目标完成；Goal+Document 沿用文档图自身校验，Run 完成时同步目标状态。不要让这些执行器各自宣布 Goal 完成。
69. **新增 Goal/Research 表走迁移**：`goal_contracts` / `goal_iterations` / `research_plans|sources|evidence|claims` 已在 Alembic `e1f2a3b4c5d6` 建表；改已有表结构必须补新 migration，`init_db` 只补新表。
70. **目标暂停是“意图”不是“取消”**：`POST /api/v2/runs/{id}/pause` 只写 `agent_runs.pause_requested`（仅目标模式），由 `CompletionMiddleware.abefore_model` 在模型调用边界抛出 `GoalPauseRequested`，收敛为 `limit_exceeded + goal_paused` 并保留目标契约；不要把它实现成取消或强杀在途副作用。暂停后用户回复“继续”走原有续做链路。
71. **研究完成检查不止 Claim 状态**：`research_checks` 还要求研究问题覆盖目标契约必需项（中文二元词覆盖）、来源发布时间符合 `scope.time_range`、冲突证据在最终报告中被披露；`quote` 必须在已读正文中定位。放宽这些检查会让“搜到 URL 就宣布完成”重新出现。
72. **评测脚本只读且确定性**：`scripts/goal_research_eval.py` 从 `goal_contracts`、`goal_iterations`、`research_*`、`agent_runs.summary` 计算指标，不调用模型、不写业务数据；禁止让执行 Agent 自评完成率/引用准确率。
73. **研究工具不是外部副作用**：`research_get_state/read_source/verify_claim` 属于只读，`research_update_plan/add_evidence/add_claim` 属于平台内部状态（自然键去重）；它们必须在 `classification.py` 中可安全重试，否则 `resolve_completion_resume` 会把“继续”误判为外部副作用未决并拒绝续做。
74. **模型 429 是可恢复暂停**：`_is_model_rate_limit_error` 识别供应商限流后走 `limit_exceeded + model_rate_limited`，保留进度并允许“继续”；不要退回通用 failed，否则长目标会被一次限流直接判死。
75. **Deep Research 真实基线**：`docs/goal-research-eval.md` 由 `goal_research_eval.py` 从真实库生成（当前 16 个目标、12 个完成、False Completion=0、Recovery Success=1.0、Citation Accuracy=0.9、Source Read Rate=1.0、Conflict Detection/Disclosure=1.0）；新增评测后重跑并更新报告，禁止手写指标。
76. **python_exec 不是外部副作用**：本地受限执行（AST 白名单、无文件/网络/系统调用）在崩溃恢复时仍按 non_idempotent 保守处理，但 `has_external_side_effect()` 必须返回 False；否则 WorkPlan 代码步骤会被 `external_operation` 阻塞、Goal 被误判失败。判断“外部副作用”用 `has_external_side_effect`，不要只看 side_effect_class。
77. **Goal + WorkPlan 失败必须暂停而非判死**：目标模式下计划未通过验收时标记目标 `paused` 并以 `limit_exceeded + goal_work_plan` 收口（保留可续做状态），不能把单轮计划失败当成整个目标失败；只有非目标 Run 才按原失败语义收口。

## 修改范围原则

- 历史文档（`docs/` 下任务书、交付说明）是存档，不要改写其历史内容。
- 不要引入未要求的依赖、抽象或配置项；保持最小变更。
- 提交前自查：测试通过、无调试残留（console.log / print / 临时脚本）、镜像已重建并冒烟。
