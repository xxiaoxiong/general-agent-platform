# Agent Team Runtime 当前状态审查（阶段 0）

**审查时间：** 2026-11-27  
**审查范围：** `backend/app/services/agent_runtime`、团队相关模型/迁移、Checkpointer、事件总线、前端团队进度消费。

## 1. 真实调用链（重构前）

```
POST /api/v2/runs (team_mode=true)
  -> AgentRuntimeService._drive
     -> is_team_run = execution_context.team_mode is True
     -> TeamRun(...)                        # 绕过 build_agent()
     -> TeamRun.drive -> build_team_graph -> drive_team_graph
        plan:    TeamRun.invoke()           # 一次 ChatModel.ainvoke 生成任务 JSON
        schedule/work_member:
                 TeamRun.worker()           # 最多 2 次工具调用 + 1 次总结
        review/combine:
                 TeamRun.invoke()           # 一次 ChatModel.ainvoke
```

## 2. Leader / Member 执行方式（重构前）

| 角色 | 实现 | 问题 |
|---|---|---|
| Leader | `TeamRun.invoke()` 单次模型调用 | 不是完整 Agent Loop；无法通过受控工具管理任务板 |
| Member | `TeamRun.worker()`：绑定只读工具后最多 2 次调用 + 1 次总结 | 不是 DeepAgents Agent；无独立 Thread、无独立 Checkpoint、无成员级预算 |
| 任务事实 | LangGraph State（`team:{run_id}`） | 服务重启只能依赖整张图 Checkpoint；没有持久化 Task Board / Message / Artifact |

## 3. 可复用模块

- `TeamRun`：Run 租约/预算/Fencing/事件发布、项目团队分流（`drive_project`）。
- `team_graph.py`：规划、DAG 校验、并行分发、证据复核、返工、综合、Checkpoint 恢复。
- `team_project_graph.py` / `team_project_runtime.py`：软件项目专用规划、文件分工、源码生成、测试、浏览器验收、修复闭环（按任务书保留，不强行迁移）。
- `factory.build_agent`：DeepAgents 组装范式（Middleware、SubAgent 占位、Checkpointer、HITL、Skills）。
- `ToolAdapterFactory` + `ToolPolicy` + Tool Ledger：统一工具授权、副作用登记、幂等/重放治理。
- `EventBus` / `AgentRunEvent`：事件持久化、SSE 回放、脱敏。
- `CheckpointerFactory`：memory/sqlite/postgres 统一 Checkpointer，不降级。

## 4. 依赖与 API 实测

- deepagents 0.7.34、langgraph 1.2.20、langchain 1.4.9、langgraph-checkpoint 4.3.1。
- `create_deep_agent` 支持 `middleware / subagents / checkpointer / store / interrupt_on`。
- **必须显式传入 `general-purpose` 占位**，否则 deepagents 自动注入全工具默认子代理，绕过平台白名单。
- `FilesystemMiddleware` 属于受保护脚手架，且 `tools` 列表必须包含 `read_file`；不能通过空列表移除文件栈。

## 5. 数据库 / Checkpoint 现状

- `agent_runs` 是 Run 状态与租约权威；`agent_run_events` 是 SSE 回放事实来源。
- Checkpoint 由共享 PostgreSQL 承载（生产）；团队事实没有独立表。
- MySQL 大 JSON 列禁止全行排序（已知陷阱 2），新表查询需窄列投影/索引。

## 6. 重构范围与实施顺序

1. 新增 Team Run / Member / Task / Dependency / Message / Artifact 表与迁移。
2. Task Board（CAS + 租约 + DAG）与 Message Bus、Artifact Store。
3. Member Runtime（Profile/Factory/Manager/Result）：DeepAgents、独立 Thread、预算、恢复。
4. Leader DeepAgents + Team Tools（受控工具 + 服务端权限）。
5. 通用 Team Graph 接线：Task Board 为事实来源，保留复核/返工/综合/恢复。
6. 事件、快照 API、前端身份展示、测试、部署与文档。
7. 软件项目团队保持原工程图；仅在后续阶段评估迁移。
