# Work Orchestrator 改造交付说明

任务书：`docs/General-Agent-Platform-Work-Orchestrator-改造任务书.md`
交付日期：2026-11-27
状态：Phase 0–5 已实现并通过回归；默认 `shadow`，`auto` 需部署配置显式开启。

## 1. 已完成内容

### Phase 0：基线审查与执行器解耦

- 新增 [改造前现状审查](architecture/work-orchestrator-current-state.md)：调用链、四类执行路线、Checkpoint、异常处理、模块复用表、影响面与风险。
- 现有 DeepAgents / Document Graph / Team Graph / Team Project Graph 未改写执行逻辑，只增加步骤级 Checkpoint 注入点。

### Phase 1：WorkPlan / Planner / Validator / Registry

- `schemas.py`：严格 Pydantic 契约（`extra="forbid"`、步骤 1~6、ID 唯一、文本/数组/总体积上限）。
- `complexity.py`：确定性复合任务判定；简单问答不调用 Planner。
- `planner.py`：低温非流式 JSON 规划，20s 超时、Token 上限、一次格式修复；失败安全回退。
- `validator.py`：执行器白名单、依赖只能引用前序步骤、input_refs 候选校验、能力授权、URL/工作区路径限制、循环依赖检测。
- `registry.py`：9 类 `executor_profile` 映射真实引擎；团队/数据库/研究/业务能力按当前授权收窄。
- 配置：`AGENT_WORK_ORCHESTRATOR_MODE=off|shadow|auto` 等 6 项，启动期校验。

### Phase 2：单步骤与跨引擎 Adapter

- `adapters/deep_agent.py`：复用 `build_agent`、`_consume_stream`、ToolPolicy、Ledger、Checkpoint、预算与 HITL。
- `adapters/document.py`：复用 Document Graph 的生成/编辑/校验/版本；前序步骤正文作为 `conversation_reference` 资料。
- `adapters/team.py`：复用 `TeamRun` 的 Team Graph 与 Team Project Graph，支持步骤级线程。
- 统一 `WorkStepOutcome`（summary / final_text / artifact_refs / evidence_refs / verification / retryable / checkpoint_ref）。

### Phase 3：Durable Work Graph

- `graph.py`：`select_step → execute_step → verify_step → select_step/final_verify → deliver`。
- 串行调度（`AGENT_WORK_MAX_PARALLEL_STEPS=1`）；依赖未完成不执行。
- 失败只重试失败步骤（最多 `AGENT_WORK_MAX_REPLANS`），依赖失败的后续步骤标记 `skipped`；已完成步骤不重复。
- `work:{run_id}` Checkpoint 保存计划状态/步骤结果/重试次数/恢复位置；步骤执行器使用 `work:{run_id}:step:{step_id}`。
- 计划快照写 `input_payload.execution_context.work_plan`，带 Lease/Fencing；计划写入成功即幂等复用，不重新规划。
- 统一最终验收：必需步骤、交付物、未决外部副作用、验收证据；最终文本优先最后完成的交付步骤。

### Phase 4：事件与前端

- 新增 `work.*` 独立事件命名空间（不复用 `plan.updated`）；存储上限 12,000 字符、64 项数组，避免默认 400 字符截断。
- `frontend/src/features/run/workPlan.ts`：计划视图、状态推进、合并与收口（实时与历史回放共用）。
- `WorkPlanCard.vue`：目标、模式、步骤、依赖、当前步骤、执行器、产物数量、验收结果、降级原因。
- `WorkspaceHome.vue` 渲染；`finalizeRunMessage` 在失败/取消时收口活跃步骤。

### Phase 5：测试、指标与灰度

- 新增 9 项 Prometheus 指标。
- 新增测试：
  - `backend/tests/test_work_plan_schema.py`
  - `backend/tests/test_work_planner.py`
  - `backend/tests/test_work_orchestrator_graph.py`
  - `backend/tests/test_work_orchestrator_integration.py`
  - `frontend/tests/work-plan.test.mjs`
- 回归：`backend` 全量 **1124 passed, 13 skipped**；前端 **67 tests passed**；`npm run build` 通过。

## 2. 关键调用链

```
api/run/core.py::create_run
  └─ AgentRuntimeService.start_run_with_session → RunWorker.claim_run
        └─ worker._supervise → service._drive
              ├─ resolve_run_decision（Decision Architecture）
              ├─ RuntimeContext / ToolPolicy / TaskContract / Checkpointer
              ├─ work_orchestrator.integration.prepare_plan
              │     ├─ complexity.is_composite
              │     ├─ planner.plan_work（仅复合任务）
              │     ├─ validator.validate_plan
              │     ├─ persistence.persist_work_plan_record（Lease/Fencing）
              │     └─ work.plan.created 事件
              ├─ work_orchestrator.integration.drive_plan
              │     └─ graph.WorkOrchestrator.drive
              │           ├─ DeepAgentAdapter / DocumentAdapter / TeamAdapter / ProjectAdapter
              │           └─ verification.final_verify
              └─ service._finish_work_plan
                    └─ 现有 _commit / _write_back_session / run_completed / _fail
```

## 3. 数据库迁移

- `backend/alembic/versions/f9a1b2c3d4e5_add_tool_work_step_id.py`
- 新增 `agent_tool_executions.work_step_id`（String(40), nullable）+ 索引；
- 旧 Run 工具账本读取不受影响；downgrade 先删索引再删列（SQLite 兼容）。

## 4. 文件清单

新增：

```
backend/app/services/work_orchestrator/
  __init__.py schemas.py complexity.py planner.py validator.py registry.py
  persistence.py verification.py graph.py integration.py
  adapters/__init__.py base.py deep_agent.py document.py team.py
backend/alembic/versions/f9a1b2c3d4e5_add_tool_work_step_id.py
backend/tests/test_work_plan_schema.py
backend/tests/test_work_planner.py
backend/tests/test_work_orchestrator_graph.py
backend/tests/test_work_orchestrator_integration.py
frontend/src/features/run/workPlan.ts
frontend/src/features/run/WorkPlanCard.vue
frontend/tests/work-plan.test.mjs
docs/architecture/work-orchestrator-current-state.md
docs/work-orchestrator-deployment.md
```

修改：

```
backend/app/core/config.py                       # AGENT_WORK_* 配置 + 启动校验
backend/app/services/agent_runtime/context.py    # work_step_id
backend/app/services/agent_runtime/events.py     # work 事件 + 存储策略 + Legacy 适配
backend/app/services/agent_runtime/event_protocol.py
backend/app/services/agent_runtime/service/stream.py
backend/app/services/agent_runtime/completion.py # 步骤级证据过滤
backend/app/services/agent_runtime/tools/adapter.py
backend/app/services/agent_runtime/repositories/tool_execution_repository.py
backend/app/services/agent_runtime/team_runtime.py
backend/app/services/agent_runtime/team_project_runtime.py
backend/app/services/agent_runtime/document_graph.py
backend/app/models/agent.py
backend/app/observability/metrics.py
backend/tests/conftest.py                        # 测试默认 off
frontend/src/features/run/blocks.ts
frontend/src/views/workspace/WorkspaceHome.vue
docker/docker-compose.yml / docker/.env.example
AGENTS.md
```

## 5. 验证记录

| 验证项 | 结果 |
| --- | --- |
| 后端全量回归 | 1125 passed, 13 skipped（最后一次完整全绿运行；随后新增的外部副作用不重放用例已在 `test_work_orchestrator_graph.py` 单文件通过） |
| 前端单测 | 67 passed（含 3 条 WorkPlan 归约用例） |
| 前端类型检查/构建 | `npm run build` 通过 |
| Alembic upgrade/downgrade | `test_alembic_migration_upgrade_downgrade` 通过 |
| 计划 Schema 安全 | 非法执行器、重复 ID、循环/前序依赖、未授权能力、URL/工作区路径均拒绝 |
| 简单任务零额外调用 | `test_simple_task_does_not_call_planner` |
| shadow 不改路线 | `test_shadow_mode_persists_and_publishes_plan_without_execution` |
| auto 计划执行/依赖顺序 | `test_graph_runs_dependencies_in_order_and_delivers_artifact` |
| 失败跳过依赖 | `test_graph_skips_dependents_after_permanent_failure` |
| 有限修复 | `test_graph_bounded_repair_retries_only_failed_step` |
| 审批暂停/恢复 | `test_graph_pauses_for_approval_and_resumes_same_step` |
| 崩溃恢复不重做已完成步骤 | `test_graph_recovers_from_crash_without_repeating_completed_steps` |
| 外部副作用未知不自动重放 | `test_external_side_effect_unknown_needs_reconciliation_and_is_not_retried` |
| 交付物最终验收 | `test_final_verification_requires_declared_file_artifact` |
| 计划幂等复用 | `test_persisted_auto_plan_is_reused_without_replanning` |
| 事件映射/不截断 | `test_work_events_map_to_v2_and_are_not_truncated_to_400_chars` |
| **真实 Docker 复合任务** | Run `680f72c4277f49e7b0c9ca90d77b0072`：`research → document_docx` 两步计划；联网检索 7 次（7 个来源）；生成《企业 Agent 平台技术趋势》DOCX **44,156 字节**；最终验收 3/3 通过；耗时 521.6s / 17 次模型调用 / 14,203 tokens；下载接口实测返回 44,156 字节 |

真实 Docker 验证细节：

- 决策：`intent=document_generate`，约束包含「先联网调研、正文约 800 字、标题固定」。
- 计划：`research`（资料检索与核验）→ `document`（生成 Word 趋势报告），依赖关系正确。
- 研究步骤：`web_search` / `fetch_webpage` 各多次，`work.step.verification` 显示 3/3 通过（answer_present、citations_present 7 个来源、web_evidence 7 次检索）。
- 文档步骤：Document Graph 生成 1 个 DOCX（document_id=22, version=1），`document_validation` 与 `artifact_published` 通过。
- 收尾：`work.plan.completed` + `run.completed`，`summary.verification.status=passed`，产物带正式下载地址。
- 失败路径实测：首次 20s 超时产生 `work.plan.degraded`（reason=规划模型超时）并安全回退原 Decision 路线，未将 Run 判失败；据此把默认超时调至 30s、输出 Token 上限 1200。

未执行的真实环境验证（如实标注）：

- 未做百万级 Chunk 或超大文件并发压力测试；
- 未覆盖跨 Runner 双节点调度（当前 RunWorker 单节点/多 Worker 共享数据库模型不变）；
- 未对真实企业语料做大规模计划质量评测（shadow 采样可持续积累）。

## 6. 已知限制

1. 初期跨执行器严格串行；`AGENT_WORK_MAX_PARALLEL_STEPS` 仅允许 1。
2. Planner 仅处理复合任务，单步骤计划会自动回退原路线；不做拖拽式计划编辑。
3. 步骤间只传递声明的 `input_refs` 摘要/正文（单步 12k、总 30k），大文件使用工作区/Artifact 引用。
4. `work_step_id` 只用于平台工具 Ledger；DeepAgents 内部文件工具通过事件中的 `work_step_id` 取证。
5. WorkPlan 预算停止（`budget`）不提供「一键续做」；需要重新发起任务，已完成产物仍保留在工作区/文档中心。
6. 编排状态依赖 Checkpointer。`AGENT_CHECKPOINT_DSN=memory` 时进程重启无法恢复（与平台既有 Durable Execution 限制一致）。
7. WorkPlan 卡片只读展示，不提供步骤重排/手工跳过交互。
8. 执行中补充要求经现有 SteeringMiddleware 注入当前/后续步骤，但初期不会自动重建 WorkPlan DAG；若补充要求会改变执行引擎或新增交付物，建议重新发起任务，避免运行中静默改变计划。`revision` 字段已预留，后续可在此基础上实现显式重规划。
9. 未新增独立的 WorkPlan 数据库表；计划快照存 `input_payload`，运行状态存 Checkpoint，事件存 `agent_run_events`。

## 7. 回滚

```env
AGENT_WORK_ORCHESTRATOR_MODE=off
```

- 重建/重启 backend 后立即回到改造前行为；
- 旧 Run 无 `work_plan` 继续走原恢复路径；
- 如需回滚数据库：`cd backend && python -m alembic downgrade -1`（仅删除 nullable 列与索引）。
