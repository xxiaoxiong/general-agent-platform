# Agent Team Runtime 架构（阶段一 Member Runtime + 阶段二 Team Collaboration）

**目标：** DeepAgents 负责 Leader/Member 的 Agent 执行，LangGraph 负责团队控制流，
数据库负责持久化协作事实。普通 Agent、文档图、软件项目团队保持原路径可用。

## 1. 模块边界

```
backend/app/services/agent_runtime/
  team/
    errors.py                     # 领域异常
    states.py                     # 成员/任务状态机、消息状态
    repositories/
      team_repository.py          # agent_team_runs
      member_repository.py        # agent_team_members（状态 CAS）
      task_repository.py          # agent_team_tasks / dependencies（CAS + 租约）
      message_repository.py       # agent_team_messages（幂等 + inbox/ack）
      artifact_repository.py      # agent_team_artifacts
    services/
      task_board.py               # DAG 校验、CAS 更新、认领/租约/完成/失败/重试/快照
      message_bus.py              # 持久化消息、Inbox、Ack、可恢复等待
      artifact_store.py           # 引用式成果、片段读取、大小/校验和
    runtime/
      member_profile.py           # MemberProfile（角色/Prompt/模型/工具/预算）
      member_context.py           # MemberExecutionContext / MemberResult
      member_factory.py           # MemberAgentFactory（统一 DeepAgents 构建）
      member_manager.py           # MemberRuntimeManager（创建/启动/恢复/取消/预算/事件）
      member_directory.py         # 成员目录（Team Tools 解析接收者/状态）
      leader_agent.py             # LeaderRunner（规划/复核/综合 Agent Loop）
      model_routes.py             # 团队默认模型 + 角色级模型覆盖（快照不含密钥）
      capacity.py                 # Provider/模型并发与最小调用间隔
      collaboration.py            # TeamCollaboration 组装（Team/Board/Message/Artifact/Runtime）
      team_service.py             # agent_team_runs 阶段/状态
    tools/
      team_tools.py               # Leader/Member Team Tools + 风险登记
  team_graph.py                   # Team Controller（LangGraph；兼容旧 invoke/worker 路径）
  team_runtime.py                 # TeamRun：Run 租约/预算/事件 + TeamCollaboration 装配
```

模型：`backend/app/models/team.py`；迁移：`backend/alembic/versions/c8d9e0f1a2b3_add_agent_team_runtime.py`。

## 2. 调用链

```
POST /api/v2/runs (team_mode=true)
  -> AgentRuntimeService._drive
     -> TeamRun.drive
        -> create_team_collaboration
             TeamService.ensure            # team_run_id = run_id
             MemberRuntimeManager + LeaderRunner.ensure
        -> build_team_graph(TeamGraphContext(collaboration=...))
        -> drive_team_graph
           plan       : LeaderRunner.invoke("planning") -> Team Tools 创建任务
           schedule   : 回收租约、跳过失败依赖、唤醒空闲成员、同步任务板状态
           assign     : Send 并行分发（受 max_parallel 限制）
           work_member: MemberRuntimeManager.execute -> 独立 Member Agent Loop
           review     : LeaderRunner.invoke("review")（deep_review）
           revise     : MemberRuntimeManager.execute（返工）
           combine    : LeaderRunner.invoke("synthesis") -> final_text
        -> TeamCollaboration.finish（团队终态）
```

软件项目团队（`project_scope`）仍走 `team_project_runtime.drive_project` 与原
`team_project_graph`，不经过新的 Scheduler；本轮只保证 Member Runtime/Team Tools
接口可复用，不做高风险整体迁移。

## 3. 成员与任务状态

**成员：** `created → ready → running → waiting | completed | failed | interrupted | cancelled`；
`interrupted/failed/cancelled → ready` 仅允许显式恢复/重试；`completed` 为终态。
成员记录包含角色、Profile 快照、模型快照（无密钥）、预算、独立 Thread、用量、恢复次数。

**任务：** `pending → ready → claimed → running → completed | failed | blocked | cancelled | skipped`；
`claimed/running` 通过租约保护，`failed/skipped/cancelled → ready` 仅显式重试（有限次数）。

**CAS：** 所有更新使用 `UPDATE ... WHERE task_id=? AND revision=?`，`rowcount` 判定；
认领/完成/失败同时校验 `owner_member_id` + `lease_token`，旧租约无法覆盖新成员结果。
重复完成同一 artifact 幂等返回 `already`，不同 artifact 返回冲突。

## 4. 独立 Checkpoint 与恢复

- 成员 Thread：`team:{team_run_id}:member:{member_id}`，稳定、唯一、可恢复。
- Leader 阶段 Thread：规划使用基础 Thread；复核/综合使用 `...:review` / `...:synthesis`，
  避免规划阶段未完成 Checkpoint 被综合阶段误续跑（综合事实来自 Task Board/Artifact）。
- 规划阶段因预算/限流中断且任务已写入时，`LeaderRunner.rotate_thread()` 切换新代际
  （`...:r{random}`），成员身份不变。
- 恢复决策：
  1. 任务已 `completed` → 直接复用 Artifact，不重复调用 Agent；
  2. 成员 `running`（进程重启）→ 标记 `interrupted`，从自身 Thread 恢复；
  3. 任务租约过期 → `reclaim_expired` 回到 `ready`，旧租约完成被拒；
  4. Checkpoint 已写但任务未完成 → 成员完成时先 Artifact、再任务完成、最后成员状态，
     失败窗口由幂等完成/重试收敛。

## 5. Task Board / Message Bus / Artifact

- **Task Board**：DAG 校验（角色唯一、ID 唯一、自依赖/重复/缺失/循环拒绝）；
  `ready_tasks` 只返回依赖全部完成的任务；失败/取消/跳过依赖自动跳过后继。
- **Message Bus**：先持久化再唤醒；`pending → delivered → acked`；`idempotency_key`
  去重；`wait_for_message` 轮询数据库（可恢复，不依赖进程内存）；跨团队统一拒绝。
- **Artifact Store**：内容 + 预览 + 大小 + SHA256；默认单条上限
  `AGENT_TEAM_ARTIFACT_MAX_BYTES`；消息/任务只保存 `artifact_id`；读取按 `team_run_id`
  作用域，跨团队按不存在处理。

## 6. 工具、权限与预算

- Leader/Member 均为 `create_deep_agent` 构建；成员工具面 =
  父 Agent 已授权的只读业务工具 ∩ Profile 白名单 + 受控 Team Tools。
- 审批类、写入类、网络禁用类工具不进入成员工具面；所有执行仍走
  `ToolPolicy.check_tool_allowed/check_exec_allowed` 与 Tool Ledger。
- `general-purpose` 必须显式占位（空工具），防止 deepagents 注入全工具默认子代理。
- 文件栈仅保留 `FilesystemMiddleware(tools=["read_file"])`（虚拟 StateBackend，
  无宿主/工作区访问；成员读资料走 `team_artifact_get`）。
- 成员默认不暴露 `datetime_now/calculator/weather/http_get` 等易被反复调用的通用工具；
  角色显式 `tools` 白名单可放开。无待处理消息时不暴露 Inbox 工具；正常任务信息已注入，
  不暴露 `team_task_get`（返工/消息唤醒时按需暴露）。
- 预算三层：成员 Profile（模型/工具次数/时限）、团队（最大成员数/并行度/任务数）、
  Provider/模型（并发上限 + 最小调用间隔）。成员用量同步累计到父 Run 预算镜像。
- 事件：`team.updated`（前端兼容快照 + 成员列表）、`team.member.updated`
  （身份/角色/状态/当前任务/预算/错误摘要/恢复次数），均带 `team_run_id/member_id/task_id/thread_id`。

## 7. 快照与前端

- `GET /api/v2/runs/{run_id}/team`：仅 Run 所有者可读，返回团队、成员、任务板快照。
- 前端 `TeamRunCard` 继续消费 `team.updated`；新增展示执行成员 ID 与持久化任务状态。
- 断线重连以服务端快照/事件回放为准，不依赖浏览器内存。

## 8. 阶段三/四预留

- 动态调度：Task Board 已支持认领/租约/依赖/阻塞；Scheduler 可替换为动态认领循环。
- 成员递归派生：Profile 已保留 `allow_spawn`（本轮强制 false）与父成员字段。
- 独立 Critic/团队评测：`review` 阶段与 Artifact/Task 事实可直接接入独立评审器。
- Provider 容量：`ModelCapacityLimiter` 可替换为 Redis/集中容量服务实现。
