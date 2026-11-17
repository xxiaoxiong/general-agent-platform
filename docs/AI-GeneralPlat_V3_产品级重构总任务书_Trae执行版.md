# AI-GeneralPlat V3 产品级重构总任务书

## Trae 执行版 · 产品、Agent Harness、Runtime、可观测性与工程结构全面重构

  -----------------------------------------------------------------------------
  项目                           内容
  ------------------------------ ----------------------------------------------
  版本                           V3.0 · 2026-11-13

  仓库                           https://github.com/xxiaoxiong/AI-GeneralPlat

  审查基线                       `main` ·
                                 `06405286ff61cb231c0eadf8045ae4ba56bed775`

  目标形态                       企业级 Agent 产品 + 模块化单体

  核心技术                       DeepAgents + LangGraph + LangChain +
                                 Langfuse + OpenTelemetry

  执行方式                       分阶段、分批次、可回滚、每批独立验证
  -----------------------------------------------------------------------------

------------------------------------------------------------------------

# 0. 给 Trae 的最高优先级指令

本任务不是在旧 Agent 模块上继续叠加功能，而是对 AI-GeneralPlat
进行产品级和架构级重构。

**最终目标：从"需要用户配置大量参数才能使用的 Agent
管理平台"，转变为"用户打开即可对话、能够自然语言创建助手、能够稳定完成复杂任务的智能体工作台"。**

必须遵循：

1.  先确认当前 HEAD、工作区状态、依赖、部署和数据库版本。本文基线是
    `06405286`；执行时若 HEAD 变化，必须重新比较。
2.  不以保留旧架构为前提。业务能力可保留，旧技术实现不合理时允许重写。
3.  不推翻 Durable Execution。Run Lease、Fencing、Tool
    Ledger、Checkpoint、HITL、事件持久化、Outbox 和恢复决策优先复用。
4.  不再建设第二套 Agent 内核，不新增自研 ReAct Loop、文本解析式 Tool
    Calling、平行 Checkpoint、平行 Memory 或平行 LLM Trace。
5.  DeepAgents 负责 Harness；LangGraph 负责图状态、Checkpoint
    和中断恢复；LangChain 负责模型、工具和
    Middleware；平台负责身份、资源、权限、Run 生命周期和企业级治理。
6.  迁移完成后删除旧 ReAct Engine 和 `AGENT_RUNTIME_ENABLED=false`
    的旧引擎回退路径。紧急回滚通过 Git/镜像版本回滚。
7.  不允许删除、放宽或跳过测试来制造通过结果；Mock
    测试不得描述成真实模型 E2E。
8.  不允许默认开放高危能力。外部副作用、数据库访问、代码执行和文件删除必须经过授权与安全策略。
9.  不把模型原始 Chain of Thought
    当作产品可观测性。用户看到的是任务进度、执行摘要、工具活动、结果和错误。
10. 不拆微服务，采用模块化单体。没有明确证据时不新增独立微服务。
11. 知识库、工作流、应用市场、模型管理等现有业务先保留，只调整它们与
    Agent 平台的集成边界。
12. 每阶段必须有代码、测试、迁移、文档和验收证据。每批独立提交。

## 0.1 Git 执行规则

-   从当前 `main` 创建 `refactor/agent-product-v3` 分支。
-   不直接在 `main` 上做大规模重构。
-   每批一个主题、一个提交。
-   数据库迁移必须可升级、可回滚；数据迁移先备份、演练、校验。
-   删除旧模块前必须完成全仓引用扫描和新实现回归。

------------------------------------------------------------------------

# 1. 现状与审查结论

## 1.1 审查基线

本次审查基于：

`06405286ff61cb231c0eadf8045ae4ba56bed775`

最新提交为"初步修复"，包含 `app_instances.workflow_id` 迁移修复、部分
Runtime 修复，以及新增 `test_deepagents_capabilities.py`。

执行时必须再次确认 HEAD，不得假定代码未变化。

## 1.2 已有能力：必须保留

当前项目并非完全没有 DeepAgents，也不是 Durable Execution
没有实现。已经具备：

-   DeepAgents `create_deep_agent`；
-   LangGraph Checkpointer；
-   PostgreSQL Checkpointer 多 Worker 共享；
-   Run Worker、Lease、Heartbeat、Fencing；
-   Tool Execution Ledger；
-   幂等请求和 Session 并发控制；
-   HITL 中断、审批、恢复和人工核对；
-   Run 级模型/工具/步数预算；
-   持久化事件、SSE 回放、`Last-Event-ID`；
-   Session Outbox；
-   工具副作用分类、SSRF、SQL 只读、路径检查和敏感信息脱敏；
-   DeepAgents 文件系统、Todo、SubAgent 和大工具输出卸载的部分测试。

这些是重构底座，不得为了目录变短而删除。

## 1.3 主要问题清单

  -------------------------------------------------------------------------------------------------------------
  ID        问题                       证据/表现                                                      优先级
  --------- -------------------------- -------------------------------------------------------------- ---------
  A01       产品入口仍是传统配置平台   AgentList                                                      P0
                                       要求手动配置模型、Prompt、工具、知识库、数据库、迭代数         

  A02       缺少默认助手               用户需要先创建 Agent 才能使用主要对话流程                      P0

  A03       业务工具无默认能力包       `_resolve_tool_specs()` 只加载 Agent 显式选择的工具，默认      P0
                                       `tools=[]`                                                     

  A04       原生工具权限未统一         DeepAgents 文件工具不经过 `ToolAdapterFactory` 的 `ToolPolicy` P0

  A05       execute 与实际 Sandbox     当前 Backend 不实现 Sandbox，测试确认 execute 返回不可用错误   P0
            不匹配                                                                                    

  A06       模型能力验证不足           UI 展示活动 Chat 模型，Tool Calling 主要依赖手工配置           P1

  A07       Memory 双轨                旧 SQLite MemoryManager 仍在 API 路径；LangGraph Store         P1
                                       未接入记忆文件路由                                             

  A08       Skills 只有硬编码示例      没有完整生命周期、版本和管理入口                               P1

  A09       SubAgent 以静态定义为主    researcher/data-analyst 固定定义，缺少统一能力目录             P1

  A10       Langfuse 未接入生产链路    telemetry.py 主要是 LoggingExporter，没有 CallbackHandler      P0

  A11       监控读旧模块               旧 Trace/Inference 统计不能代表新 Runtime                      P1

  A12       前端仍是旧 SSE 协议        LegacyEventAdapter 转成 thought/action/observation/final       P1

  A13       前端取消不等于后端取消     stopGeneration 只 AbortController.abort()                      P0

  A14       前端没有完整断线恢复       API 支持 Run ID 和事件回放，但聊天页没有完整使用               P1

  A15       SSE 错误被静默忽略         JSON 解析异常被 catch 后忽略                                   P1

  A16       Runtime Service 过大       service.py 约 2077 行，混合执行、审批、恢复、事件、预算        P1

  A17       Agent API 职责混杂         agents.py 约 972 行，包含 CRUD、聊天、记忆、工作流、审批、监控 P1

  A18       前端聊天组件过大           AgentChat.vue 约 1143 行                                       P1

  A19       旧引擎仍有调用             AgentEngine、MemoryManager、InferenceQueue、ModelManager、旧   P1
                                       Trace                                                          

  A20       测试基线不统一             报告 301 passed、提交记录出现 303 passed，最新 HEAD 又新增测试 P0

  A21       UI/性能验证不足            UI 点击级、长稳、性能基准未完成                                P1

  A22       Agent 列表用户隔离缺口     列表查询未按 owner_id 过滤，详情接口却有所有权校验             P0

  A23       异常响应可能泄露内部信息   全局异常处理把 `str(exc)` 返回客户端                           P0

  A24       最新迁移需验证             `e8f9a0b1c2d3` 增加 app_instances.workflow_id，必须纳入 MySQL  P0
                                       迁移测试                                                       
  -------------------------------------------------------------------------------------------------------------

## 1.4 正确的框架职责

DeepAgents 的 `create_deep_agent()` 是 Harness 组装入口，本身返回由
LangGraph 驱动的可执行图。

``` text
平台 Assistant / Agent Definition
              |
              v
        DeepAgents Harness
   Planning / Todo / Filesystem
   Skills / SubAgents / Middleware
              |
              v
          LangGraph
   State / Checkpoint / Interrupt
   Resume / Streaming / Graph State
              |
              v
          LangChain
   ChatModel / Tools / Middleware
   Callbacks / Retrievers
```

平台自己的 Run Worker 和 Durable Execution
是企业平台层治理，不是重复实现框架：

-   Run 创建、幂等、用户和资源授权；
-   Worker Claim、Lease、Heartbeat、Fencing；
-   工具副作用账本；
-   Run 状态和预算；
-   事件持久化和 SSE；
-   业务 Session 写回；
-   恢复决策和人工核对。

**禁止新增自研 Agent Loop，也禁止把这些职责重新塞回一个
AgentRuntimeService。**

------------------------------------------------------------------------

# 2. 产品定位与目标体验

## 2.1 新产品定位

从"模型、工具、知识库、工作流和 Agent 的配置管理平台"，转变为：

> 以智能体为核心交互入口，能够直接对话、自然语言创建助手、自主调用能力、执行复杂任务、持续展示进度并可靠交付结果的企业级
> Agent 产品。

对标
ChatGPT、豆包的是交互范式和使用门槛，不承诺复制闭源产品的模型能力或内部实现。

## 2.2 产品入口

用户登录后直接进入统一智能体工作台，不要求先创建 Agent：

1.  平台自动提供默认通用助手；
2.  用户直接开始对话；
3.  支持文件上传、提问、研究、分析和生成内容；
4.  系统根据任务和权限选择能力；
5.  复杂任务展示任务进度；
6.  完成后展示结果和文件；
7.  常用任务可以保存为专用助手。

Agent 管理成为可选能力，不再是使用平台的前置步骤。

## 2.3 导航建议

普通用户工作区：

-   新建对话；
-   最近对话；
-   我的智能助手；
-   文件与任务成果；
-   知识空间。

管理员管理中心：

-   模型供应商与模型；
-   默认模型策略；
-   能力包与工具权限；
-   Skills 管理；
-   数据源和知识库；
-   Agent 运行监控；
-   Langfuse Trace；
-   用户、审计和系统配置。

工作流、应用市场等模块先保留，不在本次强行推翻整个业务系统。

## 2.4 自然语言创建助手

用户只需描述：

> 帮我创建一个行业研究助手，能够搜索最新资料、分析数据、核实信息来源，并生成结构化研究报告。

系统自动完成名称、描述、角色指令、能力包、模型策略、Skills、SubAgent、资源授权、风险策略、预算和基础测试。

创建流程支持：

-   自动生成草稿；
-   用户预览和确认；
-   一键创建并立即使用；
-   创建后通过自然语言调整；
-   高级配置折叠在设置区域。

普通用户不得被强制要求填写 System Prompt、Temperature、Max
Iterations、Tool List 等底层参数。

模型选择默认由平台完成。管理员配置默认模型和候选模型；用户可在高级设置中调整偏好，但不能绕过管理员策略。

## 2.5 能力包

  -----------------------------------------------------------------------------------
  能力包             包含能力                              默认策略
  ------------------ ------------------------------------- --------------------------
  通用助手           对话、计算、时间、基础文件处理        默认启用

  网络研究           搜索、网页读取、来源整理、事实核验    受管理员网络策略控制

  文档处理           PDF/Word/Excel/PPT 读取、提取、生成   限制在安全文件范围

  数据分析           数据库只读查询、计算、统计、图表      必须显式绑定数据源

  知识助手           知识库检索、引用、知识整理            必须绑定授权知识库

  编程开发           文件操作、代码分析、测试              文件隔离；代码执行必须有
                                                           Sandbox

  业务自动化         HTTP、邮件、Webhook、外部业务写操作   默认关闭，逐项授权并 HITL
  -----------------------------------------------------------------------------------

能力包是平台领域抽象，不是新的 Agent Loop。内部仍映射到 DeepAgents
Tools、Skills、SubAgents 和 Middleware。

------------------------------------------------------------------------

# 3. 目标架构

## 3.1 总体架构

``` mermaid
flowchart TB
    U[用户工作台 / 管理后台] --> API[FastAPI API v2]
    API --> APP[Assistant / Conversation / Run Services]
    APP --> RUN[Run Orchestrator]
    RUN --> WORKER[Durable Run Worker]
    WORKER --> EXEC[DeepAgents Executor]
    EXEC --> GRAPH[LangGraph Graph]
    GRAPH --> MODEL[LangChain Model Adapter]
    GRAPH --> TOOLS[LangChain Tools / Middleware]
    MODEL --> GW[LiteLLM / vLLM / OpenAI Compatible]
    TOOLS --> RES[Knowledge / Database / Web / Files / Sandbox]
    RUN --> DB[(MySQL Business DB)]
    WORKER --> CP[(PostgreSQL Checkpointer)]
    GRAPH --> STORE[(LangGraph Store)]
    RUN --> EVENT[(Run Events / Tool Ledger / Outbox)]
    EXEC --> LF[Langfuse]
    WORKER --> OTEL[OpenTelemetry]
    API --> OTEL
    RES --> OTEL
    OTEL --> PROM[Prometheus / Grafana / Loki]
```

## 3.2 模块职责

  ------------------------------------------------------------------------------
  模块               职责                                     禁止承担
  ------------------ ---------------------------------------- ------------------
  API                参数校验、身份认证、协议转换             直接组装 Agent

  Assistant Domain   定义、版本、能力绑定、资源引用           执行模型调用

  Application        创建助手、对话、发布、Run 用例           自己实现 Agent
  Services                                                    Loop

  Run Orchestrator   Run 生命周期、幂等、预算、调度           重新实现
                                                              Checkpoint

  Durable Worker     Claim、Lease、Heartbeat、Fencing、恢复   承载全部业务逻辑

  DeepAgents         组装 Harness、调用 Agent                 自研规划循环
  Executor                                                    

  LangGraph          Graph                                    替代平台业务 DB
                     State、Checkpoint、Interrupt、Resume     

  LangChain          Model、Tool、Middleware、Callback        平行 Tool Runtime

  Capability         能力定义、授权映射                       直接执行工具
  Registry                                                    

  Model Router       模型能力检查、选模、客户端管理           重建 LiteLLM

  Observability      Langfuse/OTel 初始化、关联和脱敏         作为业务事实库

  Repositories       DB 持久化和事务                          执行 Agent
  ------------------------------------------------------------------------------

## 3.3 数据事实来源

  数据                                唯一事实来源
  ----------------------------------- -------------------------------------
  Agent 定义和版本                    MySQL
  Conversation / Session / 用户消息   MySQL
  Run 状态、租约、预算                MySQL
  LangGraph 当前执行状态              PostgreSQL Checkpointer
  跨会话长期记忆                      LangGraph Store / DeepAgents Memory
  当前任务工作文件                    DeepAgents Backend / 文件存储
  工具副作用结果                      Tool Execution Ledger
  Run 事件时间线                      `agent_run_events`
  最终消息写回                        Session / Outbox
  LLM Trace 和 Agent Span             Langfuse
  服务和基础设施 Trace                OpenTelemetry
  系统指标                            Prometheus

不得让同一份事实同时由两套独立系统维护。

## 3.4 部署取舍

保留当前主部署：

-   MySQL：业务数据库；
-   PostgreSQL：LangGraph Checkpointer，必要时承载 Store；
-   Redis：缓存和通知优化，不作为 Run、Tool Ledger、Checkpoint
    事实来源；
-   LiteLLM：可作为现有模型网关；
-   Langfuse：独立部署或连接已有服务；
-   Prometheus/Grafana/Loki/Alloy：基础设施监控。

不新增微服务，不把 LangGraph Server 作为必须依赖，不重造模型代理服务。

------------------------------------------------------------------------

# 4. Agent Domain 与数据模型

## 4.1 Agent Definition / Version

当前 Agent 配置直接可变，Run 需要明确绑定不可变版本。

新增：

-   `AgentDefinition`：逻辑助手身份；
-   `AgentVersion`：不可变发布配置；
-   `AgentRun`：关联具体 `agent_version_id`。

版本内容至少包括：

-   system instructions；
-   model policy；
-   capability IDs；
-   toolset version；
-   skill references；
-   subagent profiles；
-   resource bindings；
-   memory policy；
-   execution budget；
-   security policy；
-   version number；
-   created_by / published_at。

发布新配置生成新版本。运行中的 Run
必须使用创建时绑定的版本。初期可以保留数据库 `agents`
表名，避免无必要的破坏性重命名。

## 4.2 Capability Registry

第一阶段采用代码注册表 + 配置化定义，不先建设复杂市场。

核心接口：

-   `list_capabilities(scope, user_context)`；
-   `resolve_capabilities(capability_ids, resource_bindings)`；
-   `validate_capability_policy(...)`；
-   `build_toolset(...)`；
-   `build_skill_sources(...)`；
-   `build_subagent_profiles(...)`。

每个 Capability 声明 ID、版本、描述、关联
Tools/Skills/SubAgents、依赖资源、风险等级、所需权限、HITL、重试策略和模型能力要求。

模型可以建议需要什么能力，但不能自行授权。能力最终必须经过注册表和用户权限校验。

## 4.3 默认工具策略

默认助手开箱即用不代表所有工具无条件开放：

-   计算、时间：默认允许；
-   文件读写：仅用户隔离工作区；
-   网络搜索：受管理员 Egress 策略控制；
-   知识检索：仅访问授权知识库；
-   数据库查询：必须绑定明确数据源，默认只读；
-   文件删除：按风险限制或审批；
-   HTTP POST、邮件、Webhook：默认 HITL；
-   Shell/Python：没有外部 Sandbox 时不提供。

必须解决 DeepAgents 原生工具绕过平台 ToolAdapter 的问题：

1.  配置原生 `FilesystemPermission`；
2.  使用 allow/deny/interrupt 规则；
3.  文件操作限制在当前用户、Agent、Session 隔离目录；
4.  `delete` 等危险操作需要权限和审批；
5.  无 Sandbox 时使用 Harness Profile `excluded_tools` 或受控 Middleware
    移除 `execute`；
6.  不得因为风险登记表有工具名，就宣称原生工具已受该表保护。

## 4.4 Model Router

当前模型能力主要依赖手工
`supports_function_call`，生产路径没有完整真实探测。

模型能力至少维护：

-   tool_calling；
-   streaming；
-   structured_output；
-   vision；
-   input modalities；
-   context_length；
-   max_output_tokens；
-   parallel_tool_calls；
-   provider/model identity；
-   verification status；
-   last_verified_at；
-   configuration_version。

模型配置测试必须使用无副作用工具执行真实 Tool Calling
探测。`bind_tools()` 成功只证明 SDK
能绑定工具，不足以证明模型服务端真正返回原生工具调用。

模型选择要求：

-   管理员配置默认模型；
-   按能力包需求过滤模型；
-   根据可用性、上下文、成本、延迟和质量选择；
-   备用模型必须满足相同能力约束；
-   不得静默切换到不支持 Tool Calling 的模型。

建立 `ModelClientFactory`，统一 HTTP
客户端生命周期和连接复用。模型网关处理供应商级路由和限流；平台负责用户/Agent/Run
并发和预算。不要让旧 InferenceQueue 和新 Runtime 双重排队。

Graph 是否缓存必须等工具无状态化和性能基准建立后再决定。不得缓存绑定 Run
Context、DB Session、EventBus 的闭包。

## 4.5 Memory 重构

采用四层：

-   短期记忆：LangGraph Checkpointer，thread/session 级；
-   长期记忆：LangGraph Store + DeepAgents MemoryMiddleware，采用
    `AGENTS.md`；
-   工作记忆：DeepAgents StateBackend，保存当前任务临时文件和摘要；
-   企业知识：保留现有知识库/RAG，不把全部企业文档塞入长期记忆。

旧 MemoryManager 迁移步骤：

1.  盘点 SQLite 记忆类型和字段；
2.  导出
    user_id、agent_id、memory_type、content、importance、tags、时间和来源；
3.  设计 Store namespace；
4.  只读导入预演；
5.  比对数量、内容摘要和用户归属；
6.  新系统检索测试通过后切换；
7.  旧 SQLite 保留备份观察期；
8.  删除旧 API 和 MemoryManager。

不得直接删除 `memory_v2.db`。

## 4.6 Skills 与 SubAgents

Skills：

-   使用 DeepAgents 原生 Skills Middleware；
-   统一 `/skills/` 来源和 `SKILL.md`；
-   支持内置、用户、Agent、组织范围；
-   版本、启用状态、来源、权限和审计；
-   渐进式加载；
-   Skill 不得自行提升工具权限。

SubAgents：

-   使用 DeepAgents 原生 `task` 委派；
-   把 researcher、data-analyst 迁移为可注册 Profile；
-   每个 SubAgent 有工具白名单、模型策略、Skills、预算和最大深度；
-   父子执行有统一 Trace/Run 关联；
-   仅复杂任务启用多代理；
-   不允许模型运行时创建未登记工具或 SubAgent。

------------------------------------------------------------------------

# 5. 可观测性：Langfuse + OpenTelemetry

本项目此前所说的"Langchain Fusion"按 **Langfuse** 理解。

## 5.1 当前问题

当前 `agent_runtime/telemetry.py` 主要是 LoggingExporter，不是完整 Trace
系统。`AgentRuntimeService._drive()` 主要附加
`ModelUsageCallback`，没有真正接入 Langfuse CallbackHandler。

## 5.2 Langfuse 接入

采用当前 Langfuse Python SDK v5 方向，先锁定与 Python、LangChain Core
和自托管服务端兼容的版本。

要求：

1.  初始化 Langfuse Client；
2.  初始化 LangChain CallbackHandler；
3.  使用 `propagate_attributes()` 传递 trace_name、user、session、tags
    和 metadata；
4.  Graph 调用时合并 Langfuse CallbackHandler 与
    `ModelUsageCallback`，不得覆盖已有 callbacks；
5.  捕获主 Agent、Model、Tool、Retriever、SubAgent 和 Middleware
    的嵌套执行；
6.  手动增加平台
    Run、排队、Claim、Lease、Retry、Approval、Recovery、Session
    Writeback 等 Span；
7.  将 Run ID、Session ID、Agent ID、Agent Version 和模型版本关联到
    Trace；
8.  平台 Run 详情可以跳转 Langfuse Trace。

Run 可能在另一个 Worker/进程执行，不能只依赖 API 请求中的
ContextVar。必须持久化必要 Trace Context，并在 Worker
执行时恢复或建立关联。

## 5.3 Trace 结构

``` text
Trace: user request
 └── Platform Run
      ├── queue_wait
      ├── worker_claim
      ├── agent_initialization
      ├── Agent Graph
      │    ├── planning
      │    ├── Model Invocation
      │    ├── Tool Call / Result
      │    ├── SubAgent
      │    ├── context_summarization
      │    └── verification
      ├── retry / recovery
      ├── HITL wait / resume
      ├── artifact generation
      └── session writeback
```

## 5.4 OpenTelemetry

接入
FastAPI、SQLAlchemy、HTTPX、Redis、Worker、模型请求和外部工具请求。使用
W3C Trace Context，保持 API、Worker、模型和工具之间的调用关联。

## 5.5 隐私与性能

-   默认不采集完整用户输入、完整模型输出和文件内容；
-   API Key、Token、数据库密码、Cookie 和个人敏感信息必须脱敏；
-   大文件和大工具输出不写入 Trace；
-   Trace 导出失败不能导致 Agent 执行失败；
-   使用异步、批量、有界缓冲；
-   配置采样、保留期、清理和访问权限；
-   启用 Langfuse 后不得显著增加主要执行延迟。

## 5.6 Prometheus/Grafana 指标

至少新增：

-   `agent_runs_total{status,agent_type}`；
-   `agent_run_duration_seconds`；
-   `agent_queue_wait_seconds`；
-   `agent_active_runs`；
-   `agent_run_retries_total{reason}`；
-   `agent_recovery_total{decision}`；
-   `agent_tool_calls_total{tool,status}`；
-   `agent_tool_duration_seconds{tool}`；
-   `agent_model_calls_total{provider,model,status}`；
-   `agent_model_latency_seconds{provider,model}`；
-   `agent_tokens_total{provider,model,direction}`；
-   `agent_sse_reconnect_total`；
-   `agent_event_lag_seconds`；
-   `agent_checkpoint_write_seconds`；
-   `agent_lease_lost_total`。

Grafana 至少提供 Run
总览、模型性能、工具成功率、Worker/队列、错误恢复、Token/成本、DB/Checkpoint、事件流与
SSE 八类面板。

------------------------------------------------------------------------

# 6. Run Runtime 重构

## 6.1 目标目录

``` text
backend/app/
├── api/
│   ├── v1/                         # 迁移期兼容
│   └── v2/
│       ├── workspace.py
│       ├── assistants.py
│       ├── conversations.py
│       ├── runs.py
│       ├── capabilities.py
│       └── admin/
├── assistant/
│   ├── domain/
│   ├── application/
│   └── infrastructure/
├── runtime/
│   ├── harness/deepagents_executor.py
│   ├── models/
│   ├── tools/
│   ├── skills/
│   ├── subagents/
│   ├── memory/
│   ├── execution/
│   │   ├── run_orchestrator.py
│   │   ├── worker.py
│   │   ├── lifecycle.py
│   │   ├── recovery.py
│   │   ├── retry.py
│   │   ├── budget.py
│   │   └── artifacts.py
│   ├── events/
│   └── persistence/
├── observability/
├── integrations/
└── core/
```

不要求一次性机械搬迁所有文件，但新代码必须按职责落位。

## 6.2 AgentRuntimeService 拆分

  新模块                    职责
  ------------------------- -----------------------------------------
  RunApplicationService     Run 创建、幂等、用户输入和 Session 协调
  RunOrchestrator           Run 生命周期和 Worker 调度
  DeepAgentsExecutor        构造和调用 DeepAgents Graph
  RunEventService           事件发布、持久化、回放和 SSE
  ApprovalService           HITL 审批、决策校验和恢复
  RecoveryService           Checkpoint 检查、恢复决策、人工核对
  BudgetService             预算加载、累计、超时和限制
  SessionWritebackService   Outbox 和消息写回
  ModelRouter               模型选择、能力检查、客户端复用

拆分原则：

-   不制造只有几行代码的无意义抽象；
-   每个模块有明确业务职责；
-   Repository 只负责持久化；
-   API 不直接调用底层 Graph；
-   Runtime 不依赖 Vue 或 HTTP Response；
-   关键状态变化有明确事务边界。

## 6.3 Durable Execution 保留清单

优先保留并整理：

-   `worker.py`；
-   `lifecycle.py`；
-   `persistence.py`；
-   `repositories/run_repository.py`；
-   `repositories/tool_execution_repository.py`；
-   `repositories/event_repository.py`；
-   `repositories/session_repository.py`；
-   `events.py` 的持久化 EventBus；
-   ToolResult 和 SideEffectContract；
-   Run 预算和恢复决策。

保留的是已验证的不变式，不是原文件形态。

## 6.4 Retry / Recovery

模型错误：

  错误                  处理
  --------------------- -----------------------------------
  429/限流              退避重试，遵守 Retry-After 和预算
  临时 5xx              有上限重试
  网络超时              判断是否可安全重试
  供应商不可用          满足能力约束时切换备用模型
  不支持 Tool Calling   明确失败，不回退文本解析
  上下文超限            压缩/卸载后重试

工具错误：

  工具类别                    处理
  --------------------------- -------------------------------
  只读工具                    允许安全重试
  下游明确支持幂等            使用同一 Idempotency-Key 重试
  非幂等工具                  结果未知时禁止自动重放
  参数错误                    校验并允许模型受控修正
  权限拒绝                    明确拒绝，不允许绕过
  安全策略拒绝                立即阻断，不自动放宽
  Ledger/Fencing/持久化失败   停止 Graph 推进

可以使用 LangChain Middleware
做模型重试和工具错误处理，但平台必须保留副作用等级和 Tool Ledger
的最终控制权。不得无差别重放非幂等工具。

## 6.5 LangGraph 持久化

-   Checkpointer：thread 级 Graph State；
-   Store：跨 thread 长期数据；
-   Run DB：平台 Run 生命周期；
-   Tool Ledger：副作用结果；
-   Event DB：时间线；
-   不重复维护同一事实。

根据任务风险明确 Graph durability
mode。高风险副作用必须有明确持久化边界；不得仅凭 Checkpoint
宣称外部副作用 exactly-once。

## 6.6 Cancel / Pause / Resume

-   浏览器断开不等于取消 Run；
-   用户取消必须调用后端 Cancel API；
-   首次 SSE 响应后保存 Run ID；
-   SSE 通过 `Last-Event-ID` 或 `after_seq` 恢复；
-   事件回放以数据库为事实来源；
-   取消意图先记录，Worker 确认后进入终态；
-   在途外部工具结果不确定时，不得伪造取消成功；
-   HITL 决策必须携带稳定 `action_id`；
-   Checkpoint 丢失或不可读时不得自动重新推理执行副作用。

------------------------------------------------------------------------

# 7. 新 API 契约

新增 `/api/v2`，迁移期间保留 v1 兼容适配。

  API                                     方法        作用
  --------------------------------------- ----------- --------------------------------
  `/api/v2/workspace/bootstrap`           GET         默认助手、用户能力和工作区配置
  `/api/v2/assistants`                    GET/POST    助手列表和创建
  `/api/v2/assistants/draft`              POST        自然语言生成草稿
  `/api/v2/assistants/{id}`               GET/PATCH   查询和修改
  `/api/v2/assistants/{id}/publish`       POST        发布版本
  `/api/v2/conversations`                 GET/POST    会话列表和创建
  `/api/v2/conversations/{id}/messages`   GET         消息历史
  `/api/v2/runs`                          POST        创建 Run
  `/api/v2/runs/{id}`                     GET         Run 状态
  `/api/v2/runs/{id}/events`              GET         SSE 订阅和回放
  `/api/v2/runs/{id}/cancel`              POST        取消 Run
  `/api/v2/runs/{id}/resume`              POST        审批后恢复
  `/api/v2/capabilities`                  GET         可用能力包
  `/api/v2/artifacts/{id}`                GET         安全下载产物
  `/api/v2/admin/observability`           GET         监控概览

## 7.1 新事件协议

内部不再使用 `thought/action/observation/final` 作为主协议。

``` json
{
  "event": "tool.started",
  "run_id": "run-id",
  "sequence": 12,
  "timestamp": "2026-11-13T12:00:00Z",
  "trace_id": "trace-id",
  "data": {
    "tool_name": "web_search",
    "display_name": "网络搜索",
    "summary": "正在搜索行业资料"
  }
}
```

事件类型至少包括：

-   `run.started`
-   `run.status_changed`
-   `plan.updated`
-   `assistant.delta`
-   `tool.started`
-   `tool.completed`
-   `tool.failed`
-   `subagent.started`
-   `subagent.completed`
-   `approval.required`
-   `run.retrying`
-   `run.recovering`
-   `verification.started`
-   `verification.completed`
-   `artifact.created`
-   `run.completed`
-   `run.failed`
-   `run.cancelling`
-   `run.cancelled`
-   `run.suspended`

每个事件有稳定序号和统一字段。SSE 使用标准 `id:`、`event:`、`data:`
行，持久化仍由 EventRepository/EventBus 承担。

------------------------------------------------------------------------

# 8. 前端产品级重构

## 8.1 目标组件

``` text
features/
├── workspace/
│   ├── WorkspaceHome.vue
│   ├── ConversationSidebar.vue
│   └── DefaultAssistant.vue
├── assistant/
│   ├── AssistantList.vue
│   ├── AssistantBuilder.vue
│   ├── AssistantDetail.vue
│   └── AssistantSettings.vue
├── conversation/
│   ├── ConversationPage.vue
│   ├── ChatComposer.vue
│   ├── MessageList.vue
│   └── MessageItem.vue
├── run/
│   ├── RunTimeline.vue
│   ├── RunStatus.vue
│   ├── ToolActivity.vue
│   ├── SubAgentActivity.vue
│   ├── ApprovalPanel.vue
│   └── ArtifactPanel.vue
└── admin/
    ├── ModelManagement.vue
    ├── CapabilityManagement.vue
    └── ObservabilityDashboard.vue
```

不要求推翻 Vue 3 + Element Plus，优先重构页面职责、状态管理和 API 契约。

## 8.2 对话体验

-   登录后立即可输入；
-   默认助手无需配置；
-   输入框支持文件上传；
-   支持取消、审批和恢复；
-   活动区默认简洁，可展开详情；
-   最终答案和生成文件独立展示；
-   支持多轮会话；
-   切换 Session 不串状态；
-   断线重连恢复任务；
-   失败时展示原因和下一步操作。

## 8.3 Run Timeline

普通用户看到计划、工具活动、审批、验证、产物和状态。

开发者展开后看到模型调用、工具脱敏参数、工具耗时、SubAgent、Token、Retry、Checkpoint、Recovery
和 Trace 链接。

不得展示模型原始隐式推理文本。

## 8.4 取消与恢复

1.  保存 `X-Run-Id`；
2.  停止订阅与取消 Run 分离；
3.  用户点击停止时调用 `/runs/{id}/cancel`；
4.  断线时保留 Run ID 和最后事件序号；
5.  页面恢复时查询 Run 状态；
6.  未终结 Run 重新订阅事件；
7.  JSON 解析失败记录诊断信息；
8.  审批决策携带 `action_id`；
9.  切换 Session 后旧 Run 事件不能写入新 Session。

------------------------------------------------------------------------

# 9. Task Contract、验证与结果质量

准确度不能只靠换模型或增加 System Prompt。

复杂任务必须建立可验证的完成条件：

``` text
用户目标
   |
   v
结构化任务目标与完成条件
   |
   v
DeepAgents 规划和执行
   |
   v
收集工具结果和证据
   |
   v
Verification
   |
   +---- 通过 ----> 最终交付
   |
   +---- 部分通过 -> 补充执行
   |
   +---- 不通过 --> 修复 / 重试 / 请求用户
```

按任务类型选择验证器，不要每个简单问题都额外调用一次模型。

  任务           验证方式
  -------------- ----------------------------------------
  普通知识问答   不确定性表达，必要时引用来源
  网络研究       来源 URL、关键事实交叉验证、引用完整性
  文档生成       文件存在、格式可打开、结构校验
  Excel 分析     公式/计算复核、数字一致性
  数据库分析     SQL 安全、结果行数和统计校验
  代码开发       Sandbox 中测试、构建和静态检查
  业务操作       审批记录、外部 Operation ID、结果核验

验证失败必须产生结构化事件，不允许模型说"已完成"就通过。

------------------------------------------------------------------------

# 10. 评测体系

## 10.1 固定 Benchmark

至少建立：

1.  简单问答；
2.  多轮连续对话；
3.  网络研究；
4.  文件读取和分析；
5.  大 Excel 分析；
6.  PDF/Word 提取；
7.  知识库检索和引用；
8.  数据库只读查询；
9.  多工具串联；
10. SubAgent 委派；
11. Skill 加载；
12. 大上下文卸载；
13. 模型超时和限流；
14. 工具失败与重试；
15. Worker 崩溃恢复；
16. HITL 审批与恢复；
17. 用户取消；
18. SSE 断线恢复；
19. 多用户隔离；
20. 复杂报告生成。

每个任务定义输入、预期行为、完成条件、允许能力、禁止操作、验收器和评分。

## 10.2 指标

-   Task Completion Rate；
-   Answer Correctness；
-   Evidence/Citation Accuracy；
-   Tool Selection Accuracy；
-   Tool Success Rate；
-   Retry Recovery Rate；
-   Human Intervention Rate；
-   P50/P95 Latency；
-   First Token Latency；
-   Model Calls；
-   Input/Output Tokens；
-   Cost per Task；
-   Duplicate Side Effect Count。

## 10.3 Langfuse Evaluation

-   固定任务集整理为 Dataset；
-   不同 AgentVersion、Prompt、模型和能力包分别执行实验；
-   保存分数和失败案例；
-   支持人工反馈和 LLM-as-a-Judge；
-   LLM Judge 不能替代确定性测试；
-   生产 Trace 采样进入评测集前必须脱敏。

------------------------------------------------------------------------

# 11. 旧代码迁移与删除清单

## 11.1 迁移后删除的旧执行内核

全仓无有效引用后删除：

-   `services/agent/engine.py`
-   `services/agent/output_parser.py`
-   `services/agent/prompt_builder.py`
-   `services/agent/context_manager.py`
-   `services/agent/hallucination.py`
-   `services/agent/tool_executor.py`
-   `services/agent/checkpoint.py`
-   `services/agent/trace.py`

对应能力由 DeepAgents、LangGraph、LangChain Middleware、Task
Verification、Tool Adapter 和 Langfuse/OTel 承担。

## 11.2 迁移后删除

  ------------------------------------------------------------------------
  模块                              前置条件
  --------------------------------- --------------------------------------
  `agent/memory_manager.py`         SQLite 记忆完成导出、导入、对账，Store
                                    运行通过

  `agent_service.py`                所有 API/Workflow 调用方已切换

  `inference/model_manager.py`      ModelClientFactory 覆盖实际调用

  `inference/queue.py`              网关和平台并发治理接管，旧监控无调用

  `agent_runtime/telemetry.py`      Langfuse/OTel 和结构化日志完成迁移

  `LegacyEventAdapter`              前端和客户端切换 v2 事件

  v1 Agent 旧路由                   所有调用方完成迁移
  ------------------------------------------------------------------------

## 11.3 必须保留或重构

-   `services/tools/*`：业务工具 Handler 迁移为统一 Tool Registry；
-   `services/tools/result.py`：保留 ToolResult；
-   `agent_runtime/worker.py`：保留并整理；
-   `agent_runtime/lifecycle.py`：保留状态机；
-   `agent_runtime/persistence.py`：保留 Checkpointer/Store 生命周期；
-   `agent_runtime/repositories/*`：保留数据库一致性；
-   `agent_runtime/events.py`：保留事件持久化，移除旧协议适配后继续使用；
-   `agent_runtime/tools/classification.py`：保留副作用和恢复分类；
-   `agent_runtime/tools/policy.py`：保留并扩展为统一能力策略；
-   `agent_runtime/tools/adapter.py`：保留并重构为统一业务工具 Adapter。

## 11.4 旧开关和监控

-   移除永久双引擎开关 `AGENT_RUNTIME_ENABLED`；
-   删除旧 `AgentEngine` 回退分支；
-   清理 main.py 对旧 CheckpointManager 的清理；
-   清理旧 ModelManager 的关闭和热重载；
-   重写旧 `/monitor/performance` 和 `/monitor/inference`；
-   不允许旧 API 读取旧 Trace/InferenceQueue 却展示为新 Runtime 监控。

删除前执行全仓搜索，检查 Python、Vue、测试、脚本、Workflow
Node、README、部署文件和迁移脚本。

------------------------------------------------------------------------

# 12. 分阶段实施计划

## Phase 0：基线、诊断、安全和重构准备

**目标：** 建立可信状态，修复明确高风险问题，避免在错误基线上重构。

### P0-01 仓库和运行基线

记录 HEAD、工作区、依赖、Docker 配置、MySQL/PostgreSQL/Redis
健康、Alembic head、后端测试、前端 build，保存回滚镜像和备份说明。

### P0-02 代码依赖地图

输出真实 Agent 调用链、API 到 DeepAgents/LangGraph
的图、旧引擎引用地图、Memory/Model/Trace/Checkpoint
双轨地图、前端页面/API 依赖图和删除依赖清单。

### P0-03 用户隔离和异常泄露

-   Agent 列表、计数、分页按 owner/visibility 过滤；
-   公开助手必须有明确公开策略；
-   Session、Run、Artifact、Memory 全部做用户授权；
-   全局异常响应只返回安全错误码和 request_id；
-   详细异常只进入服务端日志。

### P0-04 部署安全

-   Docker Compose 不得保留默认生产密码；
-   SECRET_KEY、数据库密码、管理员初始密码通过环境变量或 Secret 注入；
-   首次生产启动强制修改管理员凭据；
-   检查 CORS、API Key 脱敏、端口暴露、上传路径和下载权限。

### P0-05 DeepAgents 原生工具安全

盘点内置工具；配置 FilesystemPermission；统一 read/write/delete 授权；无
Sandbox 时隐藏 execute；测试用户文件隔离、路径穿越、符号链接和权限拒绝。

### P0-06 模型能力和运行错误基线

接通真实 Tool Calling 探测；不兼容模型在发布前报错；对每个模型执行
Streaming、Tool Calling、超时和错误测试；收集真实运行日志并分类。

### P0-07 测试与迁移报告

-   检查 `e8f9a0b1c2d3`；
-   在 MySQL 8 真实执行迁移和 downgrade；
-   纳入 `test_deepagents_capabilities.py`；
-   重新执行全量测试；
-   统一测试报告和提交记录数字；
-   不得引用旧报告数字作为新 HEAD 验证结果。

**Phase 0 验收：**
基线可信、权限问题修复、迁移真实验证、测试报告更新、输出架构依赖地图和错误清单，不进行大规模删除。

## Phase 1：可观测性

-   P1-01 Langfuse SDK、CallbackHandler、propagate_attributes；
-   P1-02 OTel 接入 FastAPI、SQLAlchemy、HTTPX、Redis、Worker；
-   P1-03 平台
    Span：排队、Claim、Lease、Graph、Model、Tool、Approval、Retry、Recovery、Checkpoint、Session
    Writeback；
-   P1-04 脱敏、异步导出、有界缓冲、采样和保留；
-   P1-05 Grafana 面板。

**验收：** 一次真实 Run 在 Langfuse 中能看到平台
Run、Graph、Model、Tool、SubAgent、Token、延迟和错误；Grafana 能观察
Worker、队列、模型和工具。

## Phase 2：Agent Domain、Model Router、Capability 和 Memory

-   P2-01 Agent Definition / Version；
-   P2-02 Capability Registry；
-   P2-03 Model Router 和真实模型能力探测；
-   P2-04 StoreBackend、MemoryMiddleware 和旧 SQLite 记忆迁移；
-   P2-05 Skills Registry；
-   P2-06 SubAgent Registry。

**验收：** Agent 使用能力包；模型按能力自动选择；长期记忆跨 Session
持久化；Skills 可管理；SubAgent 安全委派；所有能力受授权。

## Phase 3：产品工作台和前后端体验

-   P3-01 默认助手；
-   P3-02 自然语言 Builder；
-   P3-03 新 API v2；
-   P3-04 Chat 重构；
-   P3-05 Run Timeline；
-   P3-06 Cancel / Resume / Reconnect；
-   P3-07 保留旧业务入口，Workflow Agent Node 调用统一 Runtime。

**验收：**
用户无需底层配置即可登录、对话、上传文件、查看进度、取消、重连、创建专用助手并立即使用。

## Phase 4：Harness 质量、容错和性能

-   P4-01 Task Contract；
-   P4-02 Verification；
-   P4-03 Retry / Recovery；
-   P4-04 Context Engineering；
-   P4-05 ModelClientFactory、并发、工具超时、Graph
    构建、事件持久化优化；
-   P4-06 并发、长稳和故障注入。

**验收：**
复杂任务有完成条件；工具失败按风险策略处理；非幂等操作不盲目重放；大文件不无控制进入上下文；延迟、成功率和恢复率有实测报告。

## Phase 5：旧架构清理和发布

-   P5-01 删除旧引擎；
-   P5-02 删除旧 API/事件协议；
-   P5-03 重写
    README、架构、Runtime、数据模型、API、部署、安全、可观测和运维文档；
-   P5-04
    完成后端、前端、Playwright、MySQL、PostgreSQL、真实模型、安全、多用户、并发、长稳和故障恢复测试。

------------------------------------------------------------------------

# 13. 强制验收标准

## 13.1 产品体验

  ID      验收项       通过条件
  ------- ------------ ------------------------------------------------
  UX-01   默认助手     新用户无需创建 Agent 即可对话
  UX-02   创建助手     一段自然语言生成可用草稿
  UX-03   配置门槛     普通创建不要求模型、Prompt、工具、温度、迭代数
  UX-04   能力包       用户选择研究、文档、数据、知识等能力
  UX-05   对话过程     看到计划、工具、状态、结果和错误
  UX-06   文件         上传、处理、查看、下载产物
  UX-07   取消         UI 取消真实触发后端取消
  UX-08   重连         刷新或断网后恢复 Run 和事件
  UX-09   审批         显示动作和参数摘要，按 action_id 审批
  UX-10   多 Session   不串消息、状态和文件

## 13.2 Agent 能力

  ID      验收项              通过条件
  ------- ------------------- ------------------------------------
  AG-01   原生 Tool Calling   真实模型返回结构化工具调用
  AG-02   Planning            复杂任务形成并更新计划
  AG-03   Skills              加载并执行实际 Skill
  AG-04   SubAgent            至少两类专用子代理可安全委派
  AG-05   Memory              跨 Session 持久化并隔离
  AG-06   Context             大输出和大文件不无控制进入上下文
  AG-07   Verification        复杂任务有可检查完成条件
  AG-08   Tool Policy         原生工具和平台工具均受统一策略约束
  AG-09   Sandbox             无 Sandbox 时不暴露执行能力
  AG-10   Model Capability    不兼容模型在创建/发布阶段被发现

## 13.3 Runtime 可靠性

  ID      验收项        通过条件
  ------- ------------- -------------------------------------
  RT-01   幂等          相同 Idempotency-Key 不重复创建 Run
  RT-02   Fencing       旧 Worker 无法提交状态和副作用
  RT-03   Checkpoint    重启后恢复可恢复任务
  RT-04   Tool Ledger   成功副作用可复用结果
  RT-05   Unknown       未知非幂等结果不会自动重放
  RT-06   HITL          审批后精确恢复动作
  RT-07   Cancel        取消意图与取消终态不混淆
  RT-08   SSE           重连无丢失、无重复
  RT-09   Budget        跨恢复预算不重置
  RT-10   Outbox        Session 写回失败可安全重试

## 13.4 可观测性

Trace、Model、Tool、SubAgent、Recovery、Metrics、Privacy 和 Correlation
全部必须通过真实 Run 验收。

## 13.5 工程质量

-   不允许两个同时生效的 Agent 引擎；
-   不允许新旧 Memory 双写；
-   不允许旧 Trace 和新 Trace 混合统计；
-   不允许关键模块依赖环；
-   不允许废弃代码长期保留；
-   不允许新增万能 Service；
-   新增模块必须有测试；
-   DB 迁移必须经过目标数据库验证；
-   关键业务逻辑不得只靠 Mock；
-   真实模型 E2E 必须披露模型、环境和范围。

------------------------------------------------------------------------

# 14. 测试策略

测试分层：

1.  Unit：领域规则、权限、预算、状态机、模型能力；
2.  Integration：Repository、Checkpointer、Store、Tool Adapter；
3.  Runtime E2E：真实 DeepAgents Factory + Runtime Service；
4.  API E2E：登录、创建、对话、Run、审批、取消、恢复；
5.  UI E2E：Playwright 点击级流程；
6.  DB E2E：MySQL 业务库 + PostgreSQL Checkpointer；
7.  Fault Injection：Worker kill、网络超时、模型限流、Checkpoint 损坏；
8.  Load/Soak：多 Run 并发和长时间运行。

## 14.1 真实模型必须走生产路径

现有真实模型专项测试中，有场景直接使用 LangChain `create_agent()`
组装，并不等价于平台生产路径。

新增测试必须走：

``` text
API
 -> RunApplicationService
 -> RunWorker
 -> DeepAgentsExecutor
 -> create_deep_agent
 -> LangGraph
 -> Model / Tool
 -> EventBus
 -> DB
 -> SSE
 -> Session Writeback
```

至少覆盖多步 Tool
Calling、SubAgent、Skill、HITL、大上下文卸载、模型失败重试、Worker
崩溃恢复和 SSE 断线恢复。

## 14.2 重构前后对比

同一模型、同一任务集、同一资源下比较 First Token Latency、P50/P95 Run
Latency、Model Calls、Tool
Calls、Tokens、成功率、恢复率、并发排队、CPU/Memory 和 DB 连接数。

不预先虚构性能数字。Phase 0 建立基线，Phase 4/5 用同一基线对比。

最低要求：

-   既有 Durable Execution 不回归；
-   用户数据隔离测试 100% 通过；
-   非幂等副作用重复执行测试 100% 通过；
-   SSE 回放无丢失、无重复；
-   新产品关键 UI 流程 100% 通过；
-   任务完成率、准确率、P95 和成本有对比报告。

------------------------------------------------------------------------

# 15. 交付物

每个 Phase
提交代码、数据库迁移、自动化测试、测试日志、变更清单、架构文档、API
契约、部署配置、风险/未完成事项和 Git Commit。

最终交付：

-   新产品交互；
-   Agent Domain / Version；
-   Capability Registry；
-   DeepAgents Harness；
-   LangGraph Durable Runtime；
-   Model Router；
-   Memory / Skills / SubAgents；
-   Langfuse / OTel；
-   Run Timeline；
-   Task Contract / Verification；
-   Retry / Recovery；
-   API v2；
-   旧代码删除报告；
-   全量测试和性能报告；
-   部署运维文档。

------------------------------------------------------------------------

# 16. Trae 每阶段执行模板

每阶段开始前先输出：

1.  当前代码状态；
2.  涉及文件；
3.  现有调用链；
4.  改造方案；
5.  数据库影响；
6.  兼容性风险；
7.  测试方案；
8.  删除代码清单；
9.  预计提交批次。

每批完成后输出：

1.  修改文件及职责变化；
2.  关键调用链变化；
3.  删除的旧代码；
4.  新增抽象；
5.  测试命令和真实结果；
6.  未完成项和风险；
7.  Commit SHA。

不得只报告"已完成""测试通过"，必须提供可复现命令和结果。若框架 API
与设计不兼容，先查当前安装版本的官方文档和源码，再调整设计，不得凭旧教程猜
API。

------------------------------------------------------------------------

# 17. 当前执行顺序

``` text
Phase 0：基线、安全、诊断和依赖地图
        |
        v
Phase 1：Langfuse + OTel + 指标
        |
        v
Phase 2：Agent Domain + Model + Capability + Memory
        |
        v
Phase 3：默认助手 + Builder + Workspace + API v2
        |
        v
Phase 4：Verification + Retry/Recovery + 性能
        |
        v
Phase 5：删除旧引擎 + 全量回归 + 发布
```

Phase 2 与 Phase 3 可以在接口确定后并行，但必须先确定 Agent
Definition、Capability 和 Run API 契约。

**第一条实际执行指令：先执行 Phase 0，不要立即重写整个项目。**

第一轮只做仓库和环境基线、全仓依赖地图、真实调用链、新旧代码引用清单、关键权限缺口、运行错误分类、数据库迁移检查、测试基线统一和
Phase 1\~5 文件级任务拆解。

完成后提交《Phase 0 架构诊断与实施计划》，再进入 Phase 1。

------------------------------------------------------------------------

# 18. 技术参考

实施以当前锁定版本的官方文档和源码为准：

1.  DeepAgents Architecture\
    https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md
2.  DeepAgents `create_deep_agent`\
    https://reference.langchain.com/python/deepagents/graph/create_deep_agent
3.  DeepAgents Memory\
    https://docs.langchain.com/oss/python/deepagents/memory
4.  DeepAgents Profiles / excluded_tools\
    https://docs.langchain.com/oss/python/deepagents/profiles
5.  LangGraph Persistence\
    https://docs.langchain.com/oss/python/langgraph/persistence
6.  LangGraph Stores\
    https://docs.langchain.com/oss/python/langgraph/stores
7.  LangChain Middleware\
    https://docs.langchain.com/oss/python/langchain/middleware/overview
8.  Langfuse Python SDK v4 → v5\
    https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5
9.  Langfuse LangChain / LangGraph Integration\
    https://langfuse.com/integrations/frameworks/langchain
10. Langfuse Self-hosted Docker Compose\
    https://langfuse.com/self-hosting/deployment/docker-compose
11. OpenTelemetry GenAI Semantic Conventions\
    https://github.com/open-telemetry/semantic-conventions-genai

------------------------------------------------------------------------

# 最终验收原则

这次重构是否成功，不看新增多少技术组件，也不看目录是否漂亮，只看四件事：

1.  用户是否可以像使用 ChatGPT/豆包一样直接使用平台，而不是先学习一堆
    Agent 配置？
2.  Agent
    是否真正具备规划、工具调用、上下文管理、记忆、子代理、验证和纠错能力？
3.  执行是否可靠、可恢复、可观测，出现问题能否快速定位？
4.  代码是否真正完成新旧架构收敛，能够长期维护和扩展？

如果只是接入
Langfuse、换几个页面、保留旧引擎再增加几个工具，不算完成本次重构。

**最终目标：形成一个以 Agent 产品体验为核心、以成熟 Agent
框架为执行基础、以平台级可靠性和可观测性为保障的 AI-GeneralPlat V3。**
