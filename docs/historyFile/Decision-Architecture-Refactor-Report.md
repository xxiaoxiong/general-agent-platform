# General Agent Platform — Decision Architecture 重构交付报告

> 依据：`docs/General-Agent-Platform-Decision-Architecture-Refactor.md`
> 日期：2026-11-26
> 范围：语义决策层与多轮任务连续性；不重写 DeepAgents / LangGraph / Document Graph / Team Runtime。

## 1. 改造总览

改造前：`POST /api/v2/runs` 在 API 层用 `route_task()` 正则决定 `task_type`，随后直接组装文档资源并创建 Run；会话里出现过 Word 后，后续轮次容易被历史文档“绑架”。

改造后：

```text
API 确定性预检（身份/会话/附件指纹/显式模式/资源 ID）
        │
        ├── 显式模式 / 显式文档资源 → 规则快速通道（route_task）+ prepare_execution
        │
        └── Auto + 有上下文 → 创建 Run（decision_pending）
                    │
                    ▼
        Run 内 Decision（一次模型调用，失败回退规则）
          context_build → model_call → validator
                    │
                    ▼
        持久化决策快照 + task.decision 事件
                    │
                    ▼
        apply_decision → prepare_execution（文档资源/附件/续做）
                    │
                    ▼
        task.routed → Document Graph / Team Runtime / DeepAgents
```

核心边界：**模型拥有语义决策权，但没有权限授予权和执行绕过权。**

## 2. 关键代码调用链

1. `backend/app/api/run/core.py::create_run`
   - 确定性预检：身份、会话、助手、附件路径/大小/指纹、显式 `task_mode` / `document_id` / `document_profile_id` / `task_options`。
   - Auto 且存在历史/附件/预览时写入 `execution_context.decision_pending` 与 `decision_input`，语义决策延迟到 Run 内。
2. `backend/app/services/agent_runtime/service/stream.py::_drive`
   - 先发布 `run.started`（决策模型调用期间前端可见运行已启动）。
   - 调用 `resolve_run_decision()`；发布 `task.decision`；刷新 `is_document_run` / `is_team_run` / `deliverable`。
3. `backend/app/services/agent_runtime/decision/orchestrator.py::resolve_run_decision`
   - `build_decision_context` → `decide` → `validate_decision` → `apply_decision` → `prepare_execution` → 持久化快照。
4. `backend/app/services/agent_runtime/decision/validator.py`
   - route 白名单校正、context_refs 真实性校验、capability_suggestions 授权校验、文档资源安全边界、歧义澄清。
5. `backend/app/services/agent_runtime/decision/preparer.py::prepare_execution`
   - 从 API 层抽出的统一准备逻辑：历史附件延续、`document_turn` 历史正文解析、文档/规范版本解析、人工输入回收。
6. `backend/app/services/agent_runtime/decision/persistence.py`
   - 决策快照写回 `agent_runs.input_payload.execution_context`；恢复时直接复用，不重复调用模型。
   - 自动模式文档 Run 在决策后补写会话消息 `document_run_id` 标记。
7. `backend/app/services/assistant/task_contract.py`
   - `decision_intent` 优先于能力包/工具/关键词；契约只负责验收义务，不再承担第二套 Router。
8. `backend/app/services/agent_runtime/factory.py`
   - 系统提示注入“本轮任务决策（目标/约束/验收标准/澄清问题）”与“本轮可用附件”。

## 3. Decision 模块

```text
backend/app/services/agent_runtime/decision/
  schemas.py        # RunDecision v1（12 intent / 5 route / continuity / deliverables / context_refs ...）
  context.py        # 有界决策上下文 + 候选引用 + 不可信资料声明
  service.py        # 非流式、温度 0、一次修复重试、超时降级
  validator.py      # 服务端白名单/资源/授权校验与安全降级
  apply.py          # 决策 → execution_context 确定性映射
  preparer.py       # 文档资源/附件/续做上下文准备（API 与 Run 内共用）
  persistence.py    # 决策快照读写 + 会话文档标记
  orchestrator.py   # Run 内编排入口
```

关键约束：
- `route` 由服务端按 `intent` 受控映射，模型返回不一致时校正并记录。
- `context_refs` 只能引用 Context Builder 提供的候选 ID（`message_i` / `artifact_<id>` / `preview_document` / `attachment_i`）。
- `capability_suggestions` 只作建议，未授权能力直接丢弃。
- 文档图只处理 DOCX；用户点名 md/txt/csv/xlsx/pdf/pptx/html/json 时交给通用能力写工作区文件。
- 用户未点名 Word 且没有既有文档延续目标时，不得默认生成 DOCX。
- 新任务/换话题不继承上一轮的文件格式；只有用户本轮点名格式或明确延续既有交付时才保留。
- `resolve_task_resources` 仍按用户归属、会话、版本重新校验资源。
- 决策上下文包含：当前消息、最近 8 轮对话、会话文档产物、预览文档、本轮附件、能力/工具、最近 Run 状态（仅状态，不含原始 Checkpoint）；不注入完整 Checkpoint，避免跨 Graph/跨用户状态污染。
- 历史附件与文档正文按不可信资料处理；`context_refs` 只做候选校验，真实文件读取仍由服务端按归属与指纹决定。

## 4. 模式、事件与可观测

- `AGENT_DECISION_MODE=off|shadow|llm`（默认 `llm`；`shadow` 只记录模型决策、执行规则路线）。
- `AGENT_DECISION_TIMEOUT`（默认 15s）与 `AGENT_DECISION_MAX_TOKENS`。
- `AGENT_AUTO_TEAM_ENABLED` 默认 `false`：Auto 模式不自动启用团队工程路线，保持兼容。
- 事件：
  - `task.decision`：模型建议 + 校验后的最终接受结果 + 耗时/模型/是否回退/是否修复重试；
  - `task.routed`：只保留最终执行路线所需字段（完整决策在 `task.decision`）。
- Langfuse Span：`decision.context_build` / `decision.model_call` / `decision.validation`，与 Run trace 同源。
- 恢复：Run 重新领取后读取已持久化决策，不重复推理；决策未写入前崩溃才会重新决策。

### 事件样例（脱敏）

```json
{
  "schema_version": "v1",
  "decision_source": "llm",
  "mode": "llm",
  "applied": true,
  "intent": "general_chat",
  "route": "deep_agent",
  "effective_task_type": "general_chat",
  "effective_route": "deep_agent",
  "continuity": "modify_existing",
  "decision_status": "clear",
  "goal": "把 AI Agent 调研报告整理为 Markdown 文件",
  "deliverable_format": "md",
  "context_refs": ["artifact_20"],
  "constraints": ["保留原有章节结构"],
  "acceptance_criteria": ["产出 Markdown 文件", "不生成新的 Word"],
  "latency_ms": 8791,
  "model_id": "kimi-k3.1-pro",
  "input_tokens": 2016,
  "output_tokens": 287,
  "repair_attempted": true,
  "fallback": false,
  "notes": ["非 Word 交付格式交由通用能力生成与交付", "route 与校验后的执行边界不一致，已校正为 deep_agent"]
}
```

## 5. 旧逻辑处置清单

| 逻辑 | 处置 |
| --- | --- |
| `agent_runtime/intent.py`（API 层模型分诊） | 删除，替换为 Run 内 Decision 模块 |
| `task_router.route_task()` | 保留为显式模式直达、Auto 基线/兜底；不再决定 LLM 模式最终路线；新增 `intent_source`，`router_version=3` |
| `task_router.detect_deliverable_format()` | 保留（确定性格式语法提取），用户点名格式优先于模型 |
| `document_turn()` | 保留为历史正文/附件解析器；是否升级文档任务由决策 `continuity` 控制（`allow_upgrade`） |
| `team_project_intent.project_scope()` | 显式团队模式继续使用；Auto 团队模式使用决策 `project_scope` |
| `task_contract.infer_task_type()` | 新增 `decision_intent` 优先级；关键词/能力包降为无决策时兜底 |
| 固定 `confidence` 数字 | 保留字段兼容旧事件，不再代表可信度；以 `intent_source` / `decision_status` 为准 |

## 6. 测试与评测

### 单元/集成

- `backend/tests/test_decision_architecture.py`：40 个用例，覆盖 Schema 严格性/别名归一、Context 候选与不可信声明、Validator 降级/澄清/资源边界、Apply 映射、编排应用/Shadow/兜底/恢复复用、任务书 §9 回归场景、评测集结构校验。
- 全量后端：`python -m pytest -q` → **1010 passed, 11 skipped**（90s 级）。说明：其中一次运行在无关的 MCP 用例上出现已知的 SQLite `database is locked` 环境抖动；确认无并发 pytest 进程后重跑全绿。
- 前端：`npm run build` 通过。
- 端到端探针 `backend/tests/e2e/intent_switch_probe.py`：5 轮真实模型验收（Word → md → 纯问答 → Word 编辑 → 换话题），18/18 检查通过；`task.decision` 事件在 md/问答/编辑/换话题轮次均为 `decision_source=llm` 且 `effective_task_type` 与最终路线一致。真实镜像复跑结果见 `log/intent_switch_probe_final.json`（随后补充格式继承 guard、最近 Run 状态与 Token 记录，均有单测/全量评测覆盖，不改变执行路线）。
  - 轮次耗时：Word 生成 101.5s、md 交付 20.7s、纯问答 5.1s、Word 编辑 104.2s、换话题 25.9s。

### 离线评测集

- `backend/tests/evaluation/decision_cases.jsonl`：225 条标注样例（通用对话/调研/Word 生成/Word 编辑/Word 分析/非 Word 交付/软件项目/编码/数据/数据库/业务动作/多轮连续性/模糊请求）。
- 运行器：`backend/tests/e2e/decision_eval.py`（真实模型、同一套 Context/Validator，输出准确率、混淆矩阵、P50/P95、回退数）。

### 指标（真实模型，kimi-k3.1-pro）

最终全量 225 条（最终代码；完整记录 `log/decision_eval_final_225.json`）：

| 指标 | 结果 |
| --- | --- |
| 有效样本 / 回退 | 219 / 225（fallback 6，2.7%） |
| Intent Accuracy | 92.86% |
| Route Accuracy | 92.24% |
| Continuity Accuracy | 93.15% |
| Deliverable Format Accuracy | 100% |
| Document Graph Precision / Recall | 90.48% / 98.28% |
| Clarify Recall | 44.44%（模糊子集 9 条） |
| Decision 延迟 P50 / P95 / Max | 5.06s / 11.22s / 13.58s |
| 决策 Token（输入 / 输出） | 424,835 / 59,851（平均 1,888 / 266） |

意图混淆矩阵摘要（最终全量，完整矩阵见 `log/decision_eval_final_225.json`）：

| 期望 intent | 样本 | 主要预测分布 |
| --- | --- | --- |
| general_chat | 71 | general_chat 70、data_analysis 1 |
| coding | 8 | coding 6、general_chat 2 |
| research | 30 | research 30 |
| document_generate | 29 | document_generate 27、document_edit 1、general_chat 1 |
| document_edit | 29 | document_edit 26、document_generate 3 |
| document_analyze | 22 | document_analyze 19、general_chat 1、data_analysis 1 |
| software_project | 13 | software_project 11、general_chat 1、unknown 1 |
| data_analysis | 2 | data_analysis 2 |
| database_query | 3 | data_analysis 2、database_query 1 |
| business_action | 3 | business_action 3 |

分类结果（最终全量）：

| 类别 | Intention / Route Accuracy |
| --- | --- |
| Word 生成 | 100% / 100% |
| Word 编辑 | 100% / 100% |
| 调研 | 100% / 100% |
| 非 Word 交付 | 100% / 100% |
| 通用对话 | 100% / 100% |
| 多轮连续性 | 75% / 90% |
| Document 分析 | 85% / 90% |
| 编码 | 75% / 100% |
| 业务动作 | 100% / 33.3%（1/3 误入文档路线） |
| 模糊请求 | — / 44.4%（应澄清未澄清） |

补充重点回归 122 条（文档路线 guard 后；完整记录 `log/decision_eval_focus_122.json`）：

| 指标 | 结果 |
| --- | --- |
| 有效样本 / 回退 | 121 / 122（fallback 1） |
| Intent Accuracy | 94.64% |
| Route Accuracy | 95.04% |
| Continuity Accuracy | 92.56% |
| Document Graph Precision / Recall | 89.47% / 97.14% |
| Clarify Recall | 55.56% |

结论：
- 核心交付路线已稳定：Word 生成/编辑、调研、非 Word 交付、通用对话在最终全量中均为 100% 路线准确率；
- 主要误差集中在两类模型语义判断：模糊请求应澄清却执行了文档路线（Clarify Recall 44.4%），以及个别业务动作（“审批单/工单”类）被误判为文档生成；这是模型行为问题，不是资源越权或执行器缺陷；
- 最终代码又补充了“新任务/换话题不继承历史文件格式”和“最近 Run 状态进入决策上下文”的改动，均有单测覆盖；前者只影响 `deliverable_format` 继承，后者只提供有界状态摘要；
- Decision 调用 P95 约 11.2s，fallback 2.7%；超时按规则路由兜底，不会卡死 Run。

## 7. 性能与回退

- 决策调用在 Run 内执行，`run.started` 先发出，不阻塞建 Run。
- 每个新 Auto Run 最多一次正常决策；Schema 解析失败最多一次修复重试。
- 超时/网络/非法输出：记录原因并回退规则路由；文档资源解析失败：降级通用对话，不误杀 Run。
- 恢复/HITL/断点续跑复用已持久化决策，不重复路由。
- 回滚：`AGENT_DECISION_MODE=off` 即回到规则路线；`shadow` 用于上线前观察差异。

## 8. 与任务书的差异说明（有意的边界）

- **历史正文选择**：`context_refs` 已用于校验/绑定目标产物与预览文档；文档草稿仍由 `document_turn` 在最近 24 条消息内做有界解析，而不是按 `message_i` 精确裁剪。原因是草稿需要连续正文与原始任务锚点，纯按模型选中的消息裁剪会丢章节；当前方案优先保证文档交付质量。
- **Checkpoint 摘要**：未把原始 Checkpoint 注入决策上下文。按任务书“无法确认状态归属时宁可不注入”的原则，只使用当前 Session 的最近对话、产物元数据、最近 Run 状态和 `completion_resume`；避免跨 Graph/跨用户状态污染。
- **`clarify`**：决策快照记录 `route=clarify` 并注入澄清问题，执行侧走通用对话让模型向用户提问，不启动 Document/Team 专用图；不是硬中断式表单。
- **Auto Team**：`team_project_graph` 仅当 `AGENT_AUTO_TEAM_ENABLED=true` 时可由决策进入；默认关闭，显式 `team_mode` 保持原行为。
- **指标门槛**：专用路线 Precision 未达到建议的 95%，已在评测中如实记录；不为达标而增加关键词硬编码。

## 9. 风险与遗留问题

- 决策模型仍可能误判：最终全量 Document Graph Precision 90.48%（未达到任务书建议的 95%），Clarify Recall 44.44%（模糊子集 9 条），业务动作路线准确率 33.3%。这些是模型语义判断质量问题，不是权限/执行绕过；后续可继续用同一评测集迭代提示词或模型。
- “用户未点名 Word 且无既有文档目标不默认生成 DOCX”是本次的行为变化，属于产品规则收敛，需要在评测/验收中持续观察。
- 决策使用当前助手模型，P95 延迟约 11.2s；超时会回退规则路由（最终全量 2.7%）。如需进一步降低延迟，可后续引入独立轻量决策模型配置。
- Auto 团队工程路线默认关闭（`AGENT_AUTO_TEAM_ENABLED=false`）；显式 `team_mode` 仍使用规则 `project_scope`。
- 决策上下文使用最近 8 轮有界历史 + 产物/附件元数据，不注入完整 Checkpoint；`completion_resume` 负责未完成文件任务续做。
- 行为变化：用户未点名 Word 且无既有文档目标时，Auto 模式不再默认生成 DOCX（通用能力交付正文/其他格式）。
