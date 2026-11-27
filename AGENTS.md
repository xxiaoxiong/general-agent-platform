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
cd backend && python -m pytest -q                 # 全量回归（~1101 passed）
cd backend && python -m pytest tests/test_xxx.py -q   # 单文件
cd backend && python -m pytest tests/test_knowledge_rag.py -q  # Knowledge RAG（真实解析/Embedding/Qdrant）

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

## 修改范围原则

- 历史文档（`docs/` 下任务书、交付说明）是存档，不要改写其历史内容。
- 不要引入未要求的依赖、抽象或配置项；保持最小变更。
- 提交前自查：测试通过、无调试残留（console.log / print / 临时脚本）、镜像已重建并冒烟。
