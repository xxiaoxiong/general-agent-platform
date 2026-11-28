# Agent Team Runtime 交付说明与测试报告

**交付范围：** 任务书阶段一（Member Runtime 重构）+ 阶段二（Team Collaboration 重构）。
阶段三/四仅保留扩展接口，未实现无限制自治。

## 1. 交付内容

| # | 任务书交付物 | 实现 |
|---|---|---|
| 1 | 可运行的 Member Runtime | `team/runtime/member_manager.py`：创建/启动/恢复/中断/取消/消息唤醒，成员状态机与预算持久化 |
| 2 | Leader/Member 统一 DeepAgents | `member_factory.py`：同一 `create_deep_agent` 路径；Leader 规划/复核/综合均为 Agent Loop，不是单次模型调用 |
| 3 | 独立 Member Checkpoint 与恢复 | 稳定 Thread `team:{run}:member:{member_id}`；Leader 阶段线程 `:review`/`:synthesis`；中断/重启从自身 Checkpoint 恢复 |
| 4 | Task Board / CAS / 认领 / 租约 | `task_board.py` + `task_repository.py`：revision CAS、owner+lease token、过期回收、依赖 DAG、有限重试 |
| 5 | 持久化 Message Bus / Inbox / Ack | `message_bus.py`：先落库再唤醒、幂等键、delivered/acked、可恢复 `wait_for_message` |
| 6 | Artifact Store | `artifact_store.py`：内容/预览/大小/SHA256，按 team 作用域授权，引用式传递 |
| 7 | 数据库模型与迁移 | `models/team.py` 6 张表；`c8d9e0f1a2b3_add_agent_team_runtime.py` |
| 8 | Team Tools 注册与权限 | `team_tools.py`：Leader 8 个 / Member 7 个工具；风险登记 + ToolPolicy + Tool Ledger；服务端强制角色/团队作用域 |
| 9 | 事件与观测 | `team.updated`（兼容前端）+ `team.member.updated`（身份/状态/任务/预算/恢复次数）；团队快照 API |
| 10 | 自动化测试 | 单元/集成/并发/恢复/回归；见第 3 节 |
| 11 | 架构说明 | `docs/team-runtime-architecture.md`；审查基线 `docs/architecture/team-runtime-current-state.md` |
| 12 | 部署与迁移说明 | `docs/team-runtime-deployment.md` |
| 13 | 真实测试报告 | 本文第 3 节（含未达成项与原因） |
| 14 | 变更文件与设计取舍 | 本文第 4/5 节 |

## 2. 关键设计取舍

1. **Team Run 与 AgentRun 1:1**：`team_run_id = agent_runs.id`，恢复无需映射表；
   Member/Task ID 在团队内唯一（不同 Run 可复用 `research` 等 ID）。
2. **数据库 vs Checkpoint 职责分离**：Task/Member/Message/Artifact 是协作事实；
   LangGraph Checkpoint 只存 Agent 执行状态；Controller 以 Task Board 为准。
3. **Leader 阶段线程隔离**：规划用基础 Thread，复核/综合用 `:review`/`:synthesis`；
   规划被限流中断且任务已创建时轮换 `:r{random}` 代际，避免综合误续跑规划输出。
4. **成员工具面收窄**：只读业务工具 ∩ Profile；默认不暴露易空转的通用工具；
   无消息不暴露 Inbox；正常任务不暴露 `team_task_get`（信息已注入），返工/唤醒时按需暴露。
5. **DeepAgents 安全基线**：显式 `general-purpose` 空工具占位；`FilesystemMiddleware`
   只保留虚拟 `read_file`（无宿主访问）；不传 `interrupt_on`，审批类工具不进入成员工具面。
6. **软件项目团队不迁移**：`team_project_graph` 保留原工程能力；新 Runtime 作为可复用接口，
   避免高风险重写。
7. **预算三层**：成员 Profile、团队上限、Provider/模型并发与最小调用间隔；
   成员用量同步累计到父 Run 预算镜像。

## 3. 测试报告

### 3.1 后端回归

```bash
cd backend
python -m pytest -q -x
# 1161 passed, 13 skipped in 121.17s
```

> 注意：全量测试必须单进程前台运行。并发/后台第二个 pytest 进程会与 SQLite 争抢，
> 稳定复现同一批 anyio 跨事件循环错误（项目已有已知陷阱），并非代码失败。

新增/修改测试：

| 文件 | 覆盖 |
|---|---|
| `tests/test_team_runtime.py`（17） | DAG 校验、CAS、并发认领唯一性、租约回收、完成幂等、重试上限、跨团队隔离、消息幂等/Inbox/Ack、Artifact 片段/隔离、Member 独立 Thread/预算/恢复/消息唤醒、Leader 阶段线程、Team Graph 接线、真实 DeepAgents 多步工具循环、Team Tools 权限 |
| `tests/test_api_v2_runs.py::TestTeamSnapshotAPI`（1） | 团队快照 API 所有者校验与任务板事实 |
| `tests/test_team_graph.py`（13，回归） | 旧版 Team Graph 规划/并行/复核/返工/恢复/取消保持通过 |

### 3.2 前端

```bash
cd frontend
npm run build     # vue-tsc + vite build 通过
npm test          # 69 passed / 0 failed
```

前端变更：`TeamRunCard.vue` 展示执行成员 ID 与持久化状态；`teamProgress.ts` 扩展可选字段。

### 3.3 迁移

- 临时 SQLite：`upgrade head → downgrade b6c7d8e9f0a1 → upgrade head` 全部成功。
- Docker MySQL：入口脚本执行 `b6c7d8e9f0a1 → c8d9e0f1a2b3` 成功，6 张表创建。
- `alembic heads`：单一 head `c8d9e0f1a2b3`。

### 3.4 Docker 确定性 E2E（已完成）

**目的：** 在真实 Docker（MySQL + PostgreSQL Checkpointer + RunWorker + SSE）中验证
Leader 规划、Task Board 依赖调度、两个 Member 独立执行、Artifact、综合交付与事件链路。
模型端点使用仓库内 `backend/tests/e2e/team_mock_model.py`（OpenAI 兼容、结果可重复），
不代表 LLM 质量，只验证链路。

运行：`8cba8771439640d1b0bf6fb071c97ccb`（会话 235，最终镜像）

| 验证项 | 结果 |
|---|---|
| Run 状态 | `completed`；`final_text` 313 字符（数据库与 `run_completed` 事件一致） |
| 团队状态 | `agent_team_runs.status=completed, phase=completed` |
| 任务依赖 | `research` 先完成（16:49:44），`summary` 依赖其后完成（16:49:46），均 `completed` |
| 成员 | 3 个（Leader + 2 Member），各自独立 Thread；Leader 使用 `:synthesis` 阶段线程 |
| Artifact | 每条任务均有 `result_artifact_id`，记录大小与 SHA256 |
| 事件阶段 | `planning → working → working → working → synthesizing → completed` |
| 快照 API | `GET /api/v2/runs/{id}/team` 返回 2 任务 / 3 成员；非所有者 404 |
| Leader 用量 | 3 次模型调用 / 2 次工具（规划建 2 任务 + 综合） |
| Member 用量 | 各 1 次模型调用，0 次工具，输出保存为 Artifact |

### 3.5 真实模型 E2E（部分达成，如实记录）

使用平台现有 `kimi-k3.1-pro` 多次运行：

- 已观察到：Leader 通过 `team_task_create` 创建 2~4 个任务；成员在独立 Thread 执行并生成
  Artifact；有运行达到 2/3 任务完成（`34116797ecb04fae83eecc815a7ba34e`）。
- 未达成：完整 3 任务 + 综合交付的稳定成功。阻塞点：
  1. 供应商 429（`OpenAIRateLimitError`）在连续 10+ 次模型调用后出现；
  2. 部分请求长时间无响应，成员在 300s 时限内出现空响应模型循环
     （回调计数远高于预算，最终由成员时限中止）。
- 已采取的产品级缓解：阶段预算、成员预算、`AGENT_TEAM_MODEL_MIN_INTERVAL` 调用间隔、
  并发上限、成员时限、失败状态持久化、`team.member.updated` 错误摘要。
- 复现命令：创建 `team_mode=true` 的 Run，指定现有真实模型配置；观察
  `agent_team_members.last_error` 与 `agent_team_events`。

**结论：** 内部链路已由确定性 Docker E2E 与真实 DeepAgents 多步工具单元测试验证；
真实外部模型在本次环境的稳定性不足以作为验收依据，需在稳定供应商配额下重跑。

### 3.6 未执行项与原因

| 项目 | 状态 | 原因 |
|---|---|---|
| 真实 Docker 中途重启团队 Run | 未执行 | 需要可控的长耗时真实模型调用窗口；当前供应商限流/空响应使中断点不可控。已由成员 Checkpoint 恢复单测（PersistenceError → interrupted → 同 Thread resume）与共享 PostgreSQL Checkpointer 机制覆盖，建议在稳定模型配额下补跑 |
| 真实模型完整团队 E2E | 部分执行 | 见 3.5；已观察到规划建任务、成员独立执行与 Artifact，但供应商 429/空响应导致未能稳定完成全部任务与综合 |
| 软件项目团队真实 Docker E2E | 未重跑 | 本轮未改动 `team_project_graph`/`team_project_runtime`；全量回归中的项目团队测试全部通过 |
| 前端浏览器手工验收 | 未执行 | 前端仅增加成员 ID/持久化状态展示；`npm run build` 与 69 项单测通过，未启动 Playwright |

## 4. 变更文件清单

**新增（后端）**

- `app/models/team.py`
- `alembic/versions/c8d9e0f1a2b3_add_agent_team_runtime.py`
- `app/services/agent_runtime/team/**`（errors/states/repositories/services/runtime/tools）
- `app/api/run/team.py`
- `tests/test_team_runtime.py`
- `tests/e2e/team_mock_model.py`
- `docs/architecture/team-runtime-current-state.md`
- `docs/team-runtime-architecture.md`
- `docs/team-runtime-deployment.md`
- `docs/team-runtime-delivery.md`

**修改（后端）**

- `app/core/config.py`：`AGENT_TEAM_*` 配置与启动校验
- `app/models/__init__.py`：注册 Team 模型
- `app/services/agent_runtime/context.py`：`RuntimeContext.team_context`
- `app/services/agent_runtime/events.py`：`team.member.updated`、持久化上限
- `app/services/agent_runtime/team_graph.py`：Task Board/Leader/Member 接线、阶段预算、阶段线程、失败依赖跳过
- `app/services/agent_runtime/team_runtime.py`：TeamCollaboration 装配、团队终态、关闭任务板
- `app/services/tools/__init__.py`：`team_*` 工具路由
- `app/api/run/__init__.py`：挂载团队快照路由
- `tests/test_api_v2_runs.py`：团队快照 API 测试

**前端**

- `src/features/run/teamProgress.ts`
- `src/features/run/TeamRunCard.vue`

**部署**

- `docker/docker-compose.yml`
- `docker/.env.example`
- `docker/.env`（本地部署配置，gitignored）
- `AGENTS.md`（目录、命令、架构约定、陷阱）

## 5. 已知限制

1. 真实外部模型在本次环境存在限流/空响应循环，完整真实模型团队 E2E 未稳定通过；
   确定性端点与真实 DeepAgents 单成员多步工具测试已通过。
2. 成员工具面按运行场景动态收窄（无消息不暴露 Inbox、正常任务不暴露 `team_task_get`）；
   这是为防止模型空转的运行时策略，不是权限绕过（handler 仍保留且服务端校验）。
3. Provider 容量限制为进程内信号量；多 Worker 部署各进程独立限流，后续可替换为集中容量服务。
4. 软件项目团队仍走原工程图，尚未迁移到统一 Scheduler（任务书允许保留）。
5. 前端仅做最小展示增强（成员身份/持久化状态），未新增完整团队管理 UI。
6. 团队表只随 Alembic 迁移创建；生产 `init_db()` 不会自动补新表。

## 6. 验收清单对照（任务书 §13）

- [x] Leader 和 Member 使用统一 DeepAgents 构建体系
- [x] Member 是完整多步骤 Agent（真实 DeepAgents 工具循环测试通过）
- [x] 每个成员有独立、稳定、可恢复的 Checkpoint Thread
- [x] 成员可按角色使用不同模型、Prompt、工具和预算（Profile + 角色覆盖）
- [x] LangGraph 管理团队控制流程，不替代成员 Agent Loop
- [x] Task Board 持久化，CAS 和任务认领具备并发正确性（并发认领唯一性测试）
- [x] 成员可直接通信，消息持久化、可确认、可恢复（单元/集成测试）
- [x] 成员成果可通过 Artifact 引用共享
- [~] 服务重启后 Team/Member/Task/Message 与执行状态可恢复：DB 事实持久化与成员 Checkpoint 恢复单测通过；真实 Docker 中途重启 E2E 未单独执行（见 3.6）
- [x] 权限、预算、并发和租约机制有效
- [x] 普通 Agent 能力没有回归（全量回归 1161 passed）
- [x] 软件项目团队生成、测试、验收、修复和交付能力没有回归（原工程图未改动）
- [x] 自动化测试和真实运行报告完整（真实模型限制已如实列出）
- [x] 文档与实际代码一致，无空壳实现
