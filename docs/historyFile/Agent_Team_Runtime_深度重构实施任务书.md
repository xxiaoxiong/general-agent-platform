# Agent Team Runtime 深度重构实施任务书

**项目：** General Agent Platform  
**仓库：** https://github.com/xxiaoxiong/general-agent-platform  
**执行工具：** Codex  
**本轮范围：** 阶段一 Member Runtime 重构 + 阶段二 Team Collaboration
重构  
**目标：** DeepAgents 负责 Leader / Member 的 Agent 执行，LangGraph
负责团队控制，数据库负责持久化协作状态。

------------------------------------------------------------------------

## 1. 总体目标

将当前团队模式从“LangGraph DAG +
有限模型调用/worker”升级为真正可持续运行的多智能体团队系统。

目标架构：

``` text
User Request
    |
Team Run Service
    |
LangGraph Team Controller
    |------------------------------|
    |                              |
Team Lead Agent                Team Scheduler
  (DeepAgents)                   (LangGraph)
    |                              |
    +----------- Team Runtime -----+
                    |
       +------------+-------------+
       |            |             |
   Member A      Member B      Member C
  DeepAgents    DeepAgents    DeepAgents
       |            |             |
  Own Thread   Own Thread    Own Thread
  Own Tools    Own Tools     Own Tools
  Own State    Own State     Own State
       |            |             |
       +------------+-------------+
                    |
             Shared Services
                    |
       +------------+-------------+
       |            |             |
    Task Board   Message Bus   Artifact Store
       |            |             |
       +------------+-------------+
                    |
              Database
```

核心原则：

1.  Leader 和 Member 都是真正的 DeepAgents Agent，不允许用一次
    `ChatModel.invoke()` 包装成 Agent。
2.  每个成员有稳定身份、独立 Thread、独立
    Checkpoint、模型配置、工具权限和预算。
3.  LangGraph
    管理团队级控制流程、调度、依赖、验证和恢复，不负责替代成员的 Agent
    Loop。
4.  Task Board、Message Bus、Artifact
    是持久化协作服务，不依赖单进程内存。
5.  数据库保存团队协作事实，LangGraph Checkpoint 保存 Agent
    执行状态，两者职责不能混淆。
6.  保留现有软件项目团队、工程验收、文件修复和普通 Agent
    能力，不为统一架构而推倒重写。

## 2. 开工前代码审查

先拉取仓库当前目标分支，检查真实代码和依赖，再实施。不要假设历史观察仍然完全成立。

重点检查：

- `backend/app/services/agent_runtime/service/stream.py`
- `backend/app/services/agent_runtime/team_runtime.py`
- `backend/app/services/agent_runtime/team_graph.py`
- `backend/app/services/agent_runtime/team_project_runtime.py`
- `backend/app/services/agent_runtime/team_project_graph.py`
- `backend/app/services/agent_runtime/subagents.py`
- `backend/app/services/agent_runtime/factory.py`
- Checkpointer / Store 工厂、数据库模型、迁移、事件总线和相关测试

重点核实：

1.  `service/stream.py` 是否仍在 Team 分支创建 `TeamRun`，绕过普通
    `build_agent()`。
2.  Team 成员是否仍主要通过有限模型调用或简化 worker 执行。
3.  通用 `team_graph.py`
    是否包含规划、依赖调度、成员执行、证据复核、返工和综合交付。
4.  `team_project_graph.py`
    是否包含项目规划、文件分工、源码生成、提交、测试、浏览器验收和修复闭环。
5.  当前锁定的 DeepAgents、LangChain、LangGraph 版本及真实可用 API。

**依赖约束：** 以当前 lockfile / requirements 和实际安装版本为准。先验证
`create_deep_agent`、SubAgent、Checkpointer、Interrupt、Streaming 等
API。不得为了使用新 API 盲目升级整套 LangChain
依赖；如确需升级，先说明兼容性、影响范围并运行测试。

审查后输出简短报告：真实调用链、Leader / Member
当前执行方式、可复用模块、重构范围、数据库与 Checkpoint
现状、依赖风险和实施顺序。完成后继续实施；只有遇到真实阻塞或不可逆迁移风险才暂停询问。

## 3. 阶段一：Member Runtime 重构（P0）

### 3.1 统一成员执行体系

建立清晰的模块边界，建议包含以下职责（名称可按项目规范调整）：

- `MemberProfile`：角色、Prompt、模型、工具和预算配置。
- `MemberAgentFactory`：使用当前版本 DeepAgents 构建 Agent。
- `MemberRuntimeManager`：创建、启动、恢复、中断、取消和结束成员。
- `MemberExecutionContext`：团队、成员、任务、权限、预算、事件和
  Checkpoint 上下文。
- `MemberResult`：统一输出、Artifact 引用、错误和执行统计。

不要把所有逻辑继续堆入 `team_runtime.py`。

### 3.2 Leader / Member 使用 DeepAgents

**Member 必须具备：**

- 独立 System Prompt、Model Route、Tools、Middleware。
- 完整多轮 Agent Loop，可连续思考、调用工具、观察结果并继续。
- 独立执行预算和权限边界。
- 可读取被授权的任务资料和 Artifact。
- 可通过 Team Tools 更新任务、发送消息和提交成果。

**Leader 必须具备：**

- 完整 DeepAgents 执行能力，而不只是一次性生成任务 JSON。
- 通过受控工具创建/查询/更新任务、分配成员、查看状态、发送消息、读取成果和组织最终交付。
- 不得绕过 Task Board 直接修改成员或任务数据库。

LangGraph 仍负责团队外层控制流程。不要让 Leader 自主循环与 LangGraph
Scheduler 同时争抢任务所有权。

### 3.3 成员身份与状态

每个成员至少保存：

| 字段                        | 说明                            |
|-----------------------------|---------------------------------|
| `member_id`                 | 团队内稳定唯一 ID               |
| `team_run_id`               | 所属团队 Run                    |
| `parent_member_id`          | 上级成员，可为空                |
| `role`                      | 成员角色                        |
| `profile_snapshot`          | 本次使用的角色配置快照          |
| `thread_id`                 | 独立、稳定的 Checkpoint Thread  |
| `model_snapshot`            | 本次使用的模型配置快照          |
| `status`                    | 生命周期状态                    |
| `budget`                    | Token、模型调用、工具和时间预算 |
| `last_error`                | 最近错误摘要                    |
| `started_at / completed_at` | 执行时间                        |

成员状态至少包括：`created`、`ready`、`running`、`waiting`、`completed`、`failed`、`interrupted`、`cancelled`。实现合法状态转换，避免重复启动、错误完成和状态倒退。

### 3.4 独立 Checkpoint 与恢复

- 每个成员使用稳定唯一 Thread ID，例如
  `team:{team_run_id}:member:{member_id}`。
- 复用项目现有 Checkpointer 工厂和后端，不要为每个成员重复创建连接池或
  Checkpointer。
- 同一成员后续执行必须复用原 Thread；不同 Team Run / Member 必须隔离。
- 恢复时从成员自己的 Checkpoint 恢复，不能串用其他成员状态。
- 覆盖正常完成、模型异常、工具异常、超时、取消、人工中断和进程重启。
- 涉及 Interrupt 时，按当前依赖版本真实 API
  实现，确保恢复不重复执行已成功完成的工具步骤。

### 3.5 Profile、模型路由与权限

Profile 至少支持：

- Role / Name / Description / System Prompt。
- Model Route。
- Tool Allowlist。
- Middleware 配置。
- 最大模型调用次数、Token、执行时限和并发限制。
- 文件、数据库、网络等工具权限。
- 是否允许派生子成员（本轮默认关闭或严格限制）。

支持团队默认模型、角色指定模型和经过校验的成员模型覆盖。保存模型配置快照及实际模型
ID，但不得保存 API Key 等秘密。

所有工具必须走现有 Tool Registry / Policy /
Adapter。不能因成员属于团队就绕过用户权限、审批、数据源绑定或工具安全策略。不同成员不得共享可变数据库
Session。任务描述、外部检索结果、消息和 Artifact
均视为不可信数据，不自动获得系统指令权限。

### 3.6 事件与观测

保留现有 Run Event 协议，增量扩展。至少记录 Team 和 Member
的创建、启动、等待、恢复、完成、失败、取消，以及当前任务、模型调用、工具调用、Token、耗时、Checkpoint
恢复次数。

事件尽可能包含
`team_run_id`、`member_id`、`task_id`、`run_id`、`thread_id`。不得把凭据、完整敏感文件或不必要业务正文写入普通日志。

### 3.7 阶段一验收

- 单个 Member 能连续调用多个工具完成多步骤任务。
- 两个 Member 使用不同 Thread，历史与 Checkpoint 完全隔离。
- 同一 Member 能多轮继续并从中断状态恢复。
- 服务重启后成员能从自己的 Checkpoint 恢复。
- 不同角色可使用不同模型、Prompt、工具和预算。
- 超时、预算耗尽、工具异常、取消均有正确状态。
- Leader 能通过 Team Tools 管理团队并组织交付。
- 普通 Agent 路径不受影响。

## 4. 阶段二：Team Collaboration 重构（P0）

协作能力必须做成独立业务服务，不要把 SQL 和权限逻辑直接写入 Prompt 或
LangGraph 节点。

### 4.1 Task Board

持久化任务至少包含：

- `task_id`、`team_run_id`、`parent_task_id`
- `subject`、`description`、`status`
- `owner_member_id`、`priority`、`revision`
- `write_scopes`、`retry_count`
- `lease_expires_at`、`result_artifact_id`
- `created_at`、`updated_at`

支持创建、查询、更新、分配、认领、释放、依赖管理、阻塞原因查询、完成、失败、取消和重试。

任务状态至少包括：`pending`、`ready`、`claimed`、`running`、`blocked`、`completed`、`failed`、`cancelled`。

依赖关系需可校验：依赖必须属于同一 Team
Run；禁止自依赖、重复依赖和循环依赖。

### 4.2 CAS 并发控制

借鉴 DeepSeek Harness Task Board 的 Revision 机制：

- 更新必须携带 `expected_revision`。
- 使用数据库原子条件更新，例如 `WHERE task_id = ? AND revision = ?`。
- 成功后 revision 递增；失败返回明确冲突。
- 认领、释放、完成、重新分配都必须有并发保护。
- 不允许仅在 Python 层先查询再更新，这会产生竞态。

### 4.3 任务租约与故障恢复

- 认领任务时写入 owner、lease token 和过期时间。
- 执行期间按需续租或更新心跳。
- 正常完成后释放租约并提交结果。
- 成员异常退出或租约过期后，任务可被重新调度。
- 租约失效的旧成员不得覆盖新成员结果。
- 完成操作必须校验 owner、lease token 和 revision。
- 重复提交具备幂等保护。

优先用现有数据库和 SQLAlchemy
的原子事务实现。必须按项目实际数据库验证锁语义，不得假定 SQLite 与
PostgreSQL 的 `SELECT FOR UPDATE` 行为相同。

### 4.4 Message Bus

持久化消息至少包含：

`message_id`、`team_run_id`、`sender_member_id`、`recipient_member_id`、`message_type`、`payload`、可选
`artifact_id`、`correlation_id`、`idempotency_key`、`status`、时间戳。

消息状态至少包括：`pending`、`delivered`、`acked`、`failed`。

提供：

- `send_message`
- `list_inbox`
- `ack_message`
- `get_message`
- `wait_for_message` 或等价可恢复等待能力

交付语义：

- 先持久化消息，再尝试唤醒接收方。
- 接收方运行中：消息进入
  Inbox，在安全的下一轮输入边界读取；不得声称能强行改变正在生成的模型输出。
- 接收方空闲：通知 Scheduler 启动或恢复该成员。
- 接收方已结束：按策略拒收、重新激活或转交 Leader。
- 通过 message ID / idempotency key 去重。
- “进入收件箱”不等于“已阅读或已处理”。
- 实现可恢复的至少一次投递，不要无依据宣称 exactly-once。

### 4.5 Artifact Store

成员成果不应全部复制进其他成员 Prompt。建立统一 Artifact 服务：

- 保存元数据、内容或现有文件存储引用。
- 记录创建者、所属 Team、关联 Task。
- 支持摘要、完整内容和指定片段读取。
- 校验用户、团队和成员访问权限。
- 记录大小、类型、Checksum 和创建时间。
- 消息和任务只需引用 Artifact ID。

优先复用项目现有文件/文档/对象存储抽象。大文件不得直接塞入消息 payload
或 LangGraph State。

### 4.6 Team Tools

Leader 至少拥有：

- `team_task_create`
- `team_task_list`
- `team_task_get`
- `team_task_update`
- `team_assign_task`
- `team_send_message`
- `team_member_status`
- `team_artifact_get`

Member 至少拥有：

- `team_task_get`
- `team_task_update`（仅允许更新有权操作的字段）
- `team_send_message`
- `team_inbox_list`
- `team_message_ack`
- `team_artifact_get`
- `team_artifact_create`

工具命名和 Schema 可按现有规范调整，但权限必须由后端强制校验，不能只靠
Prompt 约束。

### 4.7 阶段二验收

- Team、Member、Task、Message、Artifact 均持久化。
- 服务重启后团队协作状态不丢失。
- 并发更新能正确触发 CAS 冲突。
- 两个成员不能同时成功认领同一独占任务。
- 成员 A 能直接给成员 B 发消息，无需 Leader 转发。
- 运行中成员能在后续安全边界读取消息。
- 空闲成员可被消息或任务调度唤醒。
- 成员可保存 Artifact，其他授权成员按引用读取。
- 重复消息、任务提交和完成请求不会造成重复副作用。
- 跨 Team Run 的任务、消息和 Artifact 访问被拒绝。

## 5. 现有团队图和软件项目团队集成

### 5.1 通用 Team Graph

保留 `team_graph.py` 已有的：

- 规划和结构校验。
- DAG 依赖和并行任务分发。
- 成员执行。
- 证据复核、成员返工和再次复核。
- 最终综合、失败说明和 Checkpoint 恢复。

将简化 worker 接入统一 Member Runtime。任务状态以 Task Board
为事实来源；LangGraph State
只保存控制流程需要的状态、引用和摘要，避免重复维护任务事实和复制完整成果。

### 5.2 软件项目团队必须保留

`team_project_graph.py` / `team_project_runtime.py`
属于重要业务资产。本轮不得无理由重写或削弱：

- 项目计划与依赖校验。
- 文件归属、路径安全校验。
- 分工生成源码、文件提交与恢复。
- 代码结构检查、测试和验收。
- 浏览器真实操作验收（若当前启用）。
- 失败文件修复和重试。
- 项目进度和最终交付。

本轮优先建设可复用的 Member Runtime / MemberExecutor
接口。除非审查确认迁移风险低且回归测试充分，否则不要强行把软件项目专用工作流整体迁移到新
Scheduler。

**软件项目团队的现有功能和验收通过率不得下降。**

### 5.3 普通 Agent

普通对话、普通 Agent、文档图和软件项目图的路由必须保持可用。不要把所有
Run 都包装成 Team Run。Team Runtime 初始化失败时，必须正确结束 Run
并释放资源。

## 6. 数据库、并发和预算

先检查项目 ORM、迁移工具和数据库支持范围，再确定表结构。建议实体：

- Team Run
- Team Member
- Team Task
- Task Dependency
- Team Message
- Team Artifact

要求：

- 使用项目现有 ORM 和迁移规范。
- 明确外键、索引、唯一约束和级联策略。
- Team、Member、Task、Inbox、租约查询有合理索引。
- CAS、任务认领和消息确认在数据库事务层保证原子性。
- 不删除或重命名现有业务表。
- 迁移兼容项目实际支持的数据库；考虑历史 Team Run 数据。
- 不保存密钥或无差别保存完整敏感上下文。

基础保护：

- Team Run 最大成员数和最大并发成员数。
- Member 级模型调用、Token、工具和时间预算。
- Provider / Model 维度并发限制或可插拔容量接口。
- 取消团队后停止后续调度，并处理正在执行的成员。
- Leader 失败不抹掉已完成成员成果。
- 单个 Member 失败不把已完成任务标记失败。
- 租约过期可恢复调度。
- 重试次数有限并有退避策略。
- 模型调用、工具调用和消息处理有必要的幂等保护。

必须分别验证“数据库任务状态恢复”和“Agent Checkpoint
恢复”，不能将两者混为一谈。

## 7. 阶段三、四的扩展预留

本轮不全面实现，只保证接口可扩展。

**阶段三：Dynamic Team Scheduler**

- Agent 自主创建和拆分任务。
- 动态修改依赖。
- 自动认领、调度和失败接管。
- 成员动态增删和重新分配。
- 有上限的成员递归派生。
- 模型级全局容量调度和动态重新规划。

**阶段四：Team Verification & Evaluation**

- 独立 Critic Agent 和证据核验。
- 团队质量评分、成员贡献和成本统计。
- Solo vs Team 对照评测。
- 团队执行回放和前端实时视图。

不要在本轮提前实现无限制自治。

## 8. 测试要求

不接受只通过手工对话验证。

**单元测试：**

- Profile 校验、Member ID / Thread ID 隔离、状态转换。
- Member Factory 配置和工具权限。
- DAG 校验、CAS 冲突、任务认领和租约。
- 消息状态、幂等、Artifact 权限。
- 预算耗尽和错误映射。

**集成测试：**

- 创建 Team 并启动多个独立 Member。
- 多成员并行执行、同一成员多轮继续。
- Member Checkpoint 恢复、中断和取消。
- 成员间发送、接收、确认消息。
- 空闲成员唤醒。
- 租约过期和重新认领。
- Artifact 创建、读取和权限隔离。
- Leader 汇总成员结果。
- 数据库重启后恢复团队状态。

**并发与故障测试：**

- 两个 Member 同时认领同一任务。
- 两个 Member 使用相同 revision 更新任务。
- 消息重复投递。
- Member / Leader 执行中进程退出。
- Checkpoint 已写入但任务状态尚未更新。
- 任务状态已更新但消息唤醒失败。
- 模型超时、工具失败、预算耗尽。
- 团队取消时仍有成员执行。

**回归测试：**

- 普通 Agent 对话、工具调用、中断、恢复和取消。
- 通用 Team Graph、证据复核和返工。
- 软件项目团队端到端生成、测试、修复和交付。
- 文档任务路由。
- 现有前端团队进度和事件消费。

未实际运行的测试不得标记为通过。外部模型服务或环境导致无法执行时，明确列出阻塞点和可重复运行命令。

## 9. 前端与事件兼容

本轮以后端为主，不进行大规模 UI 重设计。

- 保留现有 TeamRunCard 和事件协议。
- 新事件向后兼容，不改变既有事件字段含义。
- 至少支持展示成员身份、角色、状态、当前任务和错误摘要。
- 任务状态以服务端 Task Board 为准。
- 断线重连后可通过服务端快照恢复团队状态，不依赖浏览器内存事件。

## 10. 建议模块边界

以下为职责建议，按现有目录风格落地，不要为追求目录形式大规模搬迁：

``` text
agent_runtime/
  team/
    runtime/
      member_factory.py
      member_manager.py
      member_context.py
      member_profile.py
      member_result.py
    services/
      task_board.py
      message_bus.py
      artifact_store.py
      scheduler.py
    repositories/
      team_repository.py
      member_repository.py
      task_repository.py
      message_repository.py
      artifact_repository.py
    tools/
      team_tools.py
    graphs/
      team_controller.py
```

必须保持 Agent 构建、成员生命周期、业务 Service、Repository、Team Tools
和 LangGraph 节点之间职责清晰。LangGraph 节点不得直接承担复杂 SQL
和权限逻辑。

## 11. 实施顺序

1.  审查当前代码、依赖和真实调用链。
2.  确定 Member Runtime 接口、Profile 和 Thread 规则。
3.  实现 MemberAgentFactory 和单成员多步骤执行。
4.  实现独立 Checkpoint、恢复、取消和预算。
5.  将通用 Team Graph 成员执行接入 Member Runtime。
6.  完成 Leader 的统一 DeepAgents 构建和受控团队工具。
7.  实现 Team / Member / Task / Message / Artifact 数据模型与迁移。
8.  实现 Task Board、CAS、任务认领和租约。
9.  实现 Message Bus、Inbox、Ack 和成员唤醒。
10. 实现 Artifact Store 和引用式成果传递。
11. 完成事件、权限、并发和恢复测试。
12. 完成普通 Agent、通用 Team、软件项目 Team 全量回归。
13. 更新架构文档、部署配置和迁移说明。

每完成一个逻辑阶段，先运行对应测试并修复，再继续。不要积累大量未经验证的改动。

## 12. 最终交付物

1.  可运行的 Member Runtime。
2.  Leader / Member 统一 DeepAgents 构建路径。
3.  独立 Member Checkpoint 和恢复机制。
4.  Task Board、CAS、认领和租约。
5.  持久化 Message Bus、Inbox 和 Ack。
6.  Artifact Store。
7.  数据库模型和迁移。
8.  Team Tools 注册与权限校验。
9.  Team / Member / Task 事件和观测能力。
10. 自动化单元、集成、并发、恢复和回归测试。
11. 架构说明：模块职责、调用链、状态流转、数据表和恢复机制。
12. 部署与迁移说明：新增配置、环境变量、迁移和回滚注意事项。
13. 真实测试报告：命令、通过数量、失败数量、未执行项和原因。
14. 变更文件清单和关键设计取舍。

## 13. 最终验收清单

- [ ] Leader 和 Member 使用统一 DeepAgents 构建体系。
- [ ] Member 是完整多步骤 Agent，不是单次模型调用包装。
- [ ] 每个成员有独立、稳定、可恢复的 Checkpoint Thread。
- [ ] 成员可按角色使用不同模型、Prompt、工具和预算。
- [ ] LangGraph 管理团队控制流程，不替代成员 Agent Loop。
- [ ] Task Board 持久化，CAS 和任务认领具备并发正确性。
- [ ] 成员可直接通信，消息持久化、可确认、可恢复。
- [ ] 成员成果可通过 Artifact 引用共享。
- [ ] 服务重启后 Team、Member、Task、Message 和执行状态可恢复。
- [ ] 权限、预算、并发和租约机制有效。
- [ ] 普通 Agent 能力没有回归。
- [ ] 软件项目团队生成、测试、验收、修复和交付能力没有回归。
- [ ] 自动化测试和真实运行报告完整。
- [ ] 文档与实际代码一致，不存在只完成接口或空壳实现。

**最终判断标准：** 不是“创建了多个 DeepAgent
实例”，而是成员具备独立执行与恢复能力，团队具备持久化任务与通信机制，并能在并发、失败和服务重启后继续正确协作。
