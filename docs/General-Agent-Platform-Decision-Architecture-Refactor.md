# General Agent Platform --- Decision Architecture 重构任务书

> **目标：** 将当前以正则表达式和关键词规则为主的语义路由，升级为 LLM
> 驱动、State 感知、Runtime 约束的动态决策机制。\
> **仓库：** https://github.com/xxiaoxiong/general-agent-platform\
> **执行方式：** 由 Codex
> 阅读当前代码后分阶段实施；先兼容、再切换，不做大规模推倒重写。

------------------------------------------------------------------------

## 1. 改造目标

当前系统已经具备 DeepAgents Agent Loop、LangGraph State /
Checkpoint、Document Graph、Team Graph、Team Project Graph、Capability
Registry 和 Tool
Policy。本次改造不重写这些执行能力，重点调整它们之前的语义决策层，以及多轮任务连续性判断。

目标架构：

-   **LLM 决定：**
    用户意图、任务目标、任务连续性、适合的执行路线、是否需要澄清、任务计划和能力建议。
-   **State / Checkpoint 提供：**
    当前任务状态、历史计划、执行结果、失败记录、已有产物和恢复上下文。
-   **Runtime 决定：** 执行哪个已注册
    Graph、如何调度、如何恢复、如何处理超时和预算。
-   **Policy 决定：**
    工具权限、资源授权、网络权限、HITL、风险等级和副作用重试策略。

核心原则：**模型拥有语义决策权，但没有权限授予权和执行绕过权。**

### 本次不做

1.  不替换 DeepAgents，不重写 Agent Loop。
2.  不替换 LangGraph，不重写现有 Graph。
3.  不允许模型自行创建工具权限、数据库连接、知识库绑定或任意文件路径。
4.  不为每一轮 Agent 内部 Tool Call 再增加一次 Router 调用。
5.  不把所有确定性校验改成 LLM 判断。
6.  不在本次顺带重构 UI、文档工作流内部实现或整个 Runtime 生命周期。

------------------------------------------------------------------------

## 2. 当前代码基线与问题

请以仓库当前默认分支为准重新阅读代码，以下文件是本次重点：

  -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  文件                                                          当前职责                               主要问题 / 改造方向
  ------------------------------------------------------------- -------------------------------------- ------------------------------------------------------------------------------------
  `backend/app/api/run/core.py`                                 Run                                    在 Run 执行前通过规则确定语义路线；应保留确定性预检，将语义决策迁入可观测的 Run
                                                                入口、附件解析、路由、执行上下文组装   执行过程

  `backend/app/services/agent_runtime/task_router.py`           文档任务意图识别和路由                 `_PROFILE`、`_EDIT`、`_GENERATE`、`_ANALYZE` 等正则决定任务类型；改为 LLM
                                                                                                       决策，保留附件安全与显式参数校验

  `backend/app/services/agent_runtime/document_turn.py`         识别文档任务延续、回溯历史正文和附件   使用大量正则识别
                                                                                                       follow-up、export、reference、anchor；改为上下文驱动的连续性判断和受控历史引用解析

  `backend/app/services/agent_runtime/team_project_intent.py`   区分软件实施、讨论和文档任务           `_BUILD`、`_SOFTWARE`、`_FULLSTACK` 等规则决定项目范围；改为 LLM
                                                                                                       语义识别，保留范围枚举和服务端校验

  `backend/app/services/assistant/task_contract.py`             推导任务类型和完成检查项               `infer_task_type()`
                                                                                                       通过能力包、工具名、关键词推断类型；应与"执行路线决策"解耦，避免重复分类

  `backend/app/services/agent_runtime/service/stream.py`        组装 Runtime 并选择执行路径            根据 `task_type`、`team_mode` 选择 Document Graph、Team Runtime 或
                                                                                                       DeepAgents；应接收已验证的决策结果

  `backend/app/services/agent_runtime/team_graph.py`            团队规划、任务分配、复核和返工         已采用 LLM 规划 + 程序调度，原则上保留

  `backend/app/services/agent_runtime/team_project_graph.py`    软件项目规划、文件分工、实现和验收     已采用 LLM 生成计划 + Pydantic / DAG / 文件约束校验，原则上保留

  `backend/app/services/assistant/capability.py`                Capability 注册、能力解析和资源要求    确定性授权，保留

  `backend/app/services/agent_runtime/tools/policy.py`          工具风险、权限和审批                   确定性安全边界，保留

  `backend/app/services/agent_runtime/persistence.py`           Checkpointer / Store / thread_id       复用现有持久化，不另造一套 Checkpoint
  -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

### 当前问题必须被解决

-   `route_task()` 的固定优先级和正则容易把多意图请求压缩成单一类别。
-   `confidence`
    是规则中写死的数字，并非模型校准后的置信度，不应继续作为真实可信度指标。
-   `document_turn()` 通过关键词和最近 24
    条消息推断任务锚点，可能把无关历史拼入文档上下文，也可能漏掉语义相关的历史内容。
-   `project_scope()` 依赖用户是否使用预设关键词，无法稳定覆盖自然表达。
-   `TaskContract` 的任务类型推导与 Router
    语义重叠；它应该描述目标和验收义务，而不是再成为第二个 Router。
-   当前入口在 API 层先完成语义路由，路由错误会影响后续 Graph 的选择。

------------------------------------------------------------------------

## 3. 目标决策模型

新增独立的 Decision 模块，建议目录：

``` text
backend/app/services/agent_runtime/decision/
  __init__.py
  schemas.py          # Pydantic 决策结构
  context.py          # 决策上下文构建与裁剪
  service.py          # LLM 决策调用
  validator.py        # 决策结果白名单、资源和权限校验
  persistence.py      # 决策快照读写（优先复用现有 Run 持久化）
```

如果当前代码组织方式有更合适的位置，可以调整目录，但职责必须清晰，不要把全部逻辑重新堆进
`core.py` 或 `stream.py`。

### 3.1 结构化决策 Schema

建立 Pydantic `RunDecision`，至少包含：

``` python
class RunDecision(BaseModel):
    schema_version: Literal["v1"] = "v1"

    intent: Literal[
        "general_chat",
        "research",
        "document_analyze",
        "document_generate",
        "document_edit",
        "profile_extract",
        "software_project",
        "data_analysis",
        "database_query",
        "coding",
        "business_action",
        "unknown",
    ]

    route: Literal[
        "deep_agent",
        "document_graph",
        "team_graph",
        "team_project_graph",
        "clarify",
    ]

    continuity: Literal[
        "new_task",
        "continue_existing",
        "modify_existing",
        "resume_existing",
        "unrelated",
    ]

    goal: str
    constraints: list[str] = []
    deliverables: list[str] = []
    capability_suggestions: list[str] = []
    context_refs: list[str] = []
    acceptance_criteria: list[str] = []

    project_scope: Literal["fullstack", "frontend", "backend"] | None = None

    decision_status: Literal["clear", "ambiguous"]
    clarification_question: str | None = None
    rationale: str = ""
```

要求：

-   不使用 `0.98` 这类模型自报数字作为可信度；使用 `clear / ambiguous`
    和可审计的理由。
-   `rationale` 只记录简短决策摘要，不要求模型输出隐藏推理链。
-   `capability_suggestions` 只是模型建议，不代表授权。
-   `context_refs` 只能引用 Context Builder 提供的候选
    ID；模型不得自行构造文件路径、附件路径或资源 ID。
-   `route` 必须是服务端允许的枚举，不能让模型返回任意模块名。
-   对不确定或存在关键歧义的任务，允许
    `route="clarify"`，并给出一个明确的澄清问题。
-   Schema
    应设置合理的字段长度、列表长度和额外字段禁止策略，防止模型输出失控。

### 3.2 路线定义

  ---------------------------------------------------------------------------------------------------------------
  Route                   适用场景                                                 执行目标
  ----------------------- -------------------------------------------------------- ------------------------------
  `deep_agent`            普通问答、研究、数据分析、代码协助、文档分析等通用任务   现有 DeepAgents

  `document_graph`        明确需要文档生成、已有文档编辑、规范提炼等专用交付流程   现有 Document Graph

  `team_graph`            已授权的多角色研究、分析和协作任务                       现有 Team Graph

  `team_project_graph`    软件工程实施、需要真实源码和验收的软件项目               现有 Team Project Graph

  `clarify`               关键目标、目标文档或任务连续性无法可靠确定               向用户澄清，不启动错误工作流
  ---------------------------------------------------------------------------------------------------------------

`document_analyze` 当前主要使用通用 Agent
与文档预览上下文，不要为了统一而强制迁移到 Document
Graph；先保持现有正确执行路径。

`team_mode`、显式任务模式、文档 ID、文档 Profile ID
等用户明确指定的控制项必须优先于模型自动判断。自动启用 Team
的能力应受配置控制，默认保持兼容现有行为。

------------------------------------------------------------------------

## 4. Decision Context：必须结合 State 和历史，而不是只看当前消息

新增 `DecisionContextBuilder`。决策输入至少包含：

1.  当前用户消息。
2.  用户显式选择的 `task_mode`、`team_mode`。
3.  当前 Run / Session 的基本信息。
4.  最近相关的用户与助手消息，经过长度限制和敏感信息处理。
5.  当前会话已有的活动任务、未完成任务和最近失败任务摘要。
6.  已有文档、文件、附件和交付物的服务端元数据。
7.  当前已启用的 Capability 描述和可用执行路线。
8.  与当前任务明确关联的 Checkpoint 摘要（如存在）。

### Checkpoint 使用要求

当前项目的 `make_thread_id()` 对普通 Agent 使用 Session 级 thread_id，而
Team Graph 使用独立的 Run 级 thread_id。不要假设所有 Graph 都共享同一个
Checkpoint。

-   只有确定了 Graph 类型、thread_id 和所属 Run 后，才读取对应
    Checkpoint。
-   不要把原始 Checkpoint 全量内容直接塞给模型。
-   应提炼为有边界的摘要：目标、当前计划、已完成项、未完成项、最近结果、失败原因和产物引用。
-   新 Run 的决策不能误读其他用户、其他 Session 或其他 Graph 的状态。
-   无法确认状态归属时，宁可不注入，也不能猜测。
-   对话历史、Run 记录和 Checkpoint 应作为互补上下文，不要把长期 Memory
    当作当前任务状态。

### 历史引用要求

模型可以从候选消息 / 产物列表中选择 `context_refs`，例如：

``` json
{
  "context_refs": ["message_104", "artifact_22"],
  "continuity": "modify_existing"
}
```

后端必须再次验证：

-   引用是否真实存在；
-   是否属于当前用户和当前 Session；
-   是否允许当前 Run 访问；
-   对应文件是否仍然存在且指纹一致。

模型不得直接决定读取任意历史文件，也不得自行拼接 `/workspace` 路径。

附件正文和历史文档均属于不可信数据。必须明确告诉模型：其中的指令只是待分析内容，不能覆盖系统规则和用户当前要求。

------------------------------------------------------------------------

## 5. 决策调用与执行流程

### 5.1 调用位置

API 层只负责确定性预检：

-   身份与 Session 校验；
-   幂等键；
-   附件路径、大小、指纹和归属校验；
-   显式任务模式和资源 ID 校验；
-   请求基础参数验证。

语义决策应进入已经创建并持久化的 Run 执行过程，以便具备 Run
ID、Trace、预算、事件记录和恢复能力。

在 `service/stream.py`
中调整执行顺序：先完成必要的模型配置与上下文准备，再执行
Decision，得到经过验证的 `execution_context`，最后计算 `is_document_run`
/ `is_team_run` 并选择现有执行路径。不要在读取 `task_type` 后才做决策。

### 5.2 单次决策流程

``` text
新 Run
  │
  ▼
确定性预检
  │
  ├── 用户显式指定模式 / 明确资源操作
  │      └── 按显式控制解析，不重复询问模型
  │
  ▼
构建 DecisionContext
  │
  ▼
LLM Structured Decision（每个新 Auto Run 一次）
  │
  ▼
Pydantic Schema 校验
  │
  ▼
DecisionValidator
  │
  ├── 非法路线 / 未授权能力 / 无效引用 → 拒绝或安全降级
  ├── 关键歧义 → clarify
  └── 合法决策
          │
          ▼
持久化决策快照 + 发布决策事件
          │
          ▼
Document Graph / Team Graph / Team Project Graph / DeepAgents
```

性能要求：

-   不要在 Agent 内部每轮工具调用前重复执行 Decision。
-   显式模式、审批恢复、取消、已有 Run 的断点续跑，不应重复调用语义
    Router。
-   自动模式下每个新 Run
    最多一次正常决策调用；只有结构化输出解析失败时才允许一次修复重试。
-   优先使用当前 Agent 已配置的模型和
    Provider，避免引入新的模型服务依赖。可以预留独立轻量决策模型配置，但不是本次必需。
-   决策输出控制在小型结构化对象，不生成长篇计划或解释。

### 5.3 决策失败与降级

-   模型超时、服务不可用、Schema 连续校验失败：记录失败原因。
-   对没有明确资源操作要求的普通 Auto 请求，安全降级到
    `deep_agent`，不要误启动专用工作流。
-   对用户明确指定的文档、文件或执行模式，不能静默忽略用户选择；无法安全判断时应澄清或返回明确错误。
-   对模型建议的未授权
    Capability，拒绝该建议并记录，不允许自动扩大工具范围。
-   对 `ambiguous`
    决策，不能伪造高置信度，也不能用固定默认值冒充模型判断。

------------------------------------------------------------------------

## 6. 现有模块的具体改造要求

### 6.1 `task_router.py`

-   将显式任务模式、文档 ID、Profile ID、附件安全检查与语义分类分离。
-   `route_task()` 不再作为 Auto 模式的最终语义决策器。
-   可以保留兼容接口，但在 LLM 模式下不得通过
    `_PROFILE`、`_EDIT`、`_GENERATE` 等正则覆盖模型决策。
-   正则只允许用于确定性语法提取或兼容旧数据，不能决定复杂自然语言的真实意图。
-   删除固定 `confidence` 数字的可信度含义，改为决策来源和状态。

### 6.2 `document_turn.py`

-   不再自行决定是否把当前任务改成 `document_generate`。
-   拆分为"历史上下文解析"和"文档工作流路由"两件事。
-   是否延续、修改、导出已有文档由 Decision 决定。
-   历史正文和附件通过 `context_refs` 选择，再由服务端验证和读取。
-   保留必要的文本长度限制、附件指纹校验和路径安全检查。
-   避免把最近 24 条消息无差别拼接；优先使用相关消息与已有任务摘要。

### 6.3 `team_project_intent.py`

-   Auto 模式下的软件实施意图与 `project_scope` 改由 LLM 结构化输出。
-   `project_scope` 仍只能是 `fullstack / frontend / backend`。
-   `team_mode` 显式选择继续有效。
-   是否允许 Auto 自动启用 Team
    Project，应由平台配置决定，不能由模型绕过配置。
-   `team_project_graph.py` 中的文件数量、路径、依赖
    DAG、角色数量和交付物校验全部保留。

### 6.4 `task_contract.py`

将"任务语义识别"和"完成条件契约"分离：

-   `TaskContract` 不再承担第二套 Router 职责。
-   任务类型优先取已验证的 `RunDecision.intent`。
-   显式文件名、文件格式、URL、数量、字符数等可机械验证的信息可以继续确定性提取。
-   复杂任务的目标、交付物和验收标准可以复用 Decision
    的结构化结果，避免再增加一次模型调用。
-   简单问答仍保持轻量，不强制生成复杂契约。
-   保留现有 Verification、证据核验和完成状态机制。

### 6.5 `service/stream.py`

-   在选择 Graph 前获取经过验证的 Decision。
-   将 `RunDecision` 转换成当前执行引擎兼容的 `execution_context`。
-   不要让模型直接传入 `task_type`、`team_mode`
    或任意执行对象后就立即分流。
-   保留现有 Document Graph、Team Runtime、DeepAgents 组装方式。
-   Checkpoint 恢复时复用已持久化的
    Decision，不要因进程重启重新推断路线。
-   保持现有 Run 状态机、Worker Lease、Fencing、工具账本、HITL
    和副作用恢复逻辑不变。

------------------------------------------------------------------------

## 7. Decision 持久化与可观测

每次决策应保存一个可审计快照，至少包含：

``` json
{
  "schema_version": "v1",
  "decision_source": "llm",
  "intent": "document_generate",
  "route": "document_graph",
  "continuity": "continue_existing",
  "decision_status": "clear",
  "context_refs": ["message_104"],
  "capability_suggestions": ["doc_processing"],
  "model_id": "当前实际模型标识",
  "latency_ms": 0,
  "fallback": false
}
```

要求：

-   将决策快照写入 Run 的持久化输入 /
    执行上下文，或采用当前项目合适的持久化方式。
-   增加 `task.decision` 事件；现有 `task.routed`
    事件继续表示最终通过校验的执行路线。
-   Trace 至少拆分
    `decision.context_build`、`decision.model_call`、`decision.validation`。
-   记录决策模型、耗时、Token 用量、Schema 版本、降级原因和最终路线。
-   不记录隐藏推理链，不在普通日志中泄露完整附件正文、敏感字段或凭据。
-   能够从 Run ID 回看：模型建议了什么、Runtime
    最终接受了什么、是否发生降级、最终进入哪个 Graph。

------------------------------------------------------------------------

## 8. 迁移策略：必须分阶段，先 Shadow 再切换

### Phase 0：基线和测试集

-   阅读当前实现与全部相关测试。
-   固化现有 Router 的输出作为 Legacy Baseline。
-   整理不少于 200
    条路由评测样例，覆盖普通问答、研究、文档生成/编辑/分析、软件讨论/实施、多意图、否定表达、多轮延续、附件、显式模式和模糊请求。
-   为每条样例标注正确 intent、route、continuity 和是否需要澄清。

### Phase 1：增加 Decision 模块，Shadow Mode

-   新增 Schema、Context Builder、Decision Service、Validator。
-   增加 `off / shadow / llm` 配置模式。
-   Shadow 模式下同时计算 Legacy 结果和 LLM 结果，但**实际执行仍使用
    Legacy 路由**。
-   记录差异、延迟、Token、Schema 错误和上下文引用情况。
-   不删除旧代码，不改变现有执行行为。

### Phase 2：切换 Auto 路由

-   显式模式和资源选择继续使用确定性解析。
-   Auto 模式使用通过校验的 LLM Decision。
-   按配置控制 Auto Team / Team Project。
-   保留可快速回滚到 Legacy 的 Feature Flag。
-   验证四条执行路线与事件流、Run 状态和恢复行为兼容。

### Phase 3：多轮连续性和文档上下文迁移

-   将 `document_turn()` 的语义判断迁移到 Decision。
-   用 `context_refs` 选择历史消息、已有文档和产物。
-   服务端验证引用归属、文件存在性和指纹。
-   测试继续写作、修改已有文档、导出已有研究结果、切换话题、引用旧任务等场景。

### Phase 4：TaskContract 解耦

-   任务类型从 Decision 传入。
-   验收契约只负责描述和验证完成条件。
-   删除重复的关键词任务分类，保留必要的确定性格式解析。
-   确认简单问答没有额外模型调用和明显延迟增加。

### Phase 5：评测、清理与文档

-   完成 A/B 评测与回归。
-   删除已经不再使用的语义正则和死代码。
-   更新架构文档、配置说明、事件说明和测试文档。
-   输出迁移前后对比报告。

------------------------------------------------------------------------

## 9. 评测与验收标准

### 路由质量

至少统计：

-   Route Accuracy：总体路线准确率。
-   Specialized Route Precision：专用工作流路由精确率，重点防止误启动。
-   Route Recall：应进入专用路线却被漏掉的比例。
-   Continuity Accuracy：多轮任务延续 / 修改 / 新建判断准确率。
-   Clarification Quality：真正需要澄清时是否提出有效问题。
-   Confusion Matrix：各意图之间的混淆情况。

建议初始上线门槛：

-   专用工作流 Route Precision ≥ 95%。
-   多轮 Continuity Accuracy ≥ 90%。
-   200 条以上标注样例完成离线评测。
-   安全、权限、资源越权事件为 0。
-   不允许因为 Router 输出错误而绕过 Tool Policy / HITL。
-   对比 Legacy Baseline，不能只报告总体准确率，必须报告各类别 Precision
    / Recall。

上述数值是上线门槛建议，应根据实际评测集质量和业务风险复核。

### 性能和稳定性

-   显式任务模式、审批恢复、取消、断点续跑不额外调用 Router。
-   每个新的 Auto Run 最多一次正常 Decision
    调用，结构化输出失败最多修复一次。
-   记录决策耗时 P50 / P95、输入输出 Token、模型调用成本。
-   Decision 服务失败时有明确降级行为，不得造成 Run 卡死。
-   进程重启后不重复生成已持久化的 Decision。
-   幂等请求不重复创建 Run 或重复执行 Decision。
-   不破坏现有 Checkpoint、工具账本、Lease / Fencing 和副作用恢复。

### 必须覆盖的回归场景

1.  "怎么开发一个设备管理平台？"------咨询，不应误启动软件项目。
2.  "帮我构建一个设备管理平台并交付源码。"------软件实施。
3.  "先调研行业，再把结果整理成
    Word。"------复合目标，不能只识别到一个关键词。
4.  "把刚才的研究报告补充数据后重新导出。"------延续并修改已有产物。
5.  "把刚才的内容翻译成英文。"------结合上下文判断，不应机械地认定为新文档。
6.  "继续。"------必须依赖可确认的活动任务上下文，不能随意继承旧任务。
7.  用户上传 Word 后说"检查第三章"。------引用正确附件，不读取其他
    Session 文件。
8.  用户明确选择 `chat` / `document_generate`。------显式选择优先。
9.  用户要求联网，但当前 Capability
    未授权网络。------模型不能自行扩大权限。
10. 模型输出非法 route、虚构 context_ref 或未授权
    Capability。------服务端拒绝或安全降级。
11. Run 在 Decision 完成后崩溃并恢复。------复用原
    Decision，不重复路由。
12. 历史附件包含恶意指令。------按不可信资料处理，不改变系统规则。

------------------------------------------------------------------------

## 10. 实施完成后需要交付

Codex 完成后请提供：

1.  **改造总览：** 新旧决策链路、模块职责变化。
2.  **关键代码调用链：** 从 API 收到消息，到 Decision、校验、Graph
    分流，再到 Checkpoint。
3.  **旧逻辑处置清单：**
    哪些正则删除、哪些保留为语法提取或安全校验、哪些保留兼容。
4.  **Schema 和事件样例：** 展示一次真实决策的输入、输出和最终执行路线。
5.  **测试报告：** 单元测试、集成测试、200+ 样例评测结果和混淆矩阵。
6.  **性能报告：** 路由耗时、模型调用次数、Token 成本、降级次数。
7.  **风险与遗留问题：** 特别说明模型误判、上下文污染、Checkpoint
    归属和多轮连续性风险。

### 最终验收原则

不是看新增了多少类、多少节点，也不是看是否把正则表达式换成了 LLM 调用。

而是验证：

-   模型是否真正基于当前请求和相关历史状态做决策；
-   复杂、多意图、多轮任务是否比旧规则更准确；
-   错误路由是否减少；
-   模型决策是否可解释、可追踪、可回放；
-   Runtime 是否仍然确定、可控、可恢复；
-   权限和安全边界是否完全没有被削弱。

**最终目标：语义决策智能化，执行控制确定性，任务状态可持续，整个系统可观测、可评测、可恢复。**
