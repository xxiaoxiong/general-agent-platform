# AGENTS.md — AI 编码代理协作指南

本文件面向在本仓库（General Agent Platform）中工作的 AI 编码代理，描述架构约定、常用命令与已知陷阱。动手改代码前请先通读「已知陷阱」一节。

## 项目概述

可私有化部署的企业级通用 Agent 平台：多模型对话、Agent 智能体（LangGraph 执行内核 + Durable Execution）、企业知识库（RAG）、可视化流程编排、提示词工程、应用市场，以及 Langfuse 全链路可观测与 Agent 自进化闭环。

- 代码注释、提交信息、文档统一使用**中文**（标识符用英文）。
- 后端 Python 3.11 + FastAPI + SQLAlchemy(async)，前端 Vue 3 + TypeScript + Element Plus。

## 目录结构

```
backend/
  app/api/            # 路由：v1（传统模块）+ v2（agent runtime / workspace / evolution 等）
  app/core/           # config.py（全量配置，启动期校验）、database.py、security、redis
  app/models/         # SQLAlchemy 模型（agent.py / evolution.py 等）
  app/schemas/        # Pydantic 模式
  app/services/       # 业务逻辑
    agent/            # Agent 执行引擎包（engine / tool_executor / context_manager / ...）
    evolution/        # 自进化闭环（metrics / judge / evolution_advisor / dev_briefing）
    inference/        # 模型连接池、请求队列
    tools/            # 工具注册表与执行器
  app/observability/  # langfuse_tracing.py、otel_tracing.py
  alembic/            # 数据库迁移
  tests/              # pytest 测试套件
frontend/
  src/api/            # API 封装（含 v2）
  src/views/          # 页面组件
  src/features/       # 业务功能组件（如 run/ApprovalPanel）
docker/               # docker-compose.yml + Dockerfile + entrypoint + grafana
docs/                 # 需求设计、任务书、阶段交付说明（历史文档，勿改写）
```

## 常用命令

```bash
# 后端测试（必须在 backend/ 目录下执行，否则 ModuleNotFoundError: app）
cd backend && python -m pytest -q                 # 全量回归（当前 ~480 passed）
cd backend && python -m pytest tests/test_xxx.py -q   # 单文件

# 前端（vue-tsc 类型检查 + 构建）
cd frontend && npm run build
cd frontend && npm run dev

# Docker（compose 项目名 general-agent-platform）
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f backend

# 数据库迁移
cd backend && python -m alembic upgrade head
```

## 架构约定

- **异步优先**：API 与 service 层使用 `AsyncSession`（`app/core/database.py`）；阻塞调用（Langfuse flush 等）用 `asyncio.to_thread` 包裹。
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

## 修改范围原则

- 历史文档（`docs/` 下任务书、交付说明）是存档，不要改写其历史内容。
- 不要引入未要求的依赖、抽象或配置项；保持最小变更。
- 提交前自查：测试通过、无调试残留（console.log / print / 临时脚本）、镜像已重建并冒烟。
