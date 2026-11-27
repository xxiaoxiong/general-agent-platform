# General Agent Platform --- Work Orchestrator 工作任务编排层改造任务书

**版本：** V1.0\
**代码基线：** `xxiaoxiong/general-agent-platform`，`main`
分支；本次审查基线提交 `de216eee`（2026-11-27）\
**执行对象：** Codex / Trae\
**改造范围：** Work Orchestrator（工作任务编排层），不包含通用 Browser
Agent、定时任务、实时语音、图片/视频生成等其他产品能力。

------------------------------------------------------------------------

## 1. 改造目标

将 General Agent Platform 从"具备多个独立 Agent
能力的对话平台"，升级为"能够根据用户最终目标自动规划、选择执行引擎、组织多个工作步骤、持续推进并验收交付物的工作平台"。

用户只描述最终目标，系统负责：

1.  理解目标、约束、上下文和交付要求。
2.  判断任务是简单任务、单一专业任务，还是跨能力域的复合任务。
3.  对复杂任务生成结构化 Work Plan。
4.  根据服务端能力注册表选择现有执行引擎。
5.  按依赖关系执行工作步骤，传递必要的中间成果。
6.  处理中断、审批、失败、恢复和有限返工。
7.  验证步骤结果与最终交付物。
8.  通过现有 Run 事件流展示任务计划和进度。

**核心原则：Work Orchestrator 是任务级控制与编排层，不替代
DeepAgents、LangGraph、现有 Agent Loop、Workflow Engine 或专业验证器。**

## 2. 当前代码审查结论

### 2.1 已有能力，必须复用

当前项目已经具备以下基础：

  ---------------------------------------------------------------------------------------------------------------------------
  现有模块                当前职责                                                         本次改造中的定位
  ----------------------- ---------------------------------------------------------------- ----------------------------------
  Decision Architecture   任务意图、连续性、上下文引用、目标、约束、交付物和验收标准识别   Work Orchestrator 的任务理解入口

  `task_router.py`        确定性文档任务识别和路由预处理                                   保留兼容，不能再扩展成第二套通用
                                                                                           Router

  Task Contract           任务类型、目标、交付格式和完成条件                               作为整体任务验收依据之一

  Verification            基于工具账本和执行证据做确定性验证                               复用，不允许模型自报完成

  DeepAgents              通用 Agent Loop、工具调用、文件系统和子代理                      通用专业执行引擎

  Team Graph              动态分工、依赖、并行、综合和证据复核                             单个工作步骤内部的团队协作引擎

  Team Project Graph      软件项目规划、源码生成、运行和浏览器验收                         软件项目专业执行引擎

  Document Graph          Word 文档生成、编辑、审阅和版本管理                              文档专业执行引擎

  RunWorker               Run Claim、Lease、Heartbeat、Fencing、恢复和执行                 全部 Work Plan 继续复用

  Checkpointer / Store    图状态持久化、跨线程记忆                                         编排状态和记忆基础设施

  EventBus / SSE          事件持久化、序号、重放和实时推送                                 Work Plan 进度的唯一事件通道

  ToolPolicy / Approval / 工具权限、副作用审批、幂等和异常核对                             所有执行步骤必须继续遵守
  Tool Ledger                                                                              
  ---------------------------------------------------------------------------------------------------------------------------

### 2.2 当前主要断层

1.  `RunDecision` 能识别
    `research`、`data_analysis`、`database_query`、`coding`、`business_action`
    等意图，但最终执行路线仍然主要是单一路线。
2.  `DecisionValidator.expected_route()` 当前主要将文档任务映射到
    `document_graph`，软件项目在允许时映射到
    `team_project_graph`，其他常见意图通常回到 `deep_agent`。
3.  `apply_decision()`
    会保存目标、约束、交付物和验收标准，但没有生成可执行的跨执行引擎步骤
    DAG。
4.  Team Graph 已经能在一个团队任务内拆分 2～4 个子任务，但它不是跨
    Research、Document、Data、Project 等专业执行引擎的统一编排器。
5.  `stream.py::_drive()` 同时承担决策、RuntimeContext
    构建、执行模式分发、流式消费、验证和 Run
    收尾，新增路线容易继续堆积条件分支。
6.  通用 `TaskContract / Verification` 主要覆盖普通
    Agent；文档和团队执行有自己的验证机制，缺少统一的 Work Step
    结果与最终验收抽象。
7.  当前 `WorkflowEngine` 是用户预先设计的固定流程，且 Workflow API
    执行路径与 RunWorker 的 Durable Run 生命周期不同，不适合作为动态
    Work Orchestrator 的直接替代品。

### 2.3 关键代码入口

实施前再次以当前仓库为准核对代码，不得仅依据本任务书假设行号不变。

-   [Run
    API](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/api/run/core.py)
-   [RunCreate
    Schema](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/api/common/schemas.py)
-   [Task
    Router](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/task_router.py)
-   [Decision
    Schema](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/decision/schemas.py)
-   [Decision
    Context](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/decision/context.py)
-   [Decision
    Validator](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/decision/validator.py)
-   [Decision
    Orchestrator](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/decision/orchestrator.py)
-   [Decision
    Apply](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/decision/apply.py)
-   [Task
    Contract](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/assistant/task_contract.py)
-   [Verification](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/assistant/verification.py)
-   [Runtime
    Stream](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/service/stream.py)
-   [Run
    Worker](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/worker.py)
-   [Run
    Persistence](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/persistence.py)
-   [Team
    Runtime](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/team_runtime.py)
-   [Team
    Graph](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/team_graph.py)
-   [Team Project
    Runtime](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/team_project_runtime.py)
-   [Document
    Graph](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/document_graph.py)
-   [Event
    Protocol](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/event_protocol.py)
-   [Event
    Storage](https://github.com/xxiaoxiong/general-agent-platform/blob/main/backend/app/services/agent_runtime/events.py)
-   [Frontend Event
    Reducer](https://github.com/xxiaoxiong/general-agent-platform/blob/main/frontend/src/features/run/blocks.ts)
-   [Workspace
    UI](https://github.com/xxiaoxiong/general-agent-platform/blob/main/frontend/src/views/workspace/WorkspaceHome.vue)

## 3. 目标架构与职责边界

``` mermaid
flowchart TD
    A["User Goal"] --> B["Existing Decision Architecture"]
    B --> C["Context Resolution"]
    C --> D{"Complex / Composite Task?"}
    D -->|No| E["Direct Execution Path"]
    D -->|Yes| F["Work Planner"]
    F --> G["WorkPlan Validator"]
    G --> H["Work Orchestrator Graph"]
    H --> I["Executor Registry"]
    I --> J["DeepAgents Adapter"]
    I --> K["Team Graph Adapter"]
    I --> L["Document Graph Adapter"]
    I --> M["Team Project Adapter"]
    J --> N["Normalized Step Outcome"]
    K --> N
    L --> N
    M --> N
    N --> O["Step Verification"]
    O -->|Failed / Partial| P["Bounded Repair"]
    P --> H
    O -->|Passed| Q{"More Steps?"}
    Q -->|Yes| H
    Q -->|No| R["Final Verification"]
    E --> R
    R --> S["Artifact Delivery"]
    S --> T["Existing Run Completion"]
```

职责边界：

  ---------------------------------------------------------------------------------------------------------
  层                      负责什么                                         不负责什么
  ----------------------- ------------------------------------------------ --------------------------------
  Decision Architecture   理解本轮请求、连续性、目标、资源引用和意图       不负责生成复杂步骤 DAG

  Work Planner            将复合目标拆成有限、可验证的工作步骤             不执行工具、不授予权限

  WorkPlan Validator      校验计划结构、依赖、执行器、能力和资源引用       不相信模型自报权限

  Work Orchestrator Graph 调度步骤、传递结果、状态推进、恢复和有限重规划   不重新实现 Agent Loop

  Executor Registry       将受控 executor profile 映射到已有执行引擎       不允许模型任意指定 Python
                                                                           类或工具名

  专业执行引擎            完成具体任务                                     不接管整个 Work Plan

  Verification            依据工具账本、产物和专业验证结果验收             不以模型的"已完成"文本作为证据
  ---------------------------------------------------------------------------------------------------------

## 4. WorkPlan 数据契约

新增独立、严格的 Pydantic 数据模型，建议放在：

`backend/app/services/work_orchestrator/schemas.py`

### 4.1 WorkPlan

至少包含：

-   `schema_version`
-   `plan_id`
-   `revision`
-   `objective`
-   `constraints`
-   `context_refs`
-   `deliverables`
-   `acceptance_criteria`
-   `steps`
-   `created_at`
-   `planner_metadata`（模型、耗时、是否 fallback；不得存密钥）

### 4.2 WorkStep

每个步骤至少包含：

-   `id`：稳定、唯一的步骤 ID。
-   `title`：简短步骤名称。
-   `objective`：该步骤可独立执行的目标。
-   `executor_profile`：服务端允许的执行器类型。
-   `depends_on`：前置步骤 ID。
-   `input_refs`：允许引用的会话资源或前置步骤产物。
-   `required_capabilities`：能力要求，只能从当前 Agent
    已授权能力中选择。
-   `deliverables`：预期产物。
-   `acceptance_criteria`：步骤验收条件。

执行状态单独维护，不要让模型生成的静态计划与运行时状态混在一起。

步骤状态建议：

`pending / running / waiting_approval / completed / failed / skipped / needs_reconciliation`

计划状态建议：

`planning / ready / running / verifying / paused / completed / failed / cancelled`

### 4.3 计划示例

用户请求："调研国产 Agent 平台，比较技术架构，最后生成 Word 报告。"

``` json
{
  "schema_version": "v1",
  "objective": "调研并比较国产 Agent 平台，生成 Word 报告",
  "steps": [
    {
      "id": "research",
      "title": "资料检索与核验",
      "objective": "收集平台官方资料、架构信息和可核验来源",
      "executor_profile": "research",
      "depends_on": [],
      "input_refs": [],
      "deliverables": ["research_sources"],
      "acceptance_criteria": ["关键结论有可核验来源"]
    },
    {
      "id": "analysis",
      "title": "架构比较分析",
      "objective": "基于研究结果比较技术架构、能力和差异",
      "executor_profile": "general_agent",
      "depends_on": ["research"],
      "input_refs": ["step:research"],
      "deliverables": ["comparison_analysis"],
      "acceptance_criteria": ["比较维度完整", "区分事实与判断"]
    },
    {
      "id": "document",
      "title": "生成 Word 报告",
      "objective": "将分析结果整理为结构化 Word 报告",
      "executor_profile": "document_docx",
      "depends_on": ["analysis"],
      "input_refs": ["step:analysis"],
      "deliverables": ["report.docx"],
      "acceptance_criteria": ["文件真实生成", "章节和来源清单完整"]
    }
  ],
  "acceptance_criteria": [
    "研究结论有可核验来源",
    "架构比较覆盖主要维度",
    "Word 文件可下载且通过文档校验"
  ]
}
```

示例仅表达数据结构。实际执行器、能力和资源引用必须由服务端注册表及权限校验决定。

### 4.4 Schema 约束

-   `extra="forbid"`，禁止未定义字段。
-   步骤数量初期限制为 1～6。
-   每个步骤 ID 唯一，依赖只能引用已定义步骤。
-   必须检测循环依赖。
-   文本长度、数组长度和总计划大小均设置硬上限。
-   `executor_profile` 使用 Literal / Enum 白名单。
-   `input_refs` 必须来自 DecisionContext
    的有效候选引用，或已经完成步骤的输出。
-   模型不能输出任意工具名、任意 Python 类名、任意 API
    URL、任意工作区路径或用户权限。
-   显式用户约束、交付格式和资源绑定优先级高于模型建议。

## 5. 任务理解与规划策略

### 5.1 复用 Decision Architecture

保留现有：

-   `build_decision_context`
-   `decide`
-   `validate_decision`
-   `apply_decision`
-   `resolve_run_decision`

不再开发第二套意图识别系统。

Work Orchestrator 消费经过服务端校验的 Decision 结果，包括
intent、goal、constraints、deliverables、acceptance
criteria、continuity、context refs 和实际执行权限。

当前文档资源绑定、安全校验和 `prepare_execution()` 逻辑继续生效。

### 5.2 复杂任务判断

不是每条消息都生成 WorkPlan。

以下情况优先进入 Work Planner：

1.  一个目标包含两个以上有明确先后关系的工作阶段。
2.  一个请求同时需要不同专业执行引擎，例如研究后生成 Word。
3.  多个交付物需要不同处理过程。
4.  一个目标包含多个需要独立验证的业务目标。
5.  当前 Task Contract 判定为复杂任务，且具备拆分价值。

普通问答、简单解释、单文件读取、简单单工具操作，直接走原有执行路径。

**简单任务不能因为新增编排层而多一次 Planner 模型调用。**

### 5.3 Planner

新增 `WorkPlanner`：

-   输入：用户目标、已校验
    Decision、必要上下文引用、允许使用的执行器描述和已授权能力。
-   输出：严格符合 WorkPlan Schema 的 JSON。
-   Planner 只规划，不调用工具。
-   规划模型调用有超时、输出 Token 上限和一次格式修复机会。
-   不向 Planner 提供无关的完整历史会话或大文件正文。
-   对于已持久化的 WorkPlan，恢复时直接复用，不重新规划。

复杂任务可以额外进行一次规划模型调用；普通任务不增加调用。

### 5.4 规划失败回退

Planner 超时、输出非法、依赖有环、执行器不存在或能力不匹配时：

-   不得把普通 Run 直接判为失败。
-   记录 `work_plan.degraded` 事件或等价诊断信息。
-   安全回退到现有 Decision + 单一执行引擎路线。
-   不允许为了让计划通过而自动扩大工具权限。
-   若资源缺失或目标存在关键歧义，沿用现有澄清机制。

## 6. Executor Registry

新增服务端执行器注册表，建议放在：

`backend/app/services/work_orchestrator/registry.py`

初期仅注册当前真实存在的执行能力：

  --------------------------------------------------------------------------------
  executor_profile        实际执行引擎            适用场景
  ----------------------- ----------------------- --------------------------------
  `general_agent`         DeepAgents              通用推理、分析、综合和普通任务

  `research`              DeepAgents + 当前 Web   资料搜索、来源整理和研究
                          Search / 网页读取能力   

  `data_analysis`         DeepAgents +            数据分析、统计和计算
                          已授权数据/计算工具     

  `database`              DeepAgents +            数据库查询
                          已绑定数据库工具        

  `coding`                DeepAgents +            代码任务
                          当前代码/Sandbox 工具   

  `business_action`       DeepAgents + ToolPolicy 受控业务操作
                          / HITL                  

  `document_docx`         Document Graph          Word 生成、编辑和审阅

  `team`                  TeamRun / Team Graph    单个复杂工作步骤内部的团队协作

  `software_project`      Team Project Graph      软件项目构建和验收
  --------------------------------------------------------------------------------

注意：

1.  `research`、`data_analysis`
    等初期是执行配置/Profile，不代表已经新建独立 Research Harness 或
    Data Harness。
2.  `team` 必须遵守显式 `team_mode` 或服务端 `AGENT_AUTO_TEAM_ENABLED`
    配置，不得因模型建议绕过用户选择和系统开关。
3.  `software_project` 继续遵守项目范围、Sandbox ACL 和自动团队配置。
4.  `business_action` 必须沿用现有副作用风险等级、审批策略和 Tool
    Execution Ledger。
5.  当前内置 Chromium 主要用于 Sandbox 项目预览，不是通用互联网 Browser
    Agent；本次不得注册不存在的 Browser Agent。
6.  Workflow Engine 暂不作为自动 Work Step 执行器；后续如需调用已发布
    Workflow，应单独设计带 Run 上下文的适配器。

Registry 只能由服务端维护。模型只能提出 profile
建议，最终执行器由服务端结合当前 Agent 配置、资源绑定和权限决定。

## 7. Work Orchestrator 执行图

新增独立包，建议结构：

``` text
backend/app/services/work_orchestrator/
├── __init__.py
├── schemas.py
├── complexity.py
├── planner.py
├── validator.py
├── registry.py
├── adapters/
│   ├── base.py
│   ├── deep_agent.py
│   ├── document.py
│   ├── team.py
│   └── project.py
├── graph.py
├── persistence.py
└── verification.py
```

允许根据现有代码组织方式调整文件拆分，但必须保持职责清晰，不要把全部实现堆进一个超大文件。

### 7.1 执行流程

建议 Work Orchestrator Graph 至少包含：

1.  `load_or_create_plan`：读取已持久化计划；不存在时才规划。
2.  `validate_plan`：服务端校验 Schema、依赖、执行器、权限和资源。
3.  `select_ready_step`：选择所有前置步骤已完成的步骤。
4.  `execute_step`：调用 Executor Adapter。
5.  `verify_step`：根据步骤类型调用对应验证器。
6.  `repair_or_fail`：有限修复；超过次数后保留部分成果并失败/暂停。
7.  `update_progress`：持久化状态并发布事件。
8.  `final_verify`：核对整体目标、所有必需步骤和交付物。
9.  `deliver`：返回最终文本、产物和验收摘要。

初期不同类型步骤采用**串行执行**，`AGENT_WORK_MAX_PARALLEL_STEPS=1`。现有
Team Graph
内部并行不受影响。后续再评估只读、无共享写资源步骤的跨步骤并行。

### 7.2 Executor Adapter 统一接口

不同执行引擎对外统一返回 `WorkStepOutcome`，建议字段：

-   `step_id`
-   `status`
-   `summary`
-   `final_text`
-   `artifact_refs`
-   `evidence_refs`
-   `verification`
-   `error_code`
-   `retryable`
-   `checkpoint_ref`

Adapter 内部继续使用原有引擎的真实接口、工具策略、Checkpoint
和验证逻辑。

不要把各执行引擎内部的消息对象、LangGraph State 或工具返回对象直接暴露给
WorkPlan Graph。

### 7.3 上下文传递

-   后续步骤只能获得其 `input_refs` 指定的上下文和前置步骤输出。
-   不要把所有步骤的完整工具输出无条件拼入 Prompt。
-   大文件、长报告和源码使用文件引用或 Artifact 引用传递。
-   资源引用必须再次进行用户、项目、会话和版本校验。
-   历史对话语义检索不是本次改造范围；不得假设它已经存在。

### 7.4 Team Graph 与 Work Orchestrator 的关系

两者分工必须明确：

-   Work Orchestrator：跨专业能力、跨执行引擎的工作步骤调度。
-   Team Graph：某个工作步骤内部的多角色分工和并行。
-   Team Project Graph：软件工程任务的专用构建和验收。

不要把现有 Team Graph 拆掉，也不要让 Work Planner
重新实现其内部团队规划、并行和证据复核。

## 8. Run 生命周期、Checkpoint 与恢复

这是本次改造的最高风险部分，必须优先保证正确性。

### 8.1 一个用户任务仍然只有一个 AgentRun

-   不要为 WorkPlan 的每个 Step 新建独立 AgentRun。
-   不要新建第二套 RunWorker、任务队列或 Lease 机制。
-   一个 WorkPlan 复用当前 Run 的
    `run_id`、用户身份、Session、Trace、Run 级预算、EventBus
    和工具账本。
-   仍由现有 RunWorker 负责
    Claim、Heartbeat、Fencing、恢复和最终状态提交。

### 8.2 Checkpoint Thread

Work Orchestrator 使用 Run 级唯一线程：

`work:{run_id}`

执行器内部若需要独立 Checkpoint，使用步骤级唯一线程：

`work:{run_id}:step:{step_id}`

必须避免不同 Run 在同一 Session 下共用 WorkPlan
Checkpoint，造成状态串扰。

现有 Team Graph / Document Graph / Team Project Graph 的 Checkpoint
Thread 需要支持步骤级隔离；兼容旧的显式 Team Mode 和旧 Run 恢复路径。

### 8.3 状态持久化

初期不建议新增一套独立任务数据库。

采用现有持久化设施：

-   `AgentRun.input_payload.execution_context.work_plan`：保存不可变的
    WorkPlan 快照、版本和计划元信息。
-   LangGraph
    Checkpointer：保存当前步骤、步骤结果、依赖状态、重试次数和恢复位置。
-   `AgentRunEvent`：保存计划与步骤进度，供 SSE 实时展示和历史回放。
-   `AgentRun.summary`：保存最终编排摘要、步骤数量、验收结果和产物摘要。

注意：当前 `decision/persistence.py::persist_execution_context()` 是读取
JSON 后更新输入快照的独立写入路径。WorkPlan 持久化必须补充并发安全和
Lease/Fencing 校验，不能直接在多处无保护地覆盖整个 `input_payload`。

计划生成应具备幂等性：计划写入成功后，即使 Worker
在发布事件前退出，恢复时也必须读取原计划，不得重新生成一份不同计划。

### 8.4 预算与副作用

-   全部步骤共享当前 Run 的 `ExecutionBudget`。
-   模型调用数、工具调用数、Token 和总时限不得在每个 Step 开始时清零。
-   继续使用 `ToolPolicy`、`AgentApproval`、`AgentToolExecution`。
-   外部副作用执行状态为 `unknown` 或 `needs_reconciliation`
    时，不允许自动重复执行。
-   只有在原工具执行账本确认可安全重放时，才允许恢复重试。
-   HITL 等待、用户拒绝、取消、超时、Worker 接管必须遵守现有 Run
    状态机。
-   不允许通过 WorkPlan 将 `approval_mode=manual` 改成自动审批。

建议给 `RuntimeContext` 增加可选
`work_step_id`，并让工具事件带上步骤归属。若步骤级验收需要从 Tool
Execution Ledger 精确取证，可给 `AgentToolExecution` 增加 nullable
`work_step_id` 字段并提供向后兼容迁移；不得破坏旧 Run 的工具账本读取。

### 8.5 失败与重规划

-   步骤失败后保留已完成步骤及其产物。
-   只重试失败步骤或明确需要修复的步骤。
-   初期自动修复/重规划最多 1 次，可配置。
-   不得因一个步骤失败而清空或重复执行全部已完成步骤。
-   依赖失败的后续步骤标记为 `skipped`，并说明原因。
-   用户执行中补充要求时，先在安全边界接收；必要时生成新计划
    revision，保留已完成且仍有效的步骤。
-   暂停、恢复、取消和 Worker 重启都必须能从 Checkpoint 继续。

## 9. 统一验收机制

不要重写现有专业验证器。

Work Orchestrator 只定义统一结果协议，例如：

``` text
WorkVerificationResult
- status: passed / partial / failed
- checks[]
- evidence_refs[]
- artifact_refs[]
- repairable
- stop_reason
```

不同 Adapter 调用对应的既有验证逻辑：

  执行器               验证方式
  -------------------- ----------------------------------------------------
  DeepAgents           现有 TaskContract + `verify_task()` + 工具执行证据
  Document Graph       现有文档结构、文件、版本和编辑校验
  Team Graph           现有任务依赖、成员结果和证据复核
  Team Project Graph   现有源码检查、构建、测试、浏览器和需求覆盖验收

最终 WorkPlan 验收至少检查：

1.  所有必需步骤是否完成。
2.  所有必需验收条件是否通过。
3.  声明的交付物是否真实存在、可读取或可下载。
4.  引用和外部操作是否有真实执行证据。
5.  是否存在未解决的副作用核对或审批状态。

任何步骤不能仅凭模型输出"已经完成"而通过。

当前简单问答仍走原有快速路径，不增加无意义的验证负担。

## 10. 事件协议与前端展示

### 10.1 新增独立 work 事件命名空间

不要复用 `plan.updated` 作为 WorkPlan 事件。当前 `plan.updated` 是
DeepAgents 内部 Todo 计划，必须保留。

建议新增内部事件及 v2 事件映射：

  内部事件                   v2 事件
  -------------------------- --------------------------
  `work_plan_created`        `work.plan.created`
  `work_plan_updated`        `work.plan.updated`
  `work_step_started`        `work.step.started`
  `work_step_completed`      `work.step.completed`
  `work_step_failed`         `work.step.failed`
  `work_step_verification`   `work.step.verification`
  `work_plan_completed`      `work.plan.completed`
  `work_plan_degraded`       `work.plan.degraded`

事件至少包含
`plan_id`、`revision`、`step_id`（步骤事件）、状态、标题、executor
profile、依赖摘要和必要的产物引用。

### 10.2 事件持久化要求

当前 `events.py` 对普通事件有默认 400
字符截断上限，`task_decision`、`team.updated` 等事件有单独策略。

WorkPlan
事件必须增加明确的存储长度和数组上限，确保完整计划可回放；不能依赖默认
400 字符策略。

所有事件继续执行敏感信息脱敏。不得将密钥、Cookie、原始凭证、完整附件正文或不必要的源码写入
WorkPlan 事件。

### 10.3 前端展示

新增：

-   `WorkPlanBlock`：对话消息中的工作计划状态。
-   `WorkPlanCard.vue`：计划卡片。
-   `blocks.ts`：WorkPlan 事件归约。
-   `WorkspaceHome.vue`：渲染工作计划卡片。
-   必要的 TypeScript 类型与单元测试。

展示内容：

-   总体目标。
-   当前计划阶段。
-   步骤列表、依赖关系和状态。
-   当前正在执行的步骤。
-   每步使用的执行能力（以用户可理解的名称展示）。
-   步骤产物和验收结果。
-   失败、跳过、等待审批或需要用户输入的原因。

实时流和历史回放必须使用同一套事件归约逻辑。页面刷新或重新打开会话后，WorkPlan
状态应由持久化事件完整重建。

第一版只做计划查看和进度展示，不做拖拽式计划编辑器，也不增加新的强制交互开关。

## 11. 配置与兼容策略

新增独立配置，不要复用或改变 `AGENT_DECISION_MODE` 语义：

``` env
AGENT_WORK_ORCHESTRATOR_MODE=shadow
AGENT_WORK_PLAN_TIMEOUT=20
AGENT_WORK_MAX_STEPS=6
AGENT_WORK_MAX_REPLANS=1
AGENT_WORK_MAX_PARALLEL_STEPS=1
```

模式：

-   `off`：完全走当前执行路径。
-   `shadow`：生成并校验候选
    WorkPlan，记录诊断和评测数据，但不改变实际执行路线。
-   `auto`：对符合条件的复杂任务应用 WorkPlan。

默认先使用 `shadow`。完成回归和真实任务评测后，再由部署配置切换到
`auto`。

兼容要求：

-   `RunCreate` 现有字段继续有效。
-   不强制增加新的用户输入字段。
-   现有
    `team_mode`、`goal_mode`、`task_mode`、审批模式、联网开关和模型选择行为不变。
-   旧 Run 没有 `work_plan` 时，必须继续走原有恢复路径。
-   旧事件和旧会话历史仍可读取。
-   Planner 故障不能导致旧有简单任务不可用。

## 12. 与现有执行入口的集成要求

当前 `_drive()` 是主要集成点，但不要一次性重写整个方法。

建议按以下顺序：

1.  先把当前 DeepAgents、Document Graph、Team Graph、Team Project Graph
    的初始化和执行逻辑提取为 Adapter，保持原有输入输出和测试通过。
2.  让现有 Decision Architecture 输出的最终执行上下文交给 Work
    Orchestrator。
3.  `off` / `shadow` 模式保持原有执行路径。
4.  `auto` 模式下，简单任务走 Direct Adapter；复合任务走 Work
    Orchestrator Graph。
5.  统一收集步骤结果、产物、验证报告和最终文本。
6.  由原有 `_drive()` 负责 Run 状态提交、Session Outbox
    写回和最终事件，避免出现两套 Run 生命周期。

**禁止为了接入 Work Orchestrator 而复制一套
RuntimeContext、ToolPolicy、RunWorker 或 EventBus。**

## 13. 分阶段实施

### 阶段 0：基线审查与执行器解耦

-   复核最新代码，列出当前四类执行路线的真实入口、输入、输出、Checkpoint
    和异常处理。
-   梳理 `_drive()` 分支与 Run 收尾逻辑。
-   为现有执行路线建立 Adapter 接口。
-   不改变当前默认执行行为。
-   现有测试全部通过。

### 阶段 1：WorkPlan、Planner 和 Validator

-   建立 WorkPlan / WorkStep Schema。
-   建立复杂任务判断逻辑。
-   建立 WorkPlanner。
-   建立 DAG 依赖和能力校验。
-   建立 Executor Registry。
-   增加 `off / shadow / auto` 配置。
-   先在 shadow 模式验证计划质量，不改变真实执行路径。

### 阶段 2：单步骤执行接入

-   让 Work Orchestrator 可以通过 Adapter 调用现有 DeepAgents、Document
    Graph、Team Graph 和 Team Project Graph。
-   单一专业任务先以一个 Work Step 执行。
-   保持现有 Run、预算、审批、Checkpoint 和验证机制。
-   计划失败安全回退。
-   验证旧 API 和旧会话恢复兼容。

### 阶段 3：复合任务与 Durable Work Graph

-   支持多步骤 DAG。
-   支持依赖调度和前置结果引用。
-   每步持久化 Checkpoint 和执行结果。
-   支持失败步骤有限修复、跳过依赖和部分成果保留。
-   支持 Worker 重启后从未完成步骤恢复。
-   支持统一最终验收。
-   初期跨执行器步骤串行；Team Graph 内部并行继续使用。

### 阶段 4：对话工作台展示

-   增加 WorkPlanBlock 和 WorkPlanCard。
-   增加 work 事件协议映射及存储策略。
-   支持实时进度和历史回放。
-   展示计划、步骤、执行器、产物和验收结果。
-   不破坏原有 Todo、Team、Project、Verification、Document 卡片。

### 阶段 5：回归、评测与灰度

-   扩充决策和编排评测数据集。
-   以 shadow 模式采集真实任务的候选计划。
-   对比旧路线和新路线的任务完成率、耗时、模型调用量和交付质量。
-   通过后小范围启用 auto，再逐步扩大范围。

## 14. 必须覆盖的测试

### 14.1 任务分类与规划

1.  简单问答不创建 WorkPlan，不增加 Planner 模型调用。
2.  单一 Word 生成仍使用 Document Graph。
3.  单一软件项目仍遵守 `AGENT_AUTO_TEAM_ENABLED`。
4.  "先研究、再分析、最后生成 Word"生成三个有依赖的步骤。
5.  "查询数据库并生成报告"能正确绑定数据库步骤和文档步骤。
6.  明确开启 Team Mode 时保持原有团队执行语义。
7.  非复合任务不应被过度拆分。

### 14.2 Schema 与安全

1.  非法 executor profile 被拒绝。
2.  重复 ID、缺失依赖、循环依赖被拒绝。
3.  未授权能力不能进入执行计划。
4.  不存在的上下文引用被拒绝。
5.  模型不能通过计划扩大工具权限、修改审批模式或指定任意项目路径。
6.  关键歧义沿用现有澄清机制。

### 14.3 故障与恢复

1.  Planner 超时、非法 JSON 时安全回退。
2.  Worker 在计划生成后、步骤开始前重启，不重复生成计划。
3.  Worker 在步骤完成后、下一个步骤开始前重启，不重复已完成步骤。
4.  正在执行的步骤中断后从正确 Checkpoint 恢复。
5.  审批等待后恢复，不重复执行已批准的外部操作。
6.  `unknown / needs_reconciliation` 外部副作用不得自动重放。
7.  Run Lease 丢失后旧 Worker 不得继续提交结果。
8.  Run 预算跨步骤、跨重启累计，不被重置。
9.  取消后保留已经确认的产物。
10. 失败步骤不会清除其他已完成步骤。

### 14.4 前端与事件

1.  实时事件能正确更新 WorkPlanCard。
2.  断线重连和 Last-Event-ID 续传后状态一致。
3.  历史会话事件回放能重建完整计划。
4.  `plan.updated` 的 DeepAgents Todo 与 `work.plan.*` 不互相覆盖。
5.  计划事件被正确脱敏且不会被默认 400 字符截断。

### 14.5 重点端到端场景

至少跑通：

-   简单问答：Direct Path。
-   研究 → 分析 → Word：跨执行器复合任务。
-   数据库查询 → 数据分析 → 报告：数据能力组合。
-   显式 Team Mode：Team Graph。
-   软件项目：Team Project Graph + Sandbox 验收。
-   复合任务中途 Worker 重启：从已完成步骤恢复。
-   复合任务中途需要审批：暂停、审批、继续和最终交付。

建议新增测试文件：

``` text
backend/tests/test_work_plan_schema.py
backend/tests/test_work_planner.py
backend/tests/test_work_executor_registry.py
backend/tests/test_work_orchestrator_graph.py
backend/tests/test_work_orchestrator_recovery.py
backend/tests/test_work_orchestrator_security.py
frontend/tests/work-plan.test.mjs
```

并回归：

-   `test_decision_architecture.py`
-   `test_api_v2_runs.py`
-   `test_task_verification.py`
-   `test_team_graph.py`
-   `test_team_project_runtime.py`
-   `test_agent_runtime_durable.py`

## 15. 评测指标与验收标准

至少记录：

-   `work_plan_generation_latency`
-   `work_plan_invalid_rate`
-   `work_plan_fallback_rate`
-   `work_plan_step_count`
-   `work_step_completion_rate`
-   `work_plan_final_verification_pass_rate`
-   `work_plan_recovery_success_rate`
-   `work_plan_duplicate_execution_count`
-   `work_plan_model_call_overhead`

最终验收：

1.  简单任务没有明显额外延迟和额外模型调用。
2.  复合任务能形成可解释、可验证的步骤计划。
3.  计划中的每个步骤都映射到真实存在且有权限的执行器。
4.  不同专业执行引擎能在一个 Run 内按依赖顺序完成工作。
5.  中间产物能可靠传递，最终产物能真实下载或预览。
6.  Worker 重启、审批、取消和失败恢复不产生重复副作用。
7.  所有步骤和最终交付都有可追溯的验收证据。
8.  历史会话回放与实时进度一致。
9.  原有 Decision、Team、Document、Project、Sandbox、Tool Policy 和 Run
    API 回归通过。
10. `off` 模式能够完整恢复旧行为，`shadow` 不改变真实执行路线。

## 16. 本次明确不做的事情

-   不开发通用互联网 Browser Agent。
-   不开发独立 Deep Research 产品或全新 Research Harness；研究任务先复用
    DeepAgents + 现有搜索能力。
-   不开发 Scheduled Agent Tasks。
-   不开发实时语音、TTS、图像和视频生成。
-   不重写 DeepAgents Agent Loop。
-   不重写 Team Graph 和 Team Project Graph。
-   不把 Workflow Engine 改成 Work Orchestrator。
-   不新增第二套 Worker、队列、Lease 或 Run 状态机。
-   不引入不必要的分布式架构。
-   不在本次顺便重构全部 UI 或数据库。

## 17. 给执行 Agent 的最终要求

1.  **先审查再修改。** 开始前重新读取 `main`
    最新代码，确认本任务书引用的模块和接口仍然存在。
2.  先输出当前执行路线的调用链、模块复用表和改造影响面，再开始实施。
3.  每个阶段完成后运行对应单测和集成测试，不允许最后一次性堆积修改。
4.  保留旧执行路径，新增功能必须由配置控制。
5.  不允许用 Mock 成功代替真实 RunWorker、Checkpoint、EventBus、Tool
    Ledger 和 Sandbox 测试。
6.  不允许降低现有测试覆盖或删除旧测试来获得通过。
7.  不得把模型生成的计划直接当作可信执行指令；必须经过服务端
    Schema、能力、资源、ACL 和 ToolPolicy 校验。
8.  完成后提供：架构变化说明、关键调用链、配置说明、数据库迁移（如有）、测试报告、未完成项和回滚方式。

**最终交付不是"多了一个 Planner 类"，而是形成一个真正接入现有 Durable
Run
的工作任务编排层：能规划、能调度、能恢复、能验收，而且不会破坏原有执行体系。**
