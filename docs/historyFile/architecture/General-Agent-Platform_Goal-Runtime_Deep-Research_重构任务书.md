# General Agent Platform：Goal Runtime 与 Deep Research 重构任务书

**用途：** 交给 Codex / Trae 执行\
**仓库：** `xxiaoxiong/general-agent-platform`\
**审查基线：** `main`，Commit
`0a975bb29e7e33e1beb8c8ae39c6ebe04891251e`\
**目标：** 将 `/goal` 从"长时间运行 +
有限返工"升级为目标驱动的持续执行闭环；将现有分散的研究能力升级为可规划、可追溯、可核验、可动态补充调查的
Deep Research。

------------------------------------------------------------------------

## 1. 改造目标与原则

对标公开产品行为：

-   **ChatGPT Deep Research：**
    自主拆解研究问题、动态调整计划、跨来源搜集和阅读资料、评估证据、识别缺口、生成带引用的报告，并允许用户干预。
-   **TRAE Goal：** 用户定义目标和完成条件；Agent
    执行后检查是否达成，未达成则继续推进，直到完成、阻塞或触发安全停止条件。

不要求复刻闭源产品内部实现，只对齐可观察到的能力。

### 必须遵循的架构原则

1.  Goal 是目标控制层，不是新的 Agent Runtime。
2.  Deep Research 是专业研究策略，不是另一套底层 Agent Runtime。
3.  LangGraph 管理外层目标/研究状态流转；DeepAgents 负责单轮 Agent
    执行。
4.  Work Orchestrator 负责任务拆分与执行器调度；Goal Runtime
    负责目标是否达成。
5.  Todo、模型自述、报告中的"目标核对"标题都不能单独证明任务完成。
6.  长程执行必须支持持久化、Checkpoint、恢复、暂停、取消和用户干预。
7.  不允许通过彻底解除预算限制实现长任务；必须保留全局成本、时长、并发、工具调用和资源安全边界。
8.  优先复用现有模块，避免无关重构和重复建设。

------------------------------------------------------------------------

## 2. 当前代码基础与改造重点

以审查基线为参考，实施前必须重新检查最新仓库，确认代码是否变化。

### Goal 相关模块

  --------------------------------------------------------------------------------------------
  文件                                                     现有职责 / 本次关注点
  -------------------------------------------------------- -----------------------------------
  `backend/app/services/agent_runtime/goal.py`             Goal 提示词与执行协议

  `backend/app/services/agent_runtime/completion.py`       完成检查、返工和续做

  `backend/app/services/agent_runtime/service/stream.py`   Run 执行、预算、恢复、事件流

  `backend/app/services/assistant/task_contract.py`        任务契约

  `backend/app/services/assistant/verification.py`         确定性验收

  `backend/app/services/agent_runtime/steering.py`         执行中追加要求

  `backend/app/api/run/core.py`                            Run 创建、Goal 快照、Steering API
  --------------------------------------------------------------------------------------------

当前主要问题：

-   Goal 主要依赖提示词、Todo 和 Completion
    Middleware，缺少完整的外层目标监督循环。
-   放宽常规预算不等于实现持续目标管理；Completion 返工仍有轮次上限。
-   验收对
    Todo、最终回答格式和工具痕迹依赖较多，无法充分证明原始目标完成。
-   目标、验收条件、执行轮次和证据尚未形成完整的持久化版本关系。
-   Steering 注入消息不等于持久化修改目标契约。

### Research 相关模块

  ------------------------------------------------------------------------------------------------
  模块                                                   现有基础 / 本次关注点
  ------------------------------------------------------ -----------------------------------------
  `backend/app/services/tools/web_tools.py`              SearXNG、降级搜索、网页抓取、来源元数据

  `backend/app/services/agent_runtime/team_runtime.py`   多智能体协作
  及 `team/`                                             

  `backend/app/services/work_orchestrator/`              复合任务规划和执行调度

  `completion.py`、`verification.py`                     完成检查与基础引用校验

  Knowledge、附件、历史检索、文档、Sandbox               研究资料和执行基础设施
  ------------------------------------------------------------------------------------------------

当前主要问题：

-   缺少持久化 Research Plan / Research State。
-   URL 被搜索到，不代表正文已读取，更不代表它支持某项结论。
-   引用检查偏向 URL 是否出现过，缺少 Claim-Evidence 逐项关联。
-   网页读取对 JS 页面、长文档、PDF、表格、图表和源码结构支持不足。
-   Team 尚未形成研究分工、共享证据、独立复核和研究质量门禁的完整闭环。

### 必须复核的集成限制

审查基线中，`api/run/core.py` 对 `goal_mode + team_mode`
有显式限制；`work_orchestrator/integration.py` 对 Goal
模式存在跳过规划的分支。实施前重新确认现状，并解决 Goal 与 Team、Work
Orchestrator、Document Workflow
的组合问题。不能简单删除限制，必须同时处理状态、预算、取消、恢复和验收一致性。

------------------------------------------------------------------------

## 3. 目标架构

``` text
用户请求
  |
Decision Engine
  |-- 普通任务 --------------------------> 现有执行路径
  |
  +-- Goal / Deep Research
          |
       Goal Runtime / Supervisor
          |-- Goal Contract
          |-- Progress Tracker
          |-- Goal Evaluator
          |-- Replan / Continue / Stop
          |
       Work Orchestrator
          |-- DeepAgents 单轮执行
          |-- Team Runtime
          |-- Document Workflow
          |-- Software / Sandbox
          +-- Deep Research Profile
                  |-- Research Planner
                  |-- Research Executor
                  |-- Evidence Store
                  |-- Evidence Verifier
                  +-- Research Synthesizer
```

职责边界：

  模块                负责什么
  ------------------- --------------------------------------------------
  Decision Engine     识别任务类型、所需能力和执行策略
  Goal Runtime        目标契约、执行轮次、进度、验收、续跑、停止和恢复
  Work Orchestrator   拆分工作包并调度执行器
  DeepAgents          单轮 Agent 推理和工具执行
  Team Runtime        多成员分工、并行执行和汇总
  Research Profile    研究计划、来源搜集、证据核验和报告综合
  Evidence Store      来源、证据片段、主张及支持/反驳关系
  Goal Evaluator      根据验收条件与证据判断是否完成

------------------------------------------------------------------------

## 4. Phase 1：Goal Contract（目标契约）

复用并升级现有 `task_contract.py`，避免重复建设第二套契约系统。

目标契约至少包含：

``` json
{
  "goal_id": "stable-id",
  "run_id": "run-id",
  "revision": 1,
  "objective": "用户原始目标",
  "acceptance_criteria": [
    {
      "id": "AC-001",
      "description": "可验证的验收要求",
      "verification_type": "test|artifact|browser|semantic|evidence|manual",
      "required": true,
      "status": "pending",
      "evidence_refs": []
    }
  ],
  "constraints": [],
  "deliverables": [],
  "stop_conditions": []
}
```

要求：

-   完整保存原始目标，不以 Prompt 截断代替持久化。
-   每条验收条件有稳定 ID，区分必需项和可选项。
-   支持 revision；用户修改目标时形成新版本并记录变更。
-   模型可以提出计划调整，但不能静默删除或弱化用户的必需条件。
-   关键输入缺失或目标存在歧义时允许向用户提问，不得猜测。
-   数据库变更提供 Alembic migration，兼容旧
    Run，并遵循现有用户隔离和权限规则。

**交付：** 数据模型、迁移、契约解析/更新服务、单元测试。

------------------------------------------------------------------------

## 5. Phase 2：Goal Supervisor（持续执行闭环）

### 5.1 外层状态流转

建议使用 LangGraph 或项目现有合适的图执行机制：

``` text
LOAD_GOAL
  -> PLAN_OR_REPLAN
  -> DISPATCH_WORK
  -> COLLECT_RESULTS
  -> EVALUATE_GOAL
       |-- 达成 ------------> FINALIZE
       |-- 有可解决缺口 ----> CONTINUE -> PLAN_OR_REPLAN
       |-- 等待用户输入 ----> WAITING_INPUT
       |-- 预算/安全边界 ---> PAUSED_OR_STOPPED
       +-- 不可恢复失败 ----> FAILED_WITH_PARTIAL_RESULTS
```

要求：

-   每轮有独立 `iteration_id`，关联 Goal、工作包和执行器。
-   单轮 Agent 结束不等于整个 Goal 结束。
-   每轮结束收集结构化结果，不只依赖自然语言总结。
-   未通过项进入下一轮；已通过项保留证据，避免无意义重复执行。
-   支持暂停、取消、等待输入、恢复和失败后续跑。
-   设置最大轮次、最大成本、模型/工具调用、最长时间、用户配额等全局安全预算。
-   每轮软预算耗尽可以持久化并启动下一轮；全局安全预算始终有效。
-   检测无进展、重复调用、反复失败和计划振荡。
-   高风险或不可逆操作继续遵循审批策略。
-   明确区分
    `completed / blocked / paused / failed / cancelled`，禁止把阻塞或预算耗尽伪装成成功。

### 5.2 Goal Evaluator：确定性 + 语义验收

确定性验收包括文件/产物存在、格式、构建、测试、Lint、Sandbox
结果、API/页面访问、必需执行记录等。

语义验收包括目标覆盖度、交付内容质量、研究结论证据支持、文档要求满足度、遗漏和矛盾等。

模型可用于语义评审，但应输出结构化结果：通过项、未通过项、证据、原因和下一步。重要任务不能仅由执行
Agent 自我宣布完成。

### 5.3 Progress Tracker

每项验收条件保存状态、责任工作包、最近检查时间、证据引用、失败原因和下一步。进度百分比由必需验收项及权重计算，不允许模型随意填写。

### 5.4 Steering 集成

复用 `steering.py`，补齐：

-   区分补充、修改、取消和优先级调整。
-   变更后更新 Goal Contract revision 并记录影响范围。
-   受影响验收项及其依赖项重新验证。
-   已完成但受新要求影响的结果标记为待复核。
-   统一支持 Agent、Team、Document Workflow 和 Research。
-   消息注入成功不能被当作目标契约更新成功。

**交付：** Supervisor、Evaluator、Tracker、预算/停滞保护、Steering
集成和恢复测试。

------------------------------------------------------------------------

## 6. Phase 3：Deep Research Profile

### 6.1 Research Plan

将研究目标拆成持久化研究问题：

``` json
{
  "research_id": "research-id",
  "goal_id": "goal-id",
  "revision": 1,
  "topic": "研究主题",
  "scope": {
    "time_range": "明确时间范围",
    "geography": [],
    "source_preferences": [],
    "excluded_sources": []
  },
  "questions": [
    {
      "id": "RQ-001",
      "question": "需要回答的研究问题",
      "priority": "high",
      "status": "pending",
      "queries": [],
      "claim_refs": [],
      "gaps": []
    }
  ],
  "report_outline": [],
  "status": "planning"
}
```

要求：

-   自动拆解研究问题、子问题和调查方向。
-   每个问题有优先级、状态和完成标准。
-   支持动态新增、合并、拆分问题，并记录 revision 和原因。
-   明确时间范围、地域、来源偏好和排除范围。
-   检查研究问题是否覆盖用户原始目标，不能生成计划后就认为覆盖完整。

### 6.2 Research Loop

``` text
PLAN -> SEARCH -> READ -> EXTRACT_FINDINGS
  -> CREATE_CLAIMS -> VERIFY_EVIDENCE
  -> IDENTIFY_GAPS
  -> REFINE_QUERIES / ADD_QUESTIONS
  -> CONTINUE or SYNTHESIZE
```

每轮记录研究问题、查询理由、实际读取来源、发现、支持/反驳证据、未解决缺口和下一步理由。识别重复查询、重复来源和低相关资料，避免为了增加搜索数量而空转。

### 6.3 Evidence Store

至少建立 Source、Evidence、Claim 三类结构。

**Source：** 来源
ID、URL、标题、来源类型、作者/机构、发布时间、事件时间、采集时间、域名、HTTP
状态、内容哈希、正文存储位置。

**Evidence：** Evidence ID、Source ID、原文片段、页码/章节/代码行等
Locator、对应研究问题和提取时间。

**Claim：** Claim
ID、主张内容、对应研究问题、支持/反驳证据、核验状态和核验说明。

证据状态至少区分：

`discovered -> read -> relevant -> supported / conflicting / insufficient -> verified`

规则：

-   搜索发现不等于读取；读取不等于相关；相关不等于支持。
-   URL 出现过不等于证据成立。
-   重要结论优先使用官方文档、源码、原始数据和一手资料。
-   保留反面证据、来源冲突和不确定性。
-   置信度不能只由来源数量决定。
-   保留证据原文和来源追溯能力。

### 6.4 Evidence Verifier

对重要 Claim 独立检查：

1.  证据是否真实读取、片段是否存在。
2.  是否与 Claim 相关且真正支持该主张。
3.  来源时间是否符合研究要求。
4.  是否存在反例、冲突或更权威来源。
5.  是否将事实、推断和预测混为一谈。

Verifier 输出结构化结果，不允许只返回"验证通过"。

### 6.5 多智能体研究

复用现有 Team Runtime，不另建成员 Agent Runtime。可按需分配 Research
Lead、专项 Researcher、Evidence Verifier 和 Synthesizer。

要求：

-   子任务有明确边界和交付契约。
-   成员共享统一 Evidence Store，而非只交换长文本。
-   成员产出必须经过统一验收。
-   避免重复调查；成员失败时保留部分成果并恢复。
-   并行度和模型/工具调用受全局预算控制。
-   简单研究任务允许单 Agent，不强制启动团队。

**交付：** Research Plan、Research State、Evidence Store、Verifier、Team
适配和恢复测试。

------------------------------------------------------------------------

## 7. Phase 4：资料检索与读取升级

先定义统一 Source Reader 接口，再按优先级接入，避免 Research Runtime
与工具实现强耦合。

  能力           建议方向                      目标
  -------------- ----------------------------- ----------------------------------
  网络搜索       保留 SearXNG 和现有降级引擎   发现来源
  网页正文       正文抽取器                    结构化正文和章节
  JS 页面        Playwright / Browser 适配器   动态页面
  PDF / Office   Docling 或现有解析能力        长文档、表格、章节
  GitHub         API / 现有连接器              README、源码、Issue、PR、Release
  内部知识       现有 Knowledge RAG            企业资料
  历史会话       History Retrieval             复用上下文
  数据分析       Sandbox Python                统计、计算和验证

要求：

-   `web_tools.py` 保持兼容，新增 Reader 抽象，不一次性推翻现有工具。
-   保留标题、章节、时间、正文、页码/代码行等 Locator。
-   长内容按需分块读取，不能只截取开头。
-   明确区分 HTTP 失败、空正文、脚本页面、权限页面和超时。
-   校验 HTTP 状态、重定向、内容类型、响应大小和超时。
-   防止 SSRF、内网探测、恶意重定向和不可信内容注入。
-   对网页、文件和搜索结果中的指令一律视为不可信数据。
-   记录采集时间和内容哈希。
-   新依赖评估镜像体积、CPU/内存、许可证、离线部署和维护成本。

第一阶段优先做好接口、来源元数据和正文质量，再根据评测结果接入
Browser、Docling 等工具。

------------------------------------------------------------------------

## 8. Phase 5：Goal、Work Orchestrator 与 Team 统一集成

Goal Runtime 不应直接耦合具体 Agent。统一执行器协议至少支持：

-   创建工作包、启动执行、查询状态。
-   获取结构化结果和产物引用。
-   暂停、取消和 Checkpoint 恢复。
-   返回执行证据与失败原因。

执行器包括 DeepAgents、Team、Document Workflow、Research
Profile、Software/Sandbox Workflow。

统一管理 Goal、Run、Work Package、Team Member、Document
Run、Research、Checkpoint、预算、取消和审批状态。

重新处理 Goal 与 Team 的互斥限制、Work Orchestrator 对 Goal
的跳过分支，以及文档工作流与 Goal
的验收边界。只有状态、预算、恢复和验收适配完成后，才能开放组合能力。

------------------------------------------------------------------------

## 9. Phase 6：前端交互

后端状态协议稳定后再改前端，沿用现有 UI 体系。

### Goal 面板

展示原始目标、revision、验收项、当前轮次、工作包、执行器、已完成/未完成/阻塞项、进度、成本、耗时、最近验收结果。

支持暂停、恢复、取消、Steering、修改目标、查看执行历史、产物和验收证据。

### Deep Research 面板

展示研究范围、研究问题、调查状态、当前搜索/阅读动作、已发现/已读取/已核验来源、关键
Claim、支持/反驳证据、缺口、来源冲突和报告进度。

支持调整范围、来源和研究问题，暂停/恢复/取消，查看原始来源和证据片段。

报告应包含摘要、范围与方法、分主题分析、证据引用、冲突与局限、未解决问题和资料链接。不得隐藏证据不足的内容。

------------------------------------------------------------------------

## 10. Phase 7：评测与质量门禁

扩展现有
`goal_mode_probe.py`、`goal_document_steering_probe.py`、`research_delivery_probe.py`
及相关单元测试。Mock 测试只能覆盖确定性逻辑，不能代替真实端到端验收。

### Goal 测试集

-   多验收条件软件开发，要求测试全部通过。
-   修复过程中新增要求、修改目标。
-   文档生成及格式验收。
-   跨 Checkpoint 恢复、Worker 中断恢复。
-   重复失败和无进展检测。
-   预算耗尽后正确暂停。
-   无法完成时正确报告阻塞。
-   Goal + Team + Work Orchestrator 组合任务。

### Deep Research 测试集

-   开源项目架构与源码研究。
-   技术产品横向比较。
-   有时间范围的行业/市场调查。
-   多来源事实交叉核验。
-   来源相互矛盾、证据不足的研究。
-   长 PDF、复杂网页读取。
-   研究中发现新问题并动态扩展。
-   执行中修改范围。
-   研究任务中断、压缩和恢复。

### 核心指标

  指标                       定义
  -------------------------- ------------------------------
  Goal Completion Rate       真正通过必需验收的目标比例
  False Completion Rate      未达成却宣布完成的比例
  Goal Coverage              原始需求被验收条件覆盖的程度
  Recovery Success           故障后正确恢复比例
  Research Coverage          核心研究问题充分回答的比例
  Claim Support Rate         重要主张有有效证据支持的比例
  Citation Accuracy          引用是否真正支持对应结论
  Source Read Rate           已读取来源 / 已发现来源
  Conflict Detection Rate    重要证据冲突识别比例
  Cost per Successful Task   成功任务平均成本
  Time to Completion         成功任务耗时

改造前后必须在相同模型、相同任务和可比较预算下对比。保留任务、执行轨迹、工具调用、证据和最终结果。指标由程序或独立评审计算，不能由执行
Agent 自评。

------------------------------------------------------------------------

## 11. 工程约束

1.  实施前重新检查最新仓库，不能机械照搬本任务书路径。
2.  先输出真实调用链、模块复用清单、数据模型和拟修改文件，再开始编码。
3.  不允许仅修改 Prompt 就宣称完成。
4.  不允许把 Todo、模型自述或 URL 存在当作最终验收。
5.  不允许通过解除全部预算实现长任务。
6.  不允许为 Research 再造 DeepAgents / Team Runtime。
7.  不破坏普通对话、文档任务、工具调用、审批、SSE、Run
    恢复和历史会话兼容。
8.  数据库变更必须有 migration；新状态必须有恢复路径。
9.  关键写操作应幂等，防止重试造成重复副作用。
10. 执行器必须支持取消、超时、错误分类和资源清理。
11. 外部网页和文件是非可信输入，防止 Prompt Injection。
12. 模型、工具、成员并发和成本纳入统一预算与可观测。
13. 关键状态可追踪：Goal -\> Work Package -\> Run -\> Tool -\> Evidence
    -\> Evaluation。
14. 保持现有代码风格，避免无关重构。
15. 必须提供单元、集成和真实端到端测试。

------------------------------------------------------------------------

## 12. 实施顺序与阶段交付

  -----------------------------------------------------------------------------------------
  阶段                    交付                                 完成标准
  ----------------------- ------------------------------------ ----------------------------
  P0 代码复核             架构图、调用链、复用清单、改造设计   真实基线和兼容风险明确

  P1 Goal Contract        数据模型、revision、验收条件         目标持久化、可追踪、可修改

  P2 Goal Supervisor      外层循环、Evaluator、预算、恢复      未达成会继续，达成才停止

  P3 Research Runtime     Plan、Evidence Store、Verifier       研究问题与结论形成证据闭环

  P4 Source Reader        网页、文件、源码读取增强             可读取并定位原始资料

  P5 统一编排             Goal + Orchestrator + Team +         复杂任务可组合执行
                          Workflow                             

  P6 前端                 Goal / Research 面板                 可观察、干预和恢复

  P7 评测                 测试集、基准和回归报告               有可重复的质量/成本对比
  -----------------------------------------------------------------------------------------

每阶段提交代码、migration（如有）、测试结果、调用链说明、配置项、部署兼容说明和已知问题。按阶段实施，不要一次性大规模重写。

------------------------------------------------------------------------

## 13. 最终验收标准

### Goal

-   [ ] 原始目标持久化，必需验收项有稳定 ID。
-   [ ] 单轮 Agent 结束不会误判为整个目标完成。
-   [ ] 未通过项自动进入下一轮，已通过项保留证据。
-   [ ] 支持暂停、取消、恢复和执行中修改目标。
-   [ ] 有停滞检测和全局安全预算。
-   [ ] 只有通过验收才标记 `completed`。
-   [ ] 无法完成时报告阻塞和部分成果。
-   [ ] 可按策略调用 Team、Document、Research、Sandbox 等执行器。

### Deep Research

-   [ ] 研究目标拆成可跟踪问题，计划支持动态调整和版本记录。
-   [ ] Source、Evidence、Claim 有结构化关联。
-   [ ] 重要结论可追溯到具体证据。
-   [ ] 能识别证据不足、冲突和来源时效问题。
-   [ ] 研究缺口可触发新调查。
-   [ ] 支持多类型资料读取、暂停、恢复和用户干预。
-   [ ] 报告区分事实、推断、预测和不确定性。
-   [ ] Claim 支持率和引用准确率有独立评测。

### 平台集成

-   [ ] Goal 可按策略使用 Work Orchestrator，而非固定跳过。
-   [ ] Goal 可组合 Team、Document Workflow、Research 和 Sandbox。
-   [ ] 团队/工作流状态与 Goal 工作包状态一致。
-   [ ] 预算、事件、取消、Checkpoint 和恢复统一。
-   [ ] 普通对话和既有业务流程回归测试通过。

------------------------------------------------------------------------

## 14. 给 Codex / Trae 的执行指令

**第一步：只审查，不立即改代码。** 重新检查 Goal、Completion、Task
Contract、Decision、Work Orchestrator、Team Runtime、Web Tools、Research
测试和数据库模型，确认当前实现。

**第二步：输出实施设计。**
提供真实调用链、可复用模块、新增/重构模块、数据模型、状态流转、API/SSE/前端影响、分阶段计划和兼容风险。

**第三步：分阶段实施。** 优先完成 P1、P2，跑通真实 Goal 闭环；再完成
P3、P4，跑通 Deep Research 证据闭环；随后处理 P5、P6
和评测。每阶段测试、汇报后再进入下一阶段。

**第四步：真实端到端验收。** 至少完成： 1. 多轮 Goal 软件修复任务。 2.
执行中修改目标的 Goal 任务。 3. 跨轮次恢复的 Goal 任务。 4. 多来源技术
Deep Research。 5. 包含冲突证据、需要继续调查的 Deep Research。 6.
Goal + Team / Work Orchestrator 组合任务。

最终提交完整代码、migration、部署配置、测试结果、改造前后指标和已知限制。

**最终目标不是让 Agent
运行得更久，而是让它持续、可靠、可验证地完成用户真正要求的复杂任务。**
