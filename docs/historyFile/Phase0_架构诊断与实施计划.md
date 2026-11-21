# Phase 0 架构诊断与实施计划

> AI-GeneralPlat V3 产品级重构 · 阶段 0 交付物
> 分支 `refactor/agent-product-v3` · 基线 `06405286ff61cb231c0eadf8045ae4ba56bed775`
> 生成日期 2026-11-13

---

## 1. 当前代码状态

| 项 | 值 |
| --- | --- |
| 审查基线 HEAD | `0640528`（与任务书 `06405286ff61...` 一致，工作区干净） |
| 执行分支 | `refactor/agent-product-v3`（自 `main` 创建，未直接改 `main`） |
| 后端测试基线（默认环境） | **335 passed, 6 skipped**（`python -m pytest tests/ -q`，31~38s） |
| 后端测试基线（MySQL 环境） | 338 passed, 3 skipped（`TEST_MYSQL_MIGRATE_SYNC_DSN` 指向 MySQL 8.0.46@3308） |
| 跳过项构成 | 3 × 真实模型 E2E（缺 `AGENT_E2E_BASE_URL/API_KEY/MODEL`）+ 3 × MySQL 迁移（默认 DSN 3307 不可达） |
| 依赖版本（实测可运行组合） | deepagents 0.7.34 / langgraph 1.2.20 / langchain 1.4.9 / langchain-core 1.7.6 / langchain-openai 1.7.4 / openai 3.46.0 |
| 数据库迁移 | head = `e8f9a0b1c2d3`（`app_instances.workflow_id` 补列），MySQL 8 真实 upgrade/downgrade 往返通过 |
| 运行环境 | MySQL 8.0.46（3308）、PostgreSQL Checkpointer（5433）、Redis（6379）本地容器可用 |

**测试基线说明（统一口径）**：历史报告中的 301/303 passed 均非本 HEAD 的验证结果，作废。
以本文件记录的命令与数字为准；每次提交在 message 中记录当次全量数字。

## 2. Phase 0 已完成修复（每批一提交）

| 批次 | Commit | 内容 | 验证 |
| --- | --- | --- | --- |
| P0-03 | `c8ae156` | A22 Agent 列表/计数按 owner+visibility 同口径过滤；A23 全局异常响应脱敏（只回 `request_id`，堆栈进服务端日志） | 新增 TestAgentListIsolation / TestExceptionMasking |
| P0-04 | `578fb02` | compose 凭据全部 `${VAR:?}` 必填注入、内部端口只绑 127.0.0.1；生产启动拒绝默认/弱 `SECRET_KEY`/`ADMIN_PASSWORD`；新增 `docker/.env.example` | test_deploy_security.py 6 用例 |
| P0-05 | `cc5d3e8` | DeepAgents 原生工具面收窄（无 Sandbox 不含 `execute`，主代理/子代理同口径）；`FilesystemPermission` deny 规则（凭据类文件）；递归预算换算修正 | test_deepagents_capabilities Section C 6 用例（factory 生产装配） |
| P0-06 | `210cc41` | 模型能力真实 Tool Calling/Streaming 探测 + `POST /models/configs/{id}/test`（未通过 422） | test_model_capability_probe.py 9 用例 |
| P0-07 | `f81b234` | `e8f9a0b1c2d3` MySQL 8 升级/回滚往返测试 | test_migration_mysql.py 3 passed（真实 MySQL） |

**未做（有意延后）**：管理员首次登录强制改密的交互流程（需登录链路+前端配合）→ Phase 3；
当前以"生产启动拒绝默认凭据"保证默认口令在生产不可能生效。

## 3. 依赖地图与真实调用链（P0-02）

### 3.1 生产执行链（新内核，保留）

```text
POST /api/v1/agents/{id}/chat|invoke          api/v1/agents.py:302/607
  → AgentRuntimeService.start_run / run_to_completion   agent_runtime/service.py
    → RunWorker（Lease/Heartbeat/Fencing）               agent_runtime/worker.py
      → AgentRuntimeService._drive                       agent_runtime/service.py:440+
        → factory.build_agent                            agent_runtime/factory.py
          → create_deep_agent（DeepAgents Harness）
            → LangGraph（Checkpoint/Interrupt/Resume）
              → ToolAdapterFactory（业务工具 + ToolPolicy） tools/adapter.py
              → FilesystemMiddleware（原生文件工具 + FilesystemPermission）
        → EventBus（持久化 agent_run_events + LegacyEventAdapter SSE）
        → SessionWriteback（session_result_outbox）
```

### 3.2 旧引擎引用地图（Phase 5 删除对象）

| 旧模块 | 现存引用点（全仓扫描） | 切换/删除前置 |
| --- | --- | --- |
| `services/agent/engine.py` 等 7 文件 | `services/agent/__init__.py` 导出 `AgentEngine`；`api/v1/agents.py:19` 导入；`workflow_service.py:582-589`（`AGENT_RUNTIME_ENABLED=false` 分支） | Workflow Agent 节点固定走 Runtime；删除开关分支 |
| `services/agent/memory_manager.py` | `api/v1/agents.py:77-127` 记忆 API（list/add/delete/clear） | Memory 迁移到 LangGraph Store 后换 API（Phase 2 P2-04） |
| `services/agent/checkpoint.py` | `main.py:47-48` 启动清理 `CheckpointManager.cleanup_old_checkpoints` | 随旧引擎删除清理调用 |
| `services/agent_service.py` | 兼容层（re-export `AgentEngine`），调用方待扫描切换 | Phase 5 |
| `services/inference/model_manager.py` | `main.py:66-67` 关闭钩子；旧监控 API | ModelClientFactory 接管后删除（Phase 2 P2-03） |
| `services/inference/queue.py` | 旧监控 `/monitor/inference` 读取 | Phase 5 重写监控后删除 |
| `agent_runtime/telemetry.py` | `service.py` 发 TelemetryRecord（LoggingExporter） | Langfuse/OTel 接入后收敛（Phase 1） |
| `LegacyEventAdapter` | `agent_runtime/events.py:158-246`；`service.py:440-465` 输出旧 SSE 契约 | 前端切 v2 事件协议后删除（Phase 3/5） |
| `AGENT_RUNTIME_ENABLED` 开关 | `core/config.py:94`、`main.py:28/52`、`workflow_service.py:568` | Phase 5 移除永久双引擎开关 |

### 3.3 双轨地图（Phase 2/5 收敛对象）

- **Memory 双轨**：旧 SQLite `MemoryManager`（`memory_v2.db`，API 路径在用） ↔ LangGraph Store（`AGENT_STORE_DSN` 已留配置未接记忆路由）。
- **Model 双轨**：旧 `inference/model_manager.py`+`queue.py` ↔ `model_adapter.build_chat_model`（生产路径）。
- **Trace 双轨**：旧 `services/agent/trace.py`（Inference 统计） ↔ `agent_runtime/telemetry.py`（LoggingExporter）↔ 待建 Langfuse。
- **Checkpoint 双轨**：旧 `services/agent/checkpoint.py`（文件清理） ↔ LangGraph Checkpointer（PostgreSQL/SQLite/memory）。

### 3.4 前端依赖（Phase 3 切换面）

- `frontend/src/api/index.ts:101-120`：`/agents/{id}/chat`（SSE）、记忆管理等 v1 路径。
- 聊天组件 `AgentChat.vue`（约 1143 行）消费 `LegacyEventAdapter` 的 thought/action/observation/final 旧契约；`stopGeneration` 仅 `AbortController.abort()`（A13 未接后端 Cancel）。
- 监控页读 `/monitor/performance`、`/monitor/inference`（旧 Trace/InferenceQueue 数据，A11）。

### 3.5 迁移链

`9ce3d801b702`（初始）→ … → `a1b2c3d4e5f6`（Phase 2 前）→ `d7e8f9a0b1c2` → `e8f9a0b1c2d3`（head，
`app_instances.workflow_id`）。fresh / pre-phase2 / head→downgrade→head 三条路径已在 MySQL 8.0.46 验证。

## 4. 运行错误分类（Phase 0 基线）

| 类别 | 现象 | 处置 |
| --- | --- | --- |
| 配置漂移 | 迁移缺 `app_instances.workflow_id` → MySQL 1054 | 已修复并有往返测试（A24） |
| 递归预算 | 中间件钩子每步计递归，`max_steps*3+10` 会让长任务误报 GraphRecursionError | 已修正为每步 12 + 余量（`service.py:1041`） |
| 权限 | 列表接口越权可见（A22）、异常信息泄露（A23） | 已修复 |
| 部署 | 默认密码/Secret 硬编码（A23 相邻风险） | 已修复 |
| 工具面 | `execute` 与实际 Sandbox 不匹配（A05） | 已修复（工具面移除，external 才暴露） |

## 5. Phase 1~5 文件级任务拆解

### Phase 1 可观测性（Langfuse + OTel + Metrics）

| 任务 | 文件 | 说明 |
| --- | --- | --- |
| P1-01 | `agent_runtime/telemetry.py`（重写）、`service.py`、`factory.py` | Langfuse Client + LangChain CallbackHandler + `propagate_attributes`（trace_name/user/session/tags）；callbacks 合并不覆盖 `ModelUsageCallback` |
| P1-02 | 新 `observability/otel.py`、`main.py` | OTel instrumentation：FastAPI/SQLAlchemy/HTTPX/Redis/Worker；W3C TraceContext |
| P1-03 | `worker.py`、`service.py`、`repositories/*` | 平台 Span：queue_wait/claim/lease/graph/model/tool/approval/retry/recovery/checkpoint/writeback |
| P1-04 | `observability/` | 脱敏、异步批量导出、有界缓冲、采样；Trace 失败不影响执行 |
| P1-05 | `docker/`、`docs/` | Prometheus 指标注册表 + Grafana 8 类面板（Run/模型/工具/Worker/恢复/Token/DB/SSE） |

### Phase 2 Agent Domain / Model / Capability / Memory

| 任务 | 文件 | 说明 |
| --- | --- | --- |
| P2-01 | 新 `assistant/domain/`（AgentDefinition/AgentVersion）、`models/agent.py`、alembic | 版本化不可变配置；Run 绑定 `agent_version_id` |
| P2-02 | 新 `runtime/capability/`（Capability Registry） | 能力包 → Tools/Skills/SubAgents 映射 + 授权校验；`build_toolset` 统一原生+业务工具授权（收敛 A04） |
| P2-03 | 新 `runtime/models/`（ModelRouter、ModelClientFactory） | 能力矩阵（tool_calling/streaming/…/verification_status）、真实探测接发布门禁、客户端复用；删除 `inference/model_manager.py`+`queue.py` 引用 |
| P2-04 | 新 `runtime/memory/`、`agent_runtime/backends.py`、alembic | LangGraph Store + MemoryMiddleware（`AGENTS.md`）；`memory_v2.db` 导出→只读导入→对账→切换→观察期（不直接删库） |
| P2-05 | `agent_runtime/skills.py` → `runtime/skills/` | Skills Registry：来源/版本/启停/权限/审计，渐进加载 |
| P2-06 | `agent_runtime/subagents.py` → `runtime/subagents/` | SubAgent Profile 注册（researcher/data-analyst 迁移），工具白名单+模型策略+预算 |

### Phase 3 产品工作台

| 任务 | 文件 | 说明 |
| --- | --- | --- |
| P3-01 | 新 `api/v2/workspace.py`、seed、`frontend/features/workspace/` | 默认通用助手开箱即用 |
| P3-02 | 新 `api/v2/assistants.py`（draft）、前端 Builder | 自然语言生成助手草稿→确认→创建 |
| P3-03 | 新 `api/v2/*`（workspace/assistants/conversations/runs/capabilities/artifacts/admin） | §7 契约；v1 保留兼容适配 |
| P3-04 | 前端 `features/conversation/`（ChatComposer/MessageList/MessageItem） | 拆分 AgentChat.vue；登录即聊 |
| P3-05 | 前端 `features/run/`（RunTimeline/ToolActivity/ApprovalPanel/ArtifactPanel） | v2 事件协议（tool.started/completed…）；不展示原始 CoT |
| P3-06 | 前端 + `api/v2/runs`（cancel/resume/events） | X-Run-Id 保存、后端 Cancel、Last-Event-ID 续传、切换 Session 隔离（A13/A14/A15） |
| P3-07 | `workflow_service.py` | Workflow Agent 节点统一走 Runtime，删除旧引擎分支 |

### Phase 4 Harness 质量

| 任务 | 文件 | 说明 |
| --- | --- | --- |
| P4-01/02 | 新 `runtime/execution/verification.py` | Task Contract + 按任务类型验证器（§9 表），验证失败产生结构化事件 |
| P4-03 | `runtime/execution/retry.py`、`recovery.py`（自 service.py 拆出） | 模型/工具错误分类处置（§6.4 表）；非幂等禁止盲目重放 |
| P4-04 | `factory.py`、`agent_runtime/` | Context Engineering：大输出卸载、Summarization 调优 |
| P4-05 | `runtime/models/`、`service.py` 拆分 | ModelClientFactory、并发、工具超时、事件持久化优化 |
| P4-06 | `tests/`（fault injection/load） | Worker kill、超时、限流、Checkpoint 损坏、长稳 |

### Phase 5 清理与发布

| 任务 | 说明 |
| --- | --- |
| P5-01 | 删除 §3.2 表中旧引擎 8 文件 + `agent_service.py`，先全仓引用扫描 |
| P5-02 | 移除 `AGENT_RUNTIME_ENABLED` 开关与 `LegacyEventAdapter`；重写 `/monitor/*` |
| P5-03 | 重写 README/架构/API/部署/安全/可观测/运维文档 |
| P5-04 | 全量回归：后端/前端/Playwright/MySQL/PostgreSQL/真实模型/安全/并发/长稳/故障恢复 |

## 6. 兼容性风险与回滚

- 所有改动为增量修复，未动 Durable Execution 不变式（Lease/Fencing/Ledger/Checkpoint/HITL/Outbox 全保留）。
- `execute` 从默认工具面移除会影响依赖 shell 的既有 Prompt 场景——由 `AGENT_SANDBOX_MODE=external` 或开发逃生开关恢复，属安全收紧而非功能回退。
- 生产启动校验拒绝默认凭据：现有部署若沿用 `Admin@123456`/change-me 密钥将无法启动（有意为之），需按 `docker/.env.example` 注入真实值。
- 回滚：单批独立提交，按 commit 粒度 `git revert`；数据库迁移 `e8f9a0b1c2d3` 已验证 downgrade。

## 7. 验收对照（任务书 §13 中 Phase 0 可达项）

| 验收项 | 状态 |
| --- | --- |
| A22 用户隔离 / A23 异常泄露 | ✅ 已修复 + 回归测试 |
| A24 迁移验证 | ✅ MySQL 8 真实 upgrade/downgrade |
| A05 execute 与 Sandbox 不匹配 | ✅ 工具面移除 + 生产装配测试 |
| A04 原生工具权限统一 | ◐ FilesystemPermission + interrupt_on 已接；能力包级统一授权在 Phase 2 P2-02 |
| A06 模型能力验证 | ◐ 真实探测 API/逻辑就绪；真实模型联调需 `AGENT_E2E_*`/供应商环境 |
| A20 测试基线统一 | ✅ 335/6（本文件为准） |
| 其余 A01~A21 产品/架构项 | → Phase 1~5 按 §5 拆解执行 |

## 8. 下一步

进入 **Phase 1 可观测性**（P1-01 Langfuse 接入 → P1-02 OTel → P1-03 平台 Span →
P1-04 脱敏/导出 → P1-05 指标/面板）。验收口径：一次真实 Run 在 Langfuse 中可见
平台 Run/Graph/Model/Tool/SubAgent/Token/延迟/错误全链路。
