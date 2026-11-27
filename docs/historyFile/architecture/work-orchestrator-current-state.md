# Work Orchestrator 改造前现状审查（Phase 0）

审查基线：当前仓库 `main`（2026-11-27）。本文是《General Agent Platform — Work Orchestrator 工作任务编排层改造任务书》阶段 0 的交付物，记录改造前的真实调用链、可复用模块、风险点与实施边界。后续代码改动以本文为审查依据；若与代码不一致，以代码为准并更新本文。

## 1. 当前 Run 调用链

### 1.1 创建与领取

```
POST /api/v2/runs (api/run/core.py::create_run)
  ├─ 会话/助手/模型/审批模式/联网开关校验
  ├─ 附件引用解析（resolve_attachment_refs_async）
  ├─ 会话文档、最近 Run、预览文档摘要
  ├─ route_task() 确定性预处理
  ├─ should_defer_semantic() ? decision_pending : prepare_execution()
  └─ AgentRuntimeService.start_run_with_session()
        ├─ AgentRun(pending) + 用户消息同事务落库
        └─ RunWorker.claim_run() 原子领取（lease_version fencing token）

RunWorker (worker.py)
  ├─ _recovery_check()：fresh / continue / waiting_approval / needs_reconciliation
  ├─ attach RunHandle + LegacyEventAdapter
  └─ _supervise()
        ├─ 心跳续租（HEARTBEAT_OK / LOST / PAUSED）
        ├─ 取消意图检测
        ├─ service._drive(handle, adapter, messages, resume, agent_config, execution_context, ...)
        └─ 终态提交后释放租约与 Registry 热数据
```

### 1.2 `_drive()` 内部分支

`backend/app/services/agent_runtime/service/stream.py::_drive` 当前按顺序承担：

1. 会话 → 项目 → Sandbox 推导（`SANDBOX_ENABLED`）。
2. 发布 `run_started`。
3. MCP 工具面装配与风险登记。
4. 模型配置装载。
5. Decision Architecture：`resolve_run_decision()` → `task_decision` 事件 → 澄清提前结束。
6. `RuntimeContext` 构建（预算 / HITL / 网络 / Sandbox / goal_mode）。
7. Run 级预算历史累计恢复（`_load_budget`）。
8. Task Contract（非文档/非团队 Run）。
9. 工具面收窄（数据库绑定、知识库绑定）→ `ToolPolicy`。
10. Checkpointer / Store / 工具 db 会话。
11. 按 `task_type` / `team_mode` 选择执行对象：
    - `is_team_run` → `TeamRun`（`team_runtime.py`），内部再分 `team_graph` 或 `team_project_graph`；
    - `is_document_run` → `build_document_graph()` + `drive_document_graph()`；
    - 否则 → `build_agent()` + `_consume_stream()`（DeepAgents）。
12. 运行时限、内层模型超时重试、预算 flush。
13. 统一终态：`_read_completion_review()` / `document_context.summary` / `team_stats` → `_commit` → `_write_back_session` → `run_completed`。
14. 异常分支：`PersistenceError` / `CancelledError` / 预算超限 / `ModelCapabilityError` / `ToolPolicyError` / `RunLeaseLost` / 其他。
15. `finally`：遥测、Run 指标、Sandbox 写租约释放。

### 1.3 四类执行路线的真实入口

| 路线 | 入口 | 输入 | Checkpoint Thread | 输出去向 | 异常处理 |
| --- | --- | --- | --- | --- | --- |
| DeepAgents | `factory.build_agent()` + `service._consume_stream()` | `payload={messages: [...]}` / `Command(resume)` / `None` | `agent-{agent_id}-session-{session_id}` | `MODEL_DELTA` / `TOOL_*` / `PLAN_UPDATED` / `VERIFICATION_*` | 统一冒泡到 `_drive` 异常分支 |
| Document Graph | `documents.graphs.build_document_graph()` + `agent_runtime.document_graph.drive_document_graph()` | `{messages, execution_context}` | 同上 | `document.*` / `artifact.created` / `input.required` | `DocumentError` 映射；`pause_for_input` 暂停 |
| Team Graph | `TeamRun.drive()` → `build_team_graph()` + `drive_team_graph()` | `{objective, reference_context, ...}` 或 checkpoint 复用 | `team:{run_id}` | `team.updated` / `final_text` | `TeamExecutionError` / `TeamResponseTruncated` |
| Team Project Graph | `TeamRun.drive()`（`project_scope`）→ `drive_project()` | 同上 + `project_scope` / `project_base_run_id` | `team:{run_id}` | `team.updated` / `project.progress` / `project.delivery` | `ProjectWorkspaceError` / 截断错误 |

### 1.4 事件与回放

- 内部事件：`agent_runtime/events.py::RuntimeEvent`，`EventBus(persist=True)` 写 `agent_run_events`，序号由 `agent_runs.last_event_seq` 原子分配。
- v2 事件：`event_protocol.py::to_v2_event`，SSE 事实来源为数据库；`replay_events_v2` 支持 `Last-Event-ID` 续传。
- 持久化截断：`_persist_max_len()` 对普通事件 400 字符，长文本/工具证据 100,000 字符，`task_decision` 6000，`team.updated` 4200。
- 前端：`frontend/src/features/run/blocks.ts::applyRunEvent` 是实时与历史回放共用的唯一归约器；`WorkspaceHome.vue` 按块类型渲染。

## 2. 可复用模块与职责边界

| 模块 | 现有职责 | Work Orchestrator 中的定位 | 是否改写 |
| --- | --- | --- | --- |
| `decision/*` | 意图、连续性、资源引用、目标/约束/交付/验收 | 编排输入；不生成 DAG | 否 |
| `task_router.py` | 确定性文档识别与路由预处理 | 兼容保留；不再扩展第二套 Router | 否 |
| `assistant/task_contract.py` | 任务类型、交付格式、完成条件 | 步骤级验收的可选输入 | 否 |
| `assistant/verification.py` | 工具账本 + 文本的确定性验证 | 步骤/最终验收复用 | 否 |
| `factory.build_agent()` | DeepAgents 图装配（工具/子代理/Skills/Memory/HITL/预算/Completion） | `deep_agent` 系列执行器 | 否（新增 step 上下文参数） |
| `document_graph.py` + `documents/graphs.py` | Word 生成/编辑/预览/审阅/版本 | `document_docx` 执行器 | 否（新增可注入 checkpoint thread） |
| `team_runtime.py` / `team_graph.py` | 团队分工、并行、证据复核 | `team` 执行器 | 否（新增可注入 thread） |
| `team_project_runtime.py` / `team_project_graph.py` | 软件项目构建与验收 | `software_project` 执行器 | 否（新增可注入 thread） |
| `worker.py` | Claim / Lease / Heartbeat / Fencing / 恢复 | 唯一 Run 生命周期与执行权 | 否 |
| `persistence.py` | Checkpointer / Store | 编排状态持久化 | 否 |
| `events.py` / `event_protocol.py` | 事件持久化、序号、回放 | `work.*` 事件通道 | 扩展映射与存储策略 |
| `ToolPolicy` / `AgentApproval` / `AgentToolExecution` | 授权、审批、幂等、账本 | 所有步骤继续遵守 | 扩展 `work_step_id`（nullable） |
| `service/stream.py::_drive` | 决策→上下文→路线分发→收尾 | 唯一集成点；新增编排分支 | 最小侵入式扩展 |

## 3. 当前主要断层（与任务书 §2.2 核对结果）

1. `RunDecision` 已有 `research / data_analysis / database_query / coding / business_action`，但 `apply_decision()` 统一落到 `task_type=general_chat`（DeepAgents），没有跨执行引擎步骤。
2. `expected_route()` 只把文档映射到 `document_graph`，软件项目映射到 `team_project_graph`，其余回到 `deep_agent`。
3. `apply_decision()` 保存 `decision_goal / decision_constraints / decision_acceptance`，未生成步骤 DAG。
4. `TeamRun` 的任务板只在团队 Run 内生效，不是跨 Research / Document / Data 的统一编排。
5. `_drive()` 同时承担决策、上下文、路线分发、流消费、验证、收尾；新增路线容易继续堆积条件分支。
6. Task Contract / Verification 主要覆盖普通 Agent；文档与团队各有一套验证；缺少统一 `WorkStepOutcome` / `WorkVerificationResult`。
7. `WorkflowEngine` 是用户预置固定流程，与 RunWorker 的 Durable Run 生命周期不同，本次不作为自动 Work Step 执行器。

## 4. 改造影响面

### 4.1 新增

```
backend/app/services/work_orchestrator/
├── __init__.py
├── schemas.py          # WorkPlan / WorkStep / 运行时状态 / Outcome / Verification
├── complexity.py       # 复合任务判定（确定性，零模型调用）
├── planner.py          # WorkPlanner（LLM JSON、超时、一次修复）
├── validator.py        # Schema/依赖/引用/能力/尺寸校验
├── registry.py         # executor_profile → 适配器 + 允许条件
├── persistence.py      # 计划快照（Lease/Fencing）+ 事件发布
├── verification.py     # 最终验收
├── graph.py            # 编排调度（DAG + 恢复 + 有限修复）
└── adapters/
    ├── base.py         # 统一接口 + 步骤证据采集
    ├── deep_agent.py   # DeepAgents（general/research/data/db/coding/business）
    ├── document.py     # Document Graph
    ├── team.py         # Team Graph
    └── project.py      # Team Project Graph
```

### 4.2 修改

| 文件 | 变更 |
| --- | --- |
| `core/config.py` | 新增 `AGENT_WORK_*` 配置与启动校验 |
| `services/agent_runtime/events.py` | `work_*` 事件常量、存储上限、Legacy 适配 |
| `services/agent_runtime/event_protocol.py` | `work.plan.*` / `work.step.*` v2 映射 |
| `services/agent_runtime/service/stream.py` | `_drive` 集成编排分支；直接路径保持原样 |
| `services/agent_runtime/context.py` | `RuntimeContext.work_step_id` |
| `services/agent_runtime/tools/adapter.py` | 工具事件与账本带 `work_step_id` |
| `services/agent_runtime/completion.py` | `_collect_evidence` 按 `work_step_id` 过滤 |
| `models/agent.py` | `AgentToolExecution.work_step_id`（nullable） |
| `alembic/versions/*` | 新增 nullable 列迁移 |
| `services/agent_runtime/team_runtime.py` | 允许注入步骤级 checkpoint thread |
| `services/agent_runtime/document_graph.py` | 允许注入步骤级 config |
| `observability/metrics.py` | 9 项编排指标 |
| `frontend/src/features/run/blocks.ts` | WorkPlan 归约 |
| `frontend/src/features/run/WorkPlanCard.vue` | 计划卡片 |
| `frontend/src/views/workspace/WorkspaceHome.vue` | 渲染计划卡片 |
| `docker/docker-compose.yml` / `docker/.env.example` | 透传 `AGENT_WORK_*` |

### 4.3 不改

- DeepAgents Agent Loop、Team Graph、Team Project Graph、Workflow Engine。
- Run 状态机、RunWorker、Lease/Heartbeat/Fencing、EventBus 持久化机制。
- ToolPolicy / Approval / Tool Ledger 的既有语义。
- 既有 `/api/v2/runs` 契约与前端事件归约。
- `plan.updated`（DeepAgents Todo）语义。

## 5. 关键风险与对策

| 风险 | 对策 |
| --- | --- |
| 简单任务多一次 Planner 模型调用 | `complexity.py` 确定性判定；不满足复合条件直接走原路径 |
| 计划生成后 Worker 崩溃，恢复重复规划/重复执行 | 计划先持久化到 `input_payload.execution_context.work_plan`；编排状态存 `work:{run_id}` Checkpoint；步骤执行器使用 `work:{run_id}:step:{step_id}` |
| 跨步骤预算被重置 | 全部步骤共享同一 `RuntimeContext` / `ExecutionBudget`，不新建 Run / Worker / Lease |
| HITL 恢复重复副作用 | 步骤适配器复用原 Checkpointer；恢复时 `Command(resume)`；`ToolPolicy` / Ledger 的 `needs_reconciliation` 不自动重放 |
| 计划越权 | Validator 只接受服务端 `executor_profile` 白名单、已授权能力、真实上下文引用；不允许模型指定工具名/类名/URL/路径 |
| 复杂任务回退破坏简单任务 | `off` 模式完全走旧路径；Planner 失败/非法计划发 `work.plan.degraded` 并回退原 Decision 路线 |
| 事件过大/泄露 | `work.*` 事件独立存储上限；统一 `redact_sensitive`；只存计划摘要与产物引用 |
| 前端旧卡片被覆盖 | `work.*` 使用独立命名空间；`plan.updated` 保持原语义 |

## 6. 实施顺序

1. 阶段 0（本文）：现状审查，不改执行行为。
2. 阶段 1：Schema / Registry / Complexity / Planner / Validator / 配置 / shadow。
3. 阶段 2：DeepAgents / Document / Team / Project Adapter + 单步骤接入。
4. 阶段 3：多步骤 DAG、Checkpoint、恢复、有限修复、统一最终验收。
5. 阶段 4：`work.*` 事件 + `WorkPlanCard` + 回放。
6. 阶段 5：指标、shadow 采样、回归与灰度。

## 7. 验收对照

| 任务书验收项 | 本仓库落地方式 |
| --- | --- |
| 简单任务无明显额外延迟/模型调用 | `complexity.is_composite()` 不满足 → 不调用 Planner |
| 复合任务形成可解释计划 | `WorkPlan` + `work.plan.created` |
| 每个步骤映射真实执行器 | `registry.resolve()` + `validator` 白名单 |
| 跨引擎按依赖顺序执行 | `graph.py` DAG 调度（初期串行） |
| 中间产物可靠传递 | `input_refs` + 步骤结果摘要 + 文件/Artifact 引用 |
| 恢复不重复副作用 | Checkpoint thread + Lease/Fencing + Ledger |
| 可追溯验收证据 | `WorkStepOutcome.evidence_refs` / `WorkVerificationResult.checks` |
| 历史回放一致 | 持久化 `work.*` 事件 + 同一前端归约器 |
| 旧能力回归 | 全量 pytest + 前端 build |
| off 恢复旧行为 | `AGENT_WORK_ORCHESTRATOR_MODE=off` 直接跳过编排层 |
