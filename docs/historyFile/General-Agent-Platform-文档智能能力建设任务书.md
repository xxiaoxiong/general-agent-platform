# General Agent Platform：文档智能能力建设实施任务书

> **交付对象：** Codex\
> **目标仓库：**
> `https://github.com/xxiaoxiong/general-agent-platform.git`\
> **任务性质：** 在现有 Agent Platform
> 上建设完整的文档理解、规范提炼、文档生成与编辑能力\
> **核心原则：** 智能体对话是统一入口；文档能力是专业执行路线；现有
> Agent Runtime 是唯一执行基础设施。\
> **实施方式：**
> 先审查当前代码与测试，再分阶段实现；不得以大规模重写现有 Runtime
> 代替增量集成。

------------------------------------------------------------------------

# 0. 给 Codex 的执行指令

本任务不是开发一个简单的 Word
导出按钮，也不是开发一个独立的模板填充器。需要将文档处理建设为 General
Agent Platform 的一项完整业务能力。

请先完整阅读本任务书，再检查仓库当前代码、依赖、迁移、测试和前端交互。任务书中的路径是当前仓库基线参考，实际实施前必须重新确认文件是否移动、接口是否变化。

## 0.1 执行要求

1.  不要只输出设计方案或伪代码。完成代码审查后，按阶段直接实施。
2.  不要一次性重写 `AgentRuntimeService`、Worker、Run 生命周期或前端
    Workspace。
3.  优先复用已有的
    DeepAgents、LangGraph、Worker、Checkpoint、Run、EventBus、SSE、HITL、Skill
    Registry、文件上传、Artifact 和预览能力。
4.  新增领域对象、API、前端组件和测试时，遵循当前项目代码风格。
5.  所有数据库结构变更必须提供可执行的 Alembic migration，并验证 MySQL
    兼容性；不能只依赖开发环境的 `create_all`。
6.  不允许把用户输入、上传文件、Skill
    内容当作可信指令；文档内容只是待处理数据，不能改变系统权限。
7.  不允许伪造文档生成成功、文件写入成功、质量检查通过或测试通过。
8.  任务必须支持正常失败、取消、人工补充、断点恢复、事件回放和服务重启后的恢复。
9.  不得让文档工作流绕过 Agent 的
    Capability、ToolPolicy、数据源绑定、HITL 和用户隔离。
10. 每个阶段完成后运行对应测试；最后运行完整后端测试、前端测试和生产构建。

## 0.2 实施前必须提交的审查结果

先检查并简要记录：

-   当前 `main` 分支实际提交版本；
-   Agent Runtime 的真实调用链；
-   Run 输入、配置快照、Worker 恢复和 Checkpoint 的数据流；
-   当前 Skill 的创建、授权、注入和版本行为；
-   文件上传、解析、预览和 Artifact 下载链路；
-   当前前端对话事件归约与历史消息恢复方式；
-   当前数据库迁移和测试环境约束；
-   本任务设计与现有实现之间的冲突点。

审查完成后直接按本任务书实施，不要因为已有基础设施存在就跳过缺失能力，也不要为了追求架构完整而引入无关重构。

------------------------------------------------------------------------

# 1. 项目背景与目标

## 1.1 业务背景

平台需要支持用户通过自然语言完成企业日常文档工作。用户提供的资料不一定是正式
Word 模板，可能是：

-   一份结构简单的参考文档；
-   一份过去已经完成的报告；
-   一份只列出章节和要点的简化文档；
-   一份包含写作风格、内容要求和注意事项的说明；
-   用户直接用自然语言描述的文档要求；
-   一个已经固化文档规则的 Skill。

平台需要能够从这些资料中提炼文档规范，经过用户确认后保存并复用。后续用户只需要在智能体对话中说出任务，Agent
就能选择规范、收集资料、生成文档、验证内容并交付文件。

本次不以"技术文档"为唯一对象。优先支持项目总结、工作汇报、会议纪要、实施方案、调研报告、业务分析、阶段总结、制度说明等通用企业文档。

## 1.2 最终产品目标

用户能够在普通智能体对话中完成以下任务：

1.  阅读和分析上传的文档。
2.  根据参考文档提炼一套可复用的文档规范。
3.  创建、修改、发布和复用 Document Profile。
4.  根据 Document Profile、用户要求和业务数据生成 Word。
5.  对已生成文档进行局部修改，并保留版本历史。
6.  在资料缺失时由 Agent 向用户收集信息，随后恢复原任务。
7.  在对话中查看文档产物、预览、下载和继续修改。
8.  在文档中心管理文档和文档规范。

## 1.3 成功标准

不能以"多了几个工具""多了一个 Skill"或"能导出 docx"作为完成标准。

最终必须形成一个可验证的业务闭环：

**参考资料 → 规范提炼 → 用户确认 → 规范保存 → 对话发起任务 → 任务路由 →
数据收集 → 文档生成/编辑 → 内容验证 → 文件交付 → 版本管理。**

------------------------------------------------------------------------

# 2. 当前仓库基线与审查结论

以下是本任务制定时检查到的仓库基础。Codex 必须在实施前重新核对。

## 2.1 已有能力

  ---------------------------------------------------------------------------------------------
  模块                    当前已有实现                         本次处理方式
  ----------------------- ------------------------------------ --------------------------------
  Agent 内核              DeepAgents + LangGraph               复用，不另建 Agent Loop

  Run 生命周期            Run、Worker、Lease、Fencing、恢复    复用并扩展必要状态

  Checkpoint              LangGraph Checkpointer / Store       复用，保证文档图可恢复

  事件机制                EventBus、事件持久化、SSE、回放      扩展文档事件

  HITL                    高风险工具人工审批                   保持原语义，不冒充普通信息收集

  Skill                   多文件 Skill、Skill Registry、Agent  复用，但不把 Profile 等同于
                          绑定                                 Skill

  Capability              Capability                           补齐文档能力映射
                          Registry、工具授权、资源绑定         

  文件上传                会话工作区上传，PDF/Word/Excel/PPT   复用并增强结构化解析
                          文本抽取                             

  文件预览                `FilePreviewPanel`，支持             扩展文档预览
                          PDF、文本、Office 抽取文本等         

  Artifact                会话工作区产物安全下载               保留并补齐文档资产管理

  可视化 Workflow         现有节点式 WorkflowEngine            不作为本次文档执行引擎
  ---------------------------------------------------------------------------------------------

## 2.2 关键代码位置

### Backend

-   `backend/app/services/agent_runtime/service.py`
    -   `_start`
    -   `_begin_run`
    -   `_drive`
    -   `_consume_stream`
    -   `_skill_files`
    -   `_resolve_tool_specs`
-   `backend/app/services/agent_runtime/worker.py`
    -   `RunJob`
    -   `run_claimed`
    -   `_job_from_row`
    -   `claim_run`
    -   恢复与 Lease 管理
-   `backend/app/services/agent_runtime/lifecycle.py`
    -   Run 状态、状态转移、暂停状态
-   `backend/app/services/agent_runtime/events.py`
    -   `RuntimeEvent`、事件类型、事件持久化
-   `backend/app/services/agent_runtime/event_protocol.py`
    -   V2 SSE 事件映射、事件边界
-   `backend/app/services/agent_runtime/factory.py`
    -   `build_agent`
    -   DeepAgents、工具、Middleware、Skill、Checkpoint 组装
-   `backend/app/services/agent_runtime/persistence.py`
    -   Checkpointer、Store、thread_id、run config
-   `backend/app/services/agent_runtime/skills.py`
    -   `SkillRegistry`、多文件 Skill 注入
-   `backend/app/assistant/capability.py`
    -   Capability Registry
-   `backend/app/assistant/versions.py`
    -   AgentVersion 快照
-   `backend/app/api/v2/runs.py`
    -   Run 创建、恢复、取消、事件接口
-   `backend/app/api/v2/files.py`
    -   会话文件上传、下载、预览
-   `backend/app/api/v2/artifacts.py`
    -   会话产物安全下载
-   `backend/app/api/v2/skills.py`
    -   Skill 管理 API
-   `backend/app/models/agent.py`
    -   Agent、AgentSession、AgentRun 等
-   `backend/app/models/skill.py`
    -   Skill 数据模型
-   `backend/app/services/workflow_service.py`
    -   当前可视化 WorkflowEngine
-   `backend/requirements.txt`
    -   已有 `python-docx`、`pypdf`、`python-pptx`、`openpyxl`

### Frontend

-   `frontend/src/views/workspace/WorkspaceHome.vue`
-   `frontend/src/views/workspace/ConversationComposer.vue`
-   `frontend/src/features/run/blocks.ts`
-   `frontend/src/features/run/MessageMarkdown.vue`
-   `frontend/src/features/run/ToolStep.vue`
-   `frontend/src/features/preview/FilePreviewPanel.vue`
-   `frontend/src/api/v2.ts`
-   `frontend/src/router/index.ts`
-   `frontend/src/layouts/MainLayout.vue`（导航需要核对）

## 2.3 当前实现的关键缺口

### 缺口 A：文件读取不等于文档生成

当前上传 API 会把 Office/PDF
文件放入会话工作区，并尽可能提取文本副本。`FilePreviewPanel` 对 Office
文件主要展示抽取文本，不等于完整文档结构解析，也不等于生成或编辑 Word。

### 缺口 B：Artifact 只有下载基础

当前 `artifacts.py`
提供基于会话工作区的安全下载，但还没有完整的文档资产、版本、Profile
关联和对话 Artifact Card 业务模型。

### 缺口 C：现有 `doc_output` 不是 Word 生成引擎

`workflow_service.py` 中的 `doc_output` 节点主要收集上游文本并返回
`is_document=True`，不能据此认为系统已经具备 DOCX
渲染、结构验证和版本管理。

### 缺口 D：当前可视化 WorkflowEngine 不适合承载长文档任务

现有 `WorkflowEngine` 采用自定义队列/BFS 方式执行节点。它与 Agent
Runtime 的 Durable Run、Checkpoint、HITL、Lease
和事件恢复机制不是同一套执行模型。

本次文档工作流必须使用 LangGraph，并接入现有 Agent Runtime。不要把现有
BFS WorkflowEngine 改造成第二套 Agent Runtime，也不要用它替代 LangGraph
文档图。

### 缺口 E：Skill 不能代替结构化文档规范

当前 Skill 是指令文件及附属文件，当前 DB Skill
表使用全局唯一名称，并且写入 API
仅超级管理员可操作。它适合保存"怎样做"，不适合单独承担每个用户自己的结构化文档规范、章节约束和版本数据。

### 缺口 F：当前没有普通业务信息收集的 Durable Resume

当前 Run Resume 主要用于工具审批，`RunResumeRequest` 的核心语义是
approve/reject。文档生成过程中询问用户补充项目时间、成果数据、报告范围等，不是工具审批。

本次必须建立独立的 `waiting_input` / Input Request
机制，不能把普通用户输入伪装成工具 HITL 审批。

### 缺口 G：Artifact 事件链路尚未完整

当前 V2 事件协议中存在 `artifact.created` 映射预留，但需要核对并补齐内部
RuntimeEvent 常量、发布、持久化、回放和前端消费链路。

------------------------------------------------------------------------

# 3. 核心架构决策

以下决策是本次实施的边界，不要随意改变。

## 3.1 统一对话入口，独立文档管理工作区

**智能体对话是日常任务的统一入口。**

用户不需要先进入文档中心才能生成文档，也不需要手动选择复杂的工作流。

文档中心是管理入口，负责：

-   文档规范管理；
-   文档资产管理；
-   文档版本查看；
-   文档任务历史；
-   规范编辑和发布。

实际生成、分析和修改仍从智能体对话发起。

## 3.2 Document Profile 与 Skill 分工

不要二选一，采用两层机制。

### Document Profile：结构化文档规范

负责描述"文档应该是什么样子"。

它必须是可被程序读取、验证和执行的结构化数据，而不是只有一段 Markdown
提示词。

包括：

-   文档类型、名称和描述；
-   目标读者；
-   语言、语气和写作风格；
-   章节结构、顺序和是否必需；
-   各章节的内容目标；
-   必需字段和数据来源要求；
-   格式偏好；
-   全局写作规则；
-   内容质量检查规则；
-   缺失信息收集规则；
-   输出格式。

### Skill：通用执行方法

负责描述"Agent 怎样完成文档任务"。

例如：

-   如何分析参考文档；
-   如何使用 Document Profile；
-   如何收集资料；
-   如何分章节撰写；
-   如何避免编造数据；
-   如何进行质量检查；
-   如何调用文档能力。

### 两者的权威关系

1.  Document Profile 是章节结构、字段和验证规则的权威来源。
2.  Skill 是执行方法和写作指导。
3.  样例文档是参考证据，不是必须逐字复制的模板。
4.  Document Workflow 负责执行流程。
5.  Document Engine 负责真实文件 I/O。

不要把 Profile JSON 和 Skill Markdown
复制成两份互相独立、容易漂移的规则。

建议提供一个平台级通用 `document-authoring` Skill。Profile 的具体内容由
Document Workflow 加载并作为结构化上下文传入，不要把所有用户 Profile
永久注入所有 Agent。

如果用户明确要求"将这套规则固化为
Skill"，可以提供导出/关联能力；但不能因为创建 Profile 就自动创建全局 DB
Skill。当前 Skill 的全局唯一名称、Agent
绑定和超级管理员写权限不适合直接承载个人 Profile。

## 3.3 文档任务作为专业 LangGraph 执行路线

文档工作流不是一个普通工具，也不是一个新 Agent Runtime。

建议采用：

``` text
POST /api/v2/runs
        |
        v
  Task Router
        |
        +--------------------+
        |                    |
        v                    v
  General Agent         Document Route
  DeepAgents                 |
                             +----------------------+
                             |          |           |
                             v          v           v
                       Profile      Generation    Editing
                       Graph          Graph        Graph
                             |          |           |
                             +----------+-----------+
                                        |
                                        v
                               Document Engine
                                        |
                                        v
                              Document Asset Store
```

三类文档子流程：

-   `ProfileExtractionGraph`：参考资料 → 规范草稿 → 用户确认 → 保存
    Profile。
-   `DocumentGenerationGraph`：加载规范 → 收集数据 → 生成内容 → 校验 →
    DOCX。
-   `DocumentEditGraph`：读取现有版本 → 定位修改范围 → 生成 Patch → 应用
    → 校验 → 新版本。

它们共享 Document Parser、Document Renderer、Document Validator、Profile
Service、Document Store。

## 3.4 现有 Runtime 仍是唯一执行基础设施

文档图必须复用：

-   `AgentRuntimeService`；
-   `RunWorker`；
-   `RunJob`；
-   `AgentRun`；
-   `RunHandle`；
-   Lease / Fencing；
-   Checkpointer；
-   EventBus / EventRepository；
-   SSE 事件协议；
-   运行预算；
-   Langfuse Trace；
-   现有权限与工具策略。

**不允许：**

-   为文档任务另建一套 Worker；
-   为文档任务另建一套 Run 生命周期；
-   每个文档子步骤都创建一个独立 Run；
-   通过递归调用 `AgentRuntimeService` 形成嵌套 Run；
-   把长文档任务全部塞进一个同步 Tool Handler；
-   用内存变量保存唯一的文档工作流状态；
-   让文档任务绕开现有 Checkpoint 与恢复机制。

## 3.5 Checkpoint 隔离与恢复

当前 `make_thread_id` 对同一 Agent Session 使用会话级
thread_id。普通聊天图与文档图可能具有不同的状态结构，不能在没有验证的情况下直接共用同一个根
Checkpoint。

本次要求：

-   普通聊天保持当前 thread_id 语义，不破坏已有会话历史；
-   文档 Run 使用稳定的、可恢复的 Run 级 Checkpoint 身份，建议
    `agent-{agent_id}-document-run-{run_id}`；
-   文档 Run 中断后恢复必须使用同一个 thread_id 和同一条执行路线；
-   新的一次文档编辑是新的 Run，读取上一个已保存
    DocumentVersion，而不是盲目复用上一次已结束的 Graph State；
-   `session_id` 仍然用于会话归属、消息写回和用户隔离，不要求文档 Graph
    thread_id 等于 Session thread_id；
-   路由结果、Profile ID、Profile Version、Document ID、执行模式必须写入
    Run 可恢复的持久化输入/配置快照；
-   `RunJob._job_from_row` 必须能够在 Worker 重启后完整还原执行上下文。

Codex 必须用实际恢复测试证明这一设计，而不是仅凭代码看起来可恢复。

------------------------------------------------------------------------

# 4. 文档领域模型

本次至少建立以下领域对象。具体字段可按当前 ORM
风格调整，但语义必须保留。

## 4.1 DocumentProfile

建议字段：

  字段                      说明
  ------------------------- ------------------------------
  id                        主键
  owner_id                  创建者/所有者
  name                      规范名称
  description               规范说明
  document_type             文档类型标识
  visibility                private / organization
  status                    draft / published / archived
  current_version           当前版本号
  created_at / updated_at   时间

个人 Profile 默认 `private`。组织共享 Profile
需要具备相应权限，不能让普通用户通过请求参数直接发布组织级规范。

## 4.2 DocumentProfileVersion

每次保存 Profile 都生成不可变版本。

建议字段：

-   id
-   profile_id
-   version_no
-   definition JSON
-   source_summary
-   source_metadata
-   extraction_confidence
-   change_summary
-   created_by
-   created_at

Run 创建时必须绑定具体 Profile Version。生成过程中用户修改
Profile，不能改变已经运行中的文档任务所使用的规则。

## 4.3 DocumentAsset

表示一份逻辑文档，不等于某个 DOCX 文件。

建议字段：

-   id
-   owner_id
-   title
-   document_type
-   session_id（可空）
-   profile_id（可空）
-   current_version
-   status
-   created_by_run_id
-   created_at / updated_at

## 4.4 DocumentVersion

每次创建或修改生成一个新版本。

建议字段：

-   id
-   document_id
-   version_no
-   filename
-   relative_path
-   mime_type
-   content_hash
-   profile_id
-   profile_version_no
-   created_by_run_id
-   change_summary
-   validation_report JSON
-   created_at

版本必须不可变。修改文档默认创建新版本，不得直接覆盖历史版本或用户上传的源文件。

## 4.5 RunInputRequest

用于文档流程中的普通信息收集，与工具审批分离。

建议字段：

-   id / request_id
-   run_id
-   status：pending / submitted / cancelled
-   prompt
-   input_schema JSON
-   required_fields JSON
-   response JSON
-   created_at / submitted_at

如果当前项目有更合适的持久化设计，可以复用已有 Run
扩展字段，但必须满足多 Worker、服务重启和幂等提交要求。

## 4.6 Profile JSON 结构建议

至少具备如下结构能力：

``` json
{
  "schema_version": 1,
  "document_type": "project_summary",
  "name": "项目总结报告",
  "description": "用于项目阶段或结项总结",
  "language": "zh-CN",
  "audience": "企业管理人员",
  "tone": "正式、简洁、客观",
  "output_formats": ["docx"],
  "sections": [
    {
      "id": "project_overview",
      "title": "项目基本情况",
      "order": 1,
      "required": true,
      "purpose": "介绍项目背景、目标和实施范围",
      "required_fields": ["project_name", "project_period"],
      "writing_guidance": "简明描述，不重复背景信息",
      "source_requirements": []
    },
    {
      "id": "project_results",
      "title": "项目成果总结",
      "order": 2,
      "required": true,
      "purpose": "总结项目交付成果",
      "required_fields": ["completed_work", "key_results"],
      "writing_guidance": "优先使用可核验数据",
      "source_requirements": ["项目实施记录"]
    }
  ],
  "required_inputs": [],
  "global_rules": [
    "不得编造业务数据",
    "避免重复描述",
    "缺少重要事实时必须向用户确认"
  ],
  "formatting": {
    "page_size": "A4",
    "heading_hierarchy": true,
    "tables_allowed": true
  },
  "quality_rules": [
    {
      "id": "required_sections",
      "severity": "error",
      "description": "必需章节必须存在"
    },
    {
      "id": "no_fabrication",
      "severity": "error",
      "description": "关键数据必须有来源或明确标注待补充"
    }
  ]
}
```

这只是 Schema 示例。实际需要用 Pydantic 定义并校验，不允许只把任意 JSON
存入数据库后交给模型自行理解。

------------------------------------------------------------------------

# 5. Task Router：对话任务路由

## 5.1 路由类型

建议支持：

  task_type           含义                       执行路线
  ------------------- -------------------------- -------------------------
  general_chat        普通问答、计算、编程等     现有 General Agent
  document_analyze    阅读、解释、总结上传文档   现有 General Agent
  profile_extract     从样例提炼文档规范         ProfileExtractionGraph
  document_generate   生成正式文档               DocumentGenerationGraph
  document_edit       修改已有文档               DocumentEditGraph

注意：`document_analyze` 不等于 `document_generate`。用户上传 Word
并要求总结时，不能自动生成新 Word。

## 5.2 Run API 增加显式模式

在 `RunCreate` 中增加可选字段，建议：

``` json
{
  "task_mode": "auto",
  "document_profile_id": null,
  "document_id": null,
  "task_options": {}
}
```

`task_mode` 至少支持：

-   `auto`
-   `chat`
-   `document_analyze`
-   `profile_extract`
-   `document_generate`
-   `document_edit`

默认 `auto`，保证旧客户端兼容。

显式模式优先于自动识别，但服务端仍然必须校验用户是否有权访问指定 Profile
或 Document。

## 5.3 自动识别策略

采用多信号路由，不要每次消息都额外调用一次大模型分类。

优先级：

1.  用户显式选择的 task_mode；
2.  当前任务上下文，例如是否在继续修改某份文档；
3.  当前消息是否明确包含"生成报告、整理成
    Word、按照规范编写、修改第三章"等操作意图；
4.  附件类型、附件内容和已有 Document/Profile 上下文；
5.  仅在候选文档任务存在歧义时，才使用轻量结构化 LLM 分类。

要求：

-   上传文件本身不能触发文档生成；
-   "阅读并总结 Word"走普通 Agent；
-   "根据这份 Word 生成一份正式报告"走文档生成；
-   "根据这份样例建立可复用规范"走 Profile 提炼；
-   "修改上次生成报告的第三章"走文档编辑；
-   无法确定时优先留在 General Agent，由 Agent 澄清；
-   不要为了识别普通聊天而增加一次不必要的模型调用。

## 5.4 路由结果

路由结果至少包含：

``` json
{
  "task_type": "document_generate",
  "confidence": 0.96,
  "reason": "用户明确要求按照项目总结规范生成 Word",
  "document_profile_id": 12,
  "document_id": null,
  "router_version": 1
}
```

路由结果必须可审计、可回放，并持久化到 Run 输入上下文或 Graph State。

请求幂等摘要必须纳入 task_mode、Profile/Document ID
和附件指纹，避免同一幂等键把不同文档任务错误复用为同一个 Run。

------------------------------------------------------------------------

# 6. Durable User Input：文档任务的人工补充与恢复

这是本次非常重要的基础改造。

## 6.1 与工具审批分离

工具审批解决的是：

> 是否允许执行某个高风险工具动作？

文档输入请求解决的是：

> 文档还缺少哪些业务信息，需要用户补充什么？

两者必须分开。

不要复用 `ApprovalPanel`，不要把用户输入转换成 approve/reject 决策。

## 6.2 新增 waiting_input 状态

在现有 Run 状态机中增加：

``` text
running
   |
   v
waiting_input
   |
   v
running
   |
   +----> completed
   +----> failed
   +----> cancelled
```

要求：

-   `waiting_input` 是可恢复暂停态，不是终态；
-   加入 `PAUSED_STATUSES`；
-   更新 `ALLOWED_TRANSITIONS`；
-   更新 Run Repository 的 paused claim；
-   更新 Worker 的恢复逻辑；
-   更新 Run 状态查询和前端状态映射；
-   不影响原有 `waiting_approval` 语义；
-   `waiting_input` 不应被当作 `completed`。

## 6.3 API 与持久化

新增接口：

`POST /api/v2/runs/{run_id}/input`

请求示例：

``` json
{
  "request_id": "input-request-id",
  "values": {
    "project_finish_date": "2026-11-15",
    "project_owner": "项目负责人"
  }
}
```

要求：

1.  校验 Run 所有权；
2.  校验 Run 当前处于 waiting_input；
3.  校验 request_id 属于当前 Run 且处于 pending；
4.  根据 input_schema 校验字段类型和必填项；
5.  先持久化用户输入，再恢复 Graph；
6.  同一个 request_id 重复提交必须幂等；
7.  冲突提交明确返回 409；
8.  恢复时使用同一 Run、同一 Checkpoint 和同一 thread_id；
9.  Worker 重启后仍能知道等待什么、用户提交了什么。

原有 `POST /runs/{run_id}/resume` 继续处理工具审批，不要破坏兼容性。

## 6.4 事件协议

新增：

-   `input.required`
-   `input.received`

并在 `run.status_changed` 中正确表达 `waiting_input`。

SSE 消费端收到 `waiting_input` 后结束当前订阅流，但 Run
仍保持可恢复。用户提交后重新订阅同一个 Run 的事件流。

必须支持事件持久化、Last-Event-ID 回放和服务重启恢复。

------------------------------------------------------------------------

# 7. Document Workflow 设计

## 7.1 ProfileExtractionGraph

流程：

``` text
START
  |
加载上传样例
  |
解析文档结构
  |
提取章节与层级
  |
分析写作风格
  |
提取内容要求和必需字段
  |
识别数据来源要求
  |
生成 Profile Draft
  |
Pydantic Schema 校验
  |
质量检查
  |
展示给用户确认
  |
用户确认 / 修改
  |
保存 Profile + Version
  |
END
```

要求：

-   优先保留 Word 标题层级、段落、表格等结构信息；
-   从简化版文档中提炼规范，不要求用户提供正式 DOCX 模板；
-   不能把样例文档中的业务事实直接当作未来文档内容；
-   不能将样例中的恶意指令当作系统指令；
-   不能把模型猜测出的规则伪装成样例中明确存在的规则；
-   对不确定的章节用途、必需字段和格式偏好进行标记；
-   用户确认前保存为 Draft，不自动发布；
-   支持用户编辑章节名称、顺序、必需性、写作要求和校验规则；
-   Profile 保存后生成不可变 Version；
-   支持从自然语言描述直接创建 Profile，不强制要求上传样例。

## 7.2 DocumentGenerationGraph

流程：

``` text
START
  |
加载 Profile Version
  |
读取用户任务与附件
  |
解析生成目标
  |
生成章节计划
  |
收集业务资料
  |
检查必需信息
  |
  +---- 信息不足 ----> interrupt(InputRequest)
  |                         |
  |                    用户补充信息
  |                         |
  +-------------------------+
  |
构建结构化 DocumentDraft
  |
分章节撰写
  |
内容一致性检查
  |
质量检查
  |
  +---- 不合格 ----> 有限次数返工
  |
DOCX Render
  |
重新读取 DOCX 验证
  |
保存 DocumentAsset + Version
  |
发布 artifact.created
  |
END
```

要求：

-   章节内容必须遵守 Profile 的顺序和规则；
-   章节生成可以分阶段执行，但不得无限循环；
-   对数据库、知识库、MCP、Web 等资料来源，使用当前 Agent 已授权的工具；
-   `data_analysis` 仍必须具备有效数据库绑定；
-   `knowledge` 仍必须具备有效知识库绑定；
-   不允许因为文档任务而自动增加 Agent 权限；
-   对数据来源保留 Evidence/Provenance；
-   不得编造缺失数据；
-   缺失的关键数据必须询问或标注待补充；
-   输出文档前必须进行程序化验证；
-   生成文件失败必须如实失败，不能只返回一段"已生成"的文本。

## 7.3 DocumentEditGraph

流程：

``` text
START
  |
加载 Document + 指定 Version
  |
解析 DOCX 结构
  |
理解用户修改要求
  |
定位目标章节/段落/表格
  |
生成修改计划
  |
检查修改范围
  |
生成 Patch
  |
应用 Patch 到新版本
  |
比较修改前后结构
  |
检查非目标章节是否被意外改变
  |
DOCX 验证
  |
保存 Version + 变更说明
  |
发布 artifact.created
  |
END
```

要求：

-   不覆盖源文件和历史版本；
-   默认生成新版本；
-   优先进行局部修改，不要为了改第三章而重写整篇文档；
-   修改目标不明确时询问用户；
-   对大范围重写或可能破坏原有内容的操作，先展示修改计划；
-   保存修改摘要和版本差异；
-   能够追溯每个版本由哪个 Run 创建。

------------------------------------------------------------------------

# 8. Document Engine：真实文件处理能力

Document Engine 是普通 Python 服务，不是 Agent
Loop，也不负责判断用户意图。

建议拆分：

``` text
backend/app/services/documents/
  __init__.py
  parser.py
  profile_service.py
  renderer.py
  editor.py
  validator.py
  storage.py
  schemas.py
  provenance.py
```

实际命名可以调整，但职责要清楚。

## 8.1 Parser

优先复用现有依赖：

-   DOCX：`python-docx`
-   PDF：`pypdf`
-   PPTX：`python-pptx`
-   XLSX：`openpyxl`

本次优先把 DOCX 结构化解析做好。PDF/PPTX/XLSX
作为参考资料和数据输入，不要求全部支持生成。

DOCX Parser 至少能够提取：

-   标题；
-   Heading 1/2/3 层级；
-   段落；
-   列表；
-   表格；
-   基础样式信息；
-   文档元信息。

不要只调用 `KnowledgeService.extract_text()`
后把整篇纯文本交给模型。知识库抽取可以作为兜底，但文档规范提炼需要尽可能保留结构。

扫描件 PDF 如果没有 OCR 结果，应明确提示无法提取，不得假装已读取。

## 8.2 DocumentDraft

模型生成内容必须先形成结构化草稿，再渲染 DOCX。

建议至少包含：

-   title；
-   subtitle / metadata；
-   abstract / executive_summary（可选）；
-   sections；
-   paragraphs；
-   lists；
-   tables；
-   source references；
-   validation metadata。

每个 Section 至少有稳定 id、标题、顺序和内容节点。

不要让模型直接拼接任意 Python、HTML 或 DOCX XML。

## 8.3 DOCX Renderer

使用已有 `python-docx` 实现。

至少支持：

-   A4 页面；
-   页面边距；
-   标题层级；
-   正文段落；
-   有序/无序列表；
-   表格；
-   页眉页脚基础能力；
-   基础分页控制；
-   中文字体设置；
-   合理的段落间距和行距；
-   文件名清理；
-   生成后重新打开验证。

格式要求由 Profile 驱动，不能在代码里写死一种技术报告模板。

本阶段不要求像素级复刻复杂 Word
模板，但必须输出结构清晰、排版整洁、可正常打开的企业文档。

## 8.4 编辑器与 Patch

局部编辑应基于结构化节点和明确锚点，不要依赖不稳定的全文字符串替换。

Patch 操作需要明确：

-   target section / paragraph / table；
-   operation；
-   before / after；
-   reason；
-   source/evidence（如适用）。

如果目标无法可靠定位，返回需要澄清，不要猜测修改位置。

## 8.5 Validator

至少包含两类验证：

### 程序化验证

-   DOCX 文件存在且非空；
-   DOCX 能被 `python-docx` 重新打开；
-   必需章节全部存在；
-   章节顺序正确；
-   必需字段已填充或被明确标记；
-   表格行列结构合法；
-   文件版本与 DB 记录一致；
-   内容没有意外为空；
-   版本文件未覆盖历史版本。

### 语义质量验证

-   是否偏离用户目标；
-   是否遗漏关键要求；
-   是否有明显重复；
-   是否有数据前后矛盾；
-   是否存在无来源的关键数字或结论；
-   是否符合 Profile 的语气和章节要求。

语义质量检查不能替代程序化验证。质量检查结果要保存为结构化
`validation_report`。

------------------------------------------------------------------------

# 9. 文档存储、版本和 Artifact

## 9.1 持久化文件位置

不要把长期文档资产只保存在可能随会话清理的临时目录。

建议在现有 `AGENT_FILES_ROOT` 下增加用户隔离的文档资产目录，例如：

``` text
AGENT_FILES_ROOT/
  {user_id}/
    documents/
      {document_id}/
        v1.docx
        v2.docx
        preview-v1.json
        preview-v2.json
```

必须通过统一 Storage Service 生成路径，拒绝用户传入任意绝对路径和 `..`。

会话上传附件仍使用现有 Session Workspace。Profile
提炼时读取上传附件，最终只保存 Profile
结构和必要来源元数据，不要求把样例文件永久复制进 Profile。

## 9.2 版本策略

-   新文档创建 DocumentAsset + Version 1；
-   修改已有文档创建 Version N+1；
-   历史版本不可覆盖；
-   保存文件前先写临时文件；
-   验证通过后再原子移动到正式版本路径；
-   DB 事务失败时处理临时文件和孤儿文件；
-   `content_hash` 用于完整性校验；
-   版本记录保存 Profile Version、Run ID、变更摘要和验证报告。

## 9.3 文档 API

建议新增独立文档 API，例如：

``` text
GET    /api/v2/document-profiles
POST   /api/v2/document-profiles
GET    /api/v2/document-profiles/{id}
PUT    /api/v2/document-profiles/{id}
GET    /api/v2/document-profiles/{id}/versions
POST   /api/v2/document-profiles/{id}/publish

GET    /api/v2/documents
GET    /api/v2/documents/{id}
GET    /api/v2/documents/{id}/versions
GET    /api/v2/documents/{id}/versions/{version}/preview
GET    /api/v2/documents/{id}/versions/{version}/download
```

具体 API 命名可以遵循当前项目风格，但必须覆盖 Profile
CRUD、版本、Document 列表、详情、预览和下载。

Profile 提炼、文档生成、文档编辑通过现有 `POST /api/v2/runs` 的
task_mode 发起，不要为每个工作流另建一套异步任务系统。

## 9.4 权限

-   普通用户只能读取、修改自己的 private Profile 和 Document；
-   organization Profile 必须经过授权发布；
-   管理员可以管理组织共享规范；
-   文档下载必须进行数据库归属校验；
-   不允许仅凭 document_id 或相对路径访问其他用户文件；
-   不暴露服务器绝对路径；
-   删除 Profile 不得破坏已经生成的 Document Version；
-   删除 Document 时必须明确处理文件和历史版本，不允许留下不受控的文件。

------------------------------------------------------------------------

# 10. Runtime、事件和可观测性集成

## 10.1 Run 输入持久化

在现有 `agent_runs.input_payload` 中扩展可恢复的执行上下文，建议包含：

``` json
{
  "messages": [],
  "use_memory_context": "",
  "db_info": {},
  "execution_context": {
    "task_type": "document_generate",
    "task_mode": "auto",
    "document_profile_id": 12,
    "document_profile_version": 2,
    "document_id": null,
    "attachment_refs": [],
    "router_version": 1
  }
}
```

不要将 API Key、数据库密码或其他密钥写入快照。

更新：

-   `RunCreate`；
-   `AgentRuntimeService._start`；
-   `_begin_run`；
-   `RunJob`；
-   `RunWorker._job_from_row`；
-   `_drive`；
-   request hash；
-   Run 配置快照。

## 10.2 事件

至少支持：

  事件                                    用途
  --------------------------------------- ------------------
  `task.routed`                           展示任务识别结果
  `document.profile_extraction.started`   开始提炼规范
  `document.profile_draft.created`        规范草稿完成
  `document.profile_saved`                规范已保存
  `document.generation.started`           开始生成文档
  `document.section.completed`            章节完成
  `document.validation.started`           开始验证
  `document.validation.completed`         验证结果
  `input.required`                        等待用户补充
  `input.received`                        用户已补充
  `artifact.created`                      文档产物生成
  `document.version.created`              新版本生成

事件必须经过内部 RuntimeEvent → EventBus → 数据库 → V2 Event Protocol →
SSE → 前端归约的完整链路。

不要只在前端本地拼接一个"已生成文档"的假状态。

`artifact.created` 数据至少包含：

-   document_id；
-   document_version_id；
-   version_no；
-   title；
-   filename；
-   mime_type；
-   size；
-   profile_id / profile_version（如适用）；
-   download API URL；
-   preview API URL。

不得包含服务器绝对路径或敏感文件系统信息。

## 10.3 Langfuse 与指标

在现有 Run Trace 下增加合理的子 Span：

-   task_route；
-   document_parse；
-   profile_extract；
-   profile_validate；
-   document_data_collect；
-   document_draft；
-   document_section_write；
-   document_quality_check；
-   document_render；
-   document_edit；
-   document_version_save。

建议记录：

-   task_type；
-   Profile ID/Version；
-   Document ID/Version；
-   输入文件数量；
-   输出文件大小；
-   模型调用次数和 Token；
-   数据收集耗时；
-   生成耗时；
-   校验结果；
-   用户补充次数；
-   失败阶段与错误类型。

不要记录完整敏感业务文档正文或密钥。

------------------------------------------------------------------------

# 11. 前端交互建设

## 11.1 智能体对话仍然是主入口

不要强制在 Composer 中加入复杂的文档配置表单。

默认使用
`task_mode=auto`。用户可以自然语言发起任务，也可以通过明确的文档操作入口显式设置
task_mode。

示例：

-   "帮我总结这份 Word" → 普通 Agent 分析；
-   "根据这份样例建立项目总结规范" → Profile 提炼；
-   "按照项目总结规范生成本季度报告" → 文档生成；
-   "修改上次报告的第三章" → 文档编辑。

## 11.2 对话中的文档任务卡片

在 `blocks.ts` 中扩展新的消息块类型，至少考虑：

-   `document_artifact`
-   `document_profile_draft`
-   `input_request`
-   必要的文档任务状态块

在 `WorkspaceHome.vue` 中渲染对应组件。

### Document Artifact Card

显示：

-   文档名称；
-   文件类型；
-   版本；
-   使用的 Profile；
-   生成状态；
-   预览；
-   下载；
-   继续修改。

点击预览时使用现有右侧 Preview 交互，不要把整个 DOCX 内容塞进普通
Markdown 气泡。

### Profile Draft Card

显示：

-   Profile 名称；
-   章节列表；
-   必需章节；
-   写作规则；
-   缺失/不确定规则；
-   确认保存；
-   修改规范。

用户确认后再持久化发布，不能让 Agent
未经确认自动创建一个全局可用的规则。

### Input Request Card

根据 `input_schema` 动态渲染：

-   文本；
-   日期；
-   数字；
-   单选；
-   多选；
-   可选备注。

提交后调用 `POST /runs/{run_id}/input`，并重新订阅原 Run 事件流。

不能用普通聊天消息重新创建一个新 Run 来假装恢复原任务。

## 11.3 文档中心

新增用户可访问的文档中心页面，建议路由
`/documents`，至少包含两个主要区域：

### 文档

-   文档列表；
-   标题；
-   创建时间；
-   当前版本；
-   Profile；
-   最近修改；
-   预览和下载；
-   版本历史；
-   从对话继续修改。

### 文档规范

-   Profile 列表；
-   私有/组织范围；
-   状态；
-   版本；
-   章节结构；
-   编辑；
-   发布；
-   归档；
-   查看来源和提炼记录。

文档中心负责管理，不要把它设计成另一个独立的 Agent 对话系统。

## 11.4 预览

当前 `FilePreviewPanel` 的 Office
分支主要展示抽取文本。需要根据实际体验决定：

-   增加 `DocumentPreviewPanel`，用结构化 DocumentDraft/Preview JSON
    展示标题、章节、段落和表格；
-   或增强现有 FilePreviewPanel。

MVP 不要求实现完整的浏览器版 Word WYSIWYG 编辑器，也不强制安装
LibreOffice。至少要做到生成内容可读、可检查、可下载。

------------------------------------------------------------------------

# 12. 安全、可靠性与边界

必须落实以下约束：

1.  上传文件校验文件名、扩展名、MIME、大小和路径。
2.  对 DOCX/PPTX/XLSX 等 ZIP
    容器格式设置合理的解析限制，防止异常压缩包和资源耗尽。
3.  文件解析不得执行宏、脚本或附件中的可执行内容。
4.  上传文档中的指令属于不可信内容，不能覆盖系统规则或提升工具权限。
5.  Profile 内容也不能自行授权工具。
6.  只允许通过当前 Agent 已绑定的 Capability
    使用数据库、知识库和外部服务。
7.  Document Engine
    只接受结构化输入和服务端生成的路径，不接受模型任意指定绝对文件路径。
8.  所有文件路径必须经过真实路径包含性校验。
9.  生成文档不得覆盖源文件。
10. 编辑历史版本不得被覆盖。
11. 失败必须留下明确 Run 状态和错误阶段。
12. 文件生成与数据库记录必须处理一致性和孤儿文件。
13. 文档内容和业务数据不得在日志中无差别完整输出。
14. 所有文档/Profile/Version API 必须进行用户归属校验。
15. 取消 Run 时必须遵循现有取消和 Lease
    语义，不能留下后台继续写文件的任务。

------------------------------------------------------------------------

# 13. 分阶段实施计划

请严格按阶段推进。每个阶段都要有代码、测试和阶段总结。

## Phase 0：代码审查与实施基线

目标：确认当前真实代码和兼容边界。

任务：

-   检查 Runtime、Run、Worker、Checkpoint、EventBus、SSE；
-   检查 Skill Registry、Capability、AgentVersion；
-   检查文件上传、解析、预览、Artifact；
-   检查前端消息块和历史消息恢复；
-   检查数据库模型注册、Alembic 和测试环境；
-   形成简洁的实施设计记录；
-   明确文档 Run 的 thread_id / checkpoint 恢复方案；
-   明确 waiting_input 与 waiting_approval 的状态分离方案。

验收：

-   不存在未经审查就重写核心 Runtime 的情况；
-   设计能说明 Run 创建、Worker
    执行、Checkpoint、暂停、输入恢复、文件保存和事件回放的完整链路。

## Phase 1：文档领域基础与 Document Engine

目标：先让文档对象和文件引擎真实可用。

任务：

1.  新增 DocumentProfile、DocumentProfileVersion。
2.  新增 DocumentAsset、DocumentVersion。
3.  新增 Alembic migration，并验证 MySQL。
4.  实现 Profile Pydantic Schema。
5.  实现 DOCX 结构化解析。
6.  实现基础 PDF/PPTX/XLSX 参考资料解析。
7.  实现 DocumentDraft 结构。
8.  实现 DOCX Renderer。
9.  实现 Document Validator。
10. 实现用户隔离的 Document Storage。
11. 实现 Document/Profile Repository 与 Service。
12. 实现 Profile/Document 基础 API。

验收：

-   能解析一份包含标题、段落、表格的 DOCX；
-   能将结构化 DocumentDraft 渲染成可打开的 DOCX；
-   能重新读取生成的 DOCX 并验证必需结构；
-   能保存 DocumentAsset 和 Version；
-   不同用户不能读取对方的 Profile 和 Document；
-   不覆盖历史版本；
-   相关单元测试通过。

## Phase 2：Durable User Input 与文档 LangGraph

目标：建立可恢复的文档执行图。

任务：

1.  新增 ProfileExtractionGraph。
2.  新增 DocumentGenerationGraph。
3.  新增 DocumentEditGraph。
4.  接入现有模型配置和 Agent Runtime Context。
5.  使用现有工具权限和数据源绑定。
6.  实现 `waiting_input` 状态。
7.  实现 RunInputRequest 持久化。
8.  实现 `/runs/{id}/input`。
9.  实现输入提交后的原 Run 恢复。
10. 实现 Worker 重启后的等待输入恢复。
11. 实现文档图的 Checkpoint 隔离。
12. 增加节点级错误处理、预算和有限重试。
13. 增加文档阶段事件。

验收：

-   文档生成遇到缺失信息时 Run 进入 `waiting_input`；
-   用户提交后恢复原 Run，而不是新建 Run；
-   服务重启后仍能恢复等待中的文档任务；
-   工具审批与普通输入请求互不干扰；
-   失败、取消、超时和预算超限状态正确；
-   不会因为恢复而重复写入同一文档版本；
-   文档图与普通聊天图不会发生 Checkpoint 状态冲突。

## Phase 3：Task Router 与现有 Runtime 集成

目标：让文档任务自然进入对话。

任务：

1.  RunCreate 增加 task_mode、Profile ID、Document ID 等可选字段。
2.  实现 TaskRouter。
3.  实现显式模式、上下文、规则识别和歧义回退。
4.  只在必要时调用模型分类，不为普通消息增加固定分类模型调用。
5.  路由结果持久化。
6.  更新 `_start`、`_begin_run`、`RunJob`、`_job_from_row`、`_drive`。
7.  文档 Run 走专用 DocumentGraph。
8.  普通 Run 保持原 General Agent 路径。
9.  更新幂等 request hash。
10. 保证旧客户端不传 task_mode 时仍可正常使用。

验收：

-   普通聊天不受影响；
-   上传 Word 并总结不会误触发文档生成；
-   明确要求生成 Word 能正确进入 DocumentGenerationGraph；
-   明确要求提炼规范能进入 ProfileExtractionGraph；
-   明确要求局部修改能进入 DocumentEditGraph；
-   不明确时能够澄清；
-   路由决策能在 Trace 和事件中追踪。

## Phase 4：对话 Artifact、Input Request 与预览交互

目标：让用户能在聊天中完成任务并拿到结果。

任务：

1.  补齐 `artifact.created` 事件全链路。
2.  增加文档 Artifact Card。
3.  增加 Profile Draft Card。
4.  增加 Input Request Card。
5.  增加 `waiting_input` 前端状态。
6.  支持提交输入并恢复原 Run。
7.  支持历史事件回放后的卡片恢复。
8.  接入文档预览、下载和继续修改。
9.  保留现有 ToolStep、Plan、ApprovalPanel 行为。

验收：

-   文档生成后无需离开对话即可看到文档卡片；
-   能预览和下载；
-   能查看版本；
-   缺少信息时出现正确的输入界面；
-   提交后原任务继续；
-   刷新页面或重新打开会话，历史文档产物仍然可见；
-   不会把 artifact 事件误渲染成普通文本。

## Phase 5：文档中心与规范管理

目标：让文档规范和文档资产可长期复用。

任务：

1.  新增 `/documents` 页面。
2.  增加 Profile 列表和详情。
3.  增加 Profile 编辑、版本和发布。
4.  增加 Document 列表和详情。
5.  增加 Document Version 历史。
6.  增加从文档中心打开对话继续修改的入口。
7.  实现私有与组织共享权限。
8.  将 Profile 提炼结果与源样例元数据关联。
9.  增加 Profile 归档和安全删除策略。

验收：

-   用户可以管理自己的文档规范；
-   新对话能复用已保存 Profile；
-   Profile 修改生成新版本；
-   组织共享规范不能被普通用户越权修改；
-   文档版本可以追溯创建 Run 和使用的 Profile Version。

## Phase 6：端到端质量、稳定性与交付

目标：验证完整业务价值，而不是只验证 API 返回 200。

任务：

-   完成全部测试场景；
-   完成安全和权限测试；
-   完成 Worker 重启与恢复测试；
-   完成前端生产构建；
-   完成数据库迁移验证；
-   完成关键日志、Trace 和指标；
-   整理部署注意事项和使用说明；
-   输出实际完成情况和未完成项。

------------------------------------------------------------------------

# 14. 端到端验收场景

以下场景必须自动化测试或提供可重复的 E2E 测试脚本。

## 场景 A：普通文档分析

用户上传 `项目总结.docx`：

> 帮我总结一下这份文件的主要内容。

预期：

-   进入 General Agent；
-   读取附件；
-   返回总结；
-   不生成 DocumentAsset；
-   不误触发 DocumentGenerationGraph。

## 场景 B：从样例提炼规范

用户上传一份简化版报告：

> 根据这份文件帮我提炼一个可复用的项目总结报告规范。

预期：

-   进入 ProfileExtractionGraph；
-   提取章节、写作风格和内容要求；
-   生成 Profile Draft；
-   用户可以修改；
-   用户确认后保存 Profile Version；
-   新对话能够复用该 Profile。

## 场景 C：自然语言创建规范

用户没有上传文件：

> 我需要一套月度工作总结规范，包含工作完成情况、关键成果、问题和下月计划。

预期：

-   能直接创建 Profile Draft；
-   不强制要求上传 DOCX；
-   用户确认后保存。

## 场景 D：完整文档生成

用户：

> 按照项目总结报告规范生成本季度项目总结，数据从当前 Agent
> 绑定的数据库中获取。

预期：

-   正确识别 Profile；
-   检查数据库资源绑定；
-   通过已授权工具获取数据；
-   生成结构化 DocumentDraft；
-   输出 DOCX；
-   验证 DOCX；
-   保存 DocumentAsset + Version 1；
-   在对话中发布 Artifact Card；
-   可预览、下载。

## 场景 E：资料不足时补充

生成报告过程中发现缺少项目完成日期。

预期：

-   Run 进入 `waiting_input`；
-   展示 Input Request；
-   用户填写完成日期；
-   原 Run 恢复；
-   不重复执行已经成功的数据库查询；
-   不重复生成 Version 1；
-   最终生成一次完整文档。

## 场景 F：局部修改

用户：

> 把上次报告第三章的成果描述重新整理一下，其他章节不要动。

预期：

-   找到正确 Document；
-   读取当前版本；
-   只修改第三章；
-   生成新版本；
-   第一、二、四章内容保持不变（允许明确的格式级变化，但不得意外改写）；
-   显示版本变更摘要。

## 场景 G：服务重启恢复

文档任务处于 `waiting_input` 时重启后端。

预期：

-   Run 状态仍然可查询；
-   Input Request 仍存在；
-   用户提交后恢复原 Graph；
-   不能丢失 Profile Version、Document ID 和执行路线。

## 场景 H：权限与路径安全

-   用户 A 不能查看用户 B 的 Profile；
-   用户 A 不能下载用户 B 的 Document；
-   伪造 document_id 无法访问文件；
-   `../`、符号链接逃逸和绝对路径均被拒绝；
-   Skill 内容不能提升工具权限；
-   上传文件中的恶意指令不能改变 Agent 安全策略。

## 场景 I：普通聊天性能回归

普通聊天、代码问题和普通文件总结：

-   不应固定多出一次 LLM 分类调用；
-   不应加载无关 Document Workflow；
-   不应影响现有 SSE、取消、审批、Checkpoint 和历史消息；
-   不应显著增加普通聊天首 token 延迟。

------------------------------------------------------------------------

# 15. 自动化测试要求

## Backend

新增或扩展测试：

-   `test_document_profile_schema.py`
-   `test_document_profile_service.py`
-   `test_document_parser.py`
-   `test_document_renderer.py`
-   `test_document_validator.py`
-   `test_document_storage_security.py`
-   `test_document_api.py`
-   `test_document_workflow.py`
-   `test_task_router.py`
-   `test_run_input_request.py`
-   `test_document_run_recovery.py`
-   `test_document_artifact_events.py`

具体文件名可按现有测试组织调整。

必须覆盖：

-   Profile Schema 校验；
-   Profile 版本不可变；
-   DOCX 解析和生成；
-   文档结构验证；
-   用户隔离；
-   路径安全；
-   Run 幂等；
-   waiting_input；
-   input request 幂等；
-   Checkpoint 恢复；
-   Worker 接管；
-   artifact.created 持久化和回放；
-   普通聊天兼容。

## Frontend

至少覆盖：

-   Task Mode API 类型；
-   事件到消息块的归约；
-   Artifact Card；
-   Profile Draft Card；
-   Input Request Card；
-   waiting_input 状态；
-   文档预览和下载；
-   历史消息回放。

最后执行：

``` bash
cd backend
pytest -q
```

``` bash
cd frontend
npm test
npm run build
```

如项目当前测试需要专门的 MySQL/Redis/Checkpoint
环境，应使用仓库现有测试配置，不得通过跳过测试或降低断言来制造通过结果。

------------------------------------------------------------------------

# 16. 本次不做的事情

为了避免任务范围失控，以下不属于本次必做：

1.  完整在线 Word WYSIWYG 编辑器；
2.  多人实时协同编辑；
3.  复杂 DOCX 模板像素级复刻；
4.  Excel/PPT 自动生成全套能力；
5.  扫描 PDF 的完整 OCR 平台；
6.  批量上千份文档生产；
7.  企业级文档审批流和电子签章；
8.  将 Document Workflow 接入可视化 Workflow Canvas；
9.  重构整个 Agent Runtime；
10. 重新建设 Skill Marketplace；
11. 自动创建一个独立的 Document Agent Runtime；
12. 为了文档功能引入新的分布式任务系统。

如果实施过程中发现某项能力是完成核心闭环的必要条件，应先说明原因，再做最小范围实现。

------------------------------------------------------------------------

# 17. 最终交付清单

Codex 完成后必须交付：

## 17.1 代码

-   Document Profile 数据模型和版本；
-   Document Asset / Version 数据模型；
-   Document Parser；
-   Document Renderer；
-   Document Editor / Patch；
-   Document Validator；
-   Document Storage；
-   ProfileExtractionGraph；
-   DocumentGenerationGraph；
-   DocumentEditGraph；
-   TaskRouter；
-   Durable User Input；
-   Runtime 集成；
-   文档 API；
-   文档事件；
-   对话 Artifact Card；
-   Input Request Card；
-   文档中心；
-   Alembic migration；
-   自动化测试。

## 17.2 文档

至少新增：

-   `docs/document-intelligence/architecture.md`
-   `docs/document-intelligence/implementation-status.md`
-   `docs/document-intelligence/operations.md`
-   `docs/document-intelligence/api.md`

内容以真实代码为准，不允许提前写成"全部实现"但代码不存在。

## 17.3 最终实施报告

必须输出：

1.  本次实际完成了什么；
2.  新增和修改了哪些核心模块；
3.  一次文档生成任务的真实调用链；
4.  TaskRouter 的判断方式；
5.  Profile 和 Skill 的关系；
6.  DocumentGraph 如何接入现有 Runtime；
7.  `waiting_input` 如何持久化和恢复；
8.  DOCX 如何生成、验证和保存；
9.  Document Version 如何管理；
10. 前端对话如何展示 Artifact；
11. 数据库迁移情况；
12. 自动化测试结果；
13. 尚未完成的能力和已知限制；
14. 如何部署、如何手工验证完整流程。

------------------------------------------------------------------------

# 18. 最终架构验收原则

这次改造最终应当形成下面的分工：

  层次               核心职责
  ------------------ -------------------------------------------
  Agent 对话         用户统一任务入口
  TaskRouter         判断任务类型和执行路线
  DeepAgents         通用智能推理、工具使用和任务执行能力
  LangGraph          文档工作流编排、状态流转、中断与恢复
  Document Profile   结构化定义文档规范
  Skill              固化通用文档执行方法
  Document Engine    真实解析、生成、修改和验证文件
  Document Store     文档资产与版本持久化
  Agent Runtime      Run、Worker、Checkpoint、事件、预算和恢复
  Document Center    Profile 和 Document 管理界面

最终用户只需要表达目标：

-   "按照这份样例建立一套报告规范。"
-   "按照之前的规范生成本季度总结。"
-   "数据不够的话先问我。"
-   "第三章重新写，其他内容不要动。"
-   "把这份报告保存为新版本。"

平台负责识别、规划、执行、收集信息、生成、验证和交付。

**本次改造的最终判断标准不是 Agent
能不能生成一段看起来像报告的文字，而是它能否可靠地完成一项完整的文档生产任务，并且用户能够复用规范、检查结果、追溯版本和继续修改。**

这才是本次文档智能能力建设真正要达到的产品价值。
