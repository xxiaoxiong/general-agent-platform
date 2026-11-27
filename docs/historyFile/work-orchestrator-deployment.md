# Work Orchestrator 部署与运维手册

面向 General Agent Platform 的「工作任务编排层」部署、配置与排障。设计详情见 [改造前现状审查](architecture/work-orchestrator-current-state.md)。

## 1. 架构与数据流

```
POST /api/v2/runs
  → RunWorker 领取（Lease / Fencing）
  → Decision Architecture 解析目标 / 约束 / 交付
  → Work Orchestrator（复合任务）
        ├─ WorkPlanner（一次低温结构化规划，20s 超时）
        ├─ WorkPlanValidator（DAG / 执行器 / 引用 / 能力 / 尺寸）
        ├─ Work Orchestrator Graph（串行调度、有限修复、最终验收）
        │     ├─ DeepAgents Adapter（general/research/data/db/coding/business）
        │     ├─ Document Graph Adapter（Word 交付）
        │     ├─ Team Graph Adapter（显式团队）
        │     └─ Team Project Adapter（软件项目）
        └─ 复用现有 Run / EventBus / ToolPolicy / Approval / Ledger / Checkpoint
  → 现有 Run 收尾（completed / failed / limit_exceeded / paused）
```

关键约束：

- 一个用户任务只有一个 `AgentRun`、一个 `RunWorker`、一套 Lease/EventBus；
- 编排状态写 `work:{run_id}` Checkpoint；步骤执行器使用 `work:{run_id}:step:{step_id}`；
- 步骤级工具证据写入 `agent_tool_executions.work_step_id`（nullable）；
- 计划快照写 `agent_runs.input_payload.execution_context.work_plan`（带 Lease/Fencing）；
- `work.plan.*` / `work.step.*` 事件写现有 `agent_run_events`，可与历史会话一起回放。

## 2. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_WORK_ORCHESTRATOR_MODE` | `shadow` | `off` / `shadow` / `auto` |
| `AGENT_WORK_PLAN_TIMEOUT` | `30` | Planner 单次模型调用超时（秒；慢模型可调高） |
| `AGENT_WORK_PLAN_MAX_TOKENS` | `1200` | Planner 输出 Token 上限 |
| `AGENT_WORK_MAX_STEPS` | `6` | 单个计划最大步骤数（1~6） |
| `AGENT_WORK_MAX_REPLANS` | `1` | 单次 Run 内自动修复/重规划上限（0~3） |
| `AGENT_WORK_MAX_PARALLEL_STEPS` | `1` | 初期固定 1（跨执行器串行） |

启动期校验（`app/core/config.py::validate_startup`）会拒绝非法值，直接拒绝启动。

### 2.1 模式说明

- `off`：完全不进入编排层，行为与改造前一致；旧 Run 恢复路径不变。若某个 Run 已生成并持久化 `auto` 计划，运行中切到 `off` 仍会继续完成该计划（避免半途切换造成重复/遗漏），新 Run 一律走旧路径。
- `shadow`（默认）：只对复合任务生成并校验候选计划，发布 `work.plan.created`，不改变执行路线；用于采集真实计划质量。
- `auto`：复合任务按计划执行；Planner 失败/计划非法/单步骤计划自动回退原 Decision 路线，并发布 `work.plan.degraded`。

### 2.2 灰度建议

1. 先保持 `shadow` 观察 `work_plan_generation_latency_seconds`、`work_plan_invalid_total`、`work_plan_fallback_total`；
2. 选择少量工作区/用户验证「调研 → Word」「数据库 → 报告」；
3. 切换 `auto` 后观察 `work_step_completion_total`、`work_plan_final_verification_total`；
4. 出现异常立即回 `off`（无需重启的执行进程会在下一次 Run 生效；Docker 需更新 `.env` 后重建/重启 backend）。

## 3. 部署

```bash
# 1) 数据库迁移（新增 nullable 列 + 索引，向后兼容）
cd backend && python -m alembic upgrade head

# 2) Docker 重建（代码烘焙进镜像）
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml up -d backend frontend

# 3) 健康检查
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs --tail=100 backend
```

`docker/.env` 示例：

```env
AGENT_WORK_ORCHESTRATOR_MODE=auto
AGENT_WORK_PLAN_TIMEOUT=20
AGENT_WORK_MAX_STEPS=6
AGENT_WORK_MAX_REPLANS=1
```

> 未设置时使用 `docker-compose.yml` 中的默认值（`shadow`）。

## 4. 可观测性

新增 Prometheus 指标（`app/observability/metrics.py`）：

| 指标 | 类型 | 用途 |
| --- | --- | --- |
| `work_plan_generation_latency_seconds` | Histogram | Planner 延迟（mode/outcome） |
| `work_plan_invalid_total` | Counter | 计划非法/校验失败 |
| `work_plan_fallback_total` | Counter | 安全回退原因 |
| `work_plan_step_count` | Histogram | 计划步骤数 |
| `work_step_completion_total` | Counter | 步骤完成/失败（执行器维度） |
| `work_plan_final_verification_total` | Counter | 最终验收结果 |
| `work_plan_recovery_total` | Counter | 计划复用/步骤恢复 |
| `work_plan_duplicate_execution_total` | Counter | 已完成步骤被重复选择 |
| `work_plan_model_call_overhead_total` | Counter | 编排层额外模型调用 |

事件（v2）：

```
work.plan.created / work.plan.updated / work.plan.completed / work.plan.degraded
work.step.started / work.step.completed / work.step.failed / work.step.verification
```

前端 `WorkPlanCard` 实时展示计划、依赖、当前步骤、执行器、产物数量与验收摘要；历史回放使用同一事件归约器。

## 5. 排障

| 现象 | 排查 |
| --- | --- |
| 计划卡片一直停在「影子计划」 | 当前为 `shadow`；改 `auto` 才会实际执行 |
| 复合任务没有计划卡片 | 是否被判定为非复合；查看 `work.plan.degraded` 的 reason |
| `work_step_failed` 且 retryable | 已自动修复 1 次仍失败；查看步骤 summary 与 Tool Ledger |
| 审批后计划未继续 | 确认 Run 状态从 `waiting_approval` 被 Claim；步骤线程 `work:{run_id}:step:{step_id}` Checkpoint 是否存在 |
| 文档步骤没有产物 | 文档图返回的 `document_validation` / `artifact_published` 检查；检查文档中心与 `artifact.created` 事件 |
| 计划状态与 UI 不一致 | 事件日志是 UI 事实来源；检查 `agent_run_events` 是否被清理 |
| 预算暂停后续跑 | 当前 WorkPlan 预算停止不提供自动续做；重新发起任务并保留原会话资料 |

## 6. 回滚

1. 配置回滚：`AGENT_WORK_ORCHESTRATOR_MODE=off`，重建/重启 backend；
2. 代码回滚：回退到本改造前的提交；
3. 数据库回滚（可选）：`python -m alembic downgrade -1` 仅删除 `agent_tool_executions.work_step_id` 与索引，不影响历史 Run；
4. 旧 Run 兼容：无 `work_plan` 的 Run 继续走原恢复路径；`work.*` 事件对旧客户端仅表现为额外 thought 行。
