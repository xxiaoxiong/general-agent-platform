# Agent 测评系统深度重构 — 交付报告

> 任务书：`docs/Agent_测评系统深度重构任务书_精简修订版.md`  
> 执行基线：以当前工作区代码复核后实施（原审查 commit 仅作参考）  
> 交付日期：2026-11-26  
> 使用与 Verifier 配置：`docs/测评系统_使用与Verifier手册.md`

## 1. 阶段 0：代码复核结论

复核确认了任务书列出的主要问题，并据此明确改造点：

| 优先级 | 原问题 | 复核结果 | 本次处理 |
|---|---|---|---|
| P0 | Campaign 未可靠固定 AgentVersion | `EvalRun.agent_version_id` 恒为空；执行时 `ensure_version()` 重新解析最新版本 | Campaign 创建时固定版本；Runtime `_begin_run` 支持显式版本且不再重新解析 |
| P0 | Benchmark Run 可能进入 Evolution 生产统计 | `AgentRun` 无来源字段；Evolution 扫描全部终态 Run | 新增 `agent_runs.source`；Evolution/开发简报/手动触发全链路排除 `evaluation` |
| P0 | 执行、验证、评分状态耦合 | `EvalRunItem.status` 同时表达执行与评分；Judge 故障记 `error` | 拆分 execution / verification / grading / outcome 四类状态 |
| P0 | 超时、取消、重启恢复不完整 | 仅 `asyncio.create_task`；进程重启后 pending/running 永久悬挂 | 新增持久化 Evaluation Worker（Lease / Heartbeat / Fencing / 恢复） |
| P1 | 缺少 Suite / Campaign / Trial / 重复 Trial | 单数据集 EvalRun，无套件与重复执行 | 新增 EvalSuite / SuiteVersion；EvalRun 升级为 Campaign；EvalRunItem 升级为 Trial |
| P1 | 过度依赖 LLM Judge | 有 constraint 等确定性能力，但无统一 Verifier 注册表 | 新增确定性 Verifier 注册表（文本/JSON Schema/约束/工具/产物） |
| P1 | 工程、文档任务缺少最终产物验证 | 仅从工具参数收集文本 | 采集 Trial 真实工作区最终文件，并支持 artifact 验证 |
| P1 | 能力指标、复杂度、样本充分性不统一 | 仅有复杂度/任务类型分层 | 新增 8 个能力维度、Case/Trial 双层统计、Wilson 置信区间、pass@k/pass^k |
| P2 | 前端承载逻辑多、诊断链路不直观 | 页面已拆面板，但缺少能力总览/套件/对比 | 新增能力总览、测评套件、版本对比、状态分解、评分历史、重新评分 |

## 2. 总体架构与数据模型

```text
EvalSuite ──< EvalSuiteDataset >── EvalDataset ──< EvalDatasetItem
    │
    └──< EvalSuiteVersion（不可变用例全量快照）
                     │
                     ▼
EvalRun（Campaign：固定 AgentVersion + SuiteVersion + 环境快照）
    │  Lease / Heartbeat / cancel_requested
    ▼
EvalRunItem（Trial：execution/verification/grading/outcome 分离）
    │  1..N 条 EvalGrade（Verifier / Judge 评分历史）
    ▼
AgentRun（source=evaluation，复用现有 DeepAgents + LangGraph Runtime）
    ├── AgentRunEvent / AgentToolExecution（Trace 与工具账本）
    └── 工作区最终文件（AGENT_FILES_ROOT/{user}/{agent}/{session}）
```

关键概念与表映射：

| 任务书概念 | 实现 |
|---|---|
| Dataset | `eval_datasets` / `eval_dataset_items`（保留） |
| Eval Suite | `eval_suites` + `eval_suite_datasets` |
| Suite Version | `eval_suite_versions.snapshot`（含用例全量内容与 digest） |
| Campaign | `eval_runs`（扩展固定版本、套件、重复次数、环境、Lease 字段） |
| Trial | `eval_run_items`（扩展 `trial_index`、四类状态、`dataset_id`、`attempt`） |
| Attempt | `eval_run_items.attempt`（仅基础设施故障、且无任何工具调用时保守重试） |
| Grade | `eval_grades`（verifier / judge，保留历史，支持重新评分） |

## 3. 阶段 1：测评可信度修复（P0）

1. **固定 AgentVersion**
   - Campaign 创建时解析并写入 `agent_version_id`；`environment` 固化 Agent 版本号、快照摘要、模型配置/Provider、Runtime 版本、工具、套件版本与 digest。
   - Runtime `_begin_run`：`agent_config.agent_version_id` 存在时只认该版本，缺失直接失败，不回落最新版本。
   - 历史 Campaign 未固定版本时不允许恢复（避免版本漂移），要求新建测评。

2. **Benchmark 与生产运行隔离**
   - `agent_runs.source`：`chat` / `workflow` / `evaluation` / 其它调用来源；工作流已显式标记 `workflow`。
   - Evolution 周期评估、手动触发、开发简报、工具/HITL 统计均排除 `source=evaluation`。
   - 测评 Run 的事件清理使用独立保留策略：普通日志保留期清理不删除 `evaluation` Run 事件。

3. **状态拆分**
   - Execution：`pending/running/succeeded/agent_failed/timeout/infra_error/cancelled`
   - Verification：`pending/passed/failed/unscored/error`
   - Grading：`pending/graded/unscored/error/needs_review`
   - Outcome：`pass/fail/inconclusive/infra_error/cancelled/unscored`
   - Agent 成功率分母只含 `pass + fail`；Judge/Verifier 故障、基础设施故障不再计入 Agent 失败。
   - 未配置评分器（`none`）结果为 `unscored`，不被当作执行失败。
   - 旧数据兼容：`status` 保留为结果镜像；新状态为 pending 时按旧 status 推导展示口径。

4. **超时、取消与恢复**
   - 单 Trial 共享超时 deadline；超时取消真实 AgentRun。
   - Campaign 级总耗时 / 总 Token 上限（`campaign_timeout_s` / `campaign_max_tokens`），在 Trial 之间检查；超限后跳过未执行 Trial 并将 Campaign 标记失败（原因明确，不计入 Agent 失败统计）。
   - 取消意图落库 `eval_runs.cancel_requested`；心跳发现后取消当前 AgentRun。
   - Evaluation Worker 使用数据库租约领取 Campaign；心跳续租；租约丢失立即停止推进。
   - 恢复时先把 running Trial 退回 pending，再按已有 AgentRun 状态处理：
     - 已终态：直接采集事实并评分，不重复执行；
     - 非终态：等待至 deadline，超时取消并记 `timeout`；
     - 多轮任务逐轮持久化进度，恢复只续做未完成轮次。
   - 基础设施故障仅在“该 Trial 没有任何工具调用”时保守重试一次，杜绝不确定外部副作用重放。

## 4. 阶段 2：Suite / Campaign / Trial

- 新增套件 CRUD、数据集组合、版本发布（内容快照 + digest）。
- Campaign 支持单数据集或套件（多数据集），套件任务按版本快照展开 Trial，发布会后修改数据集不影响历史 Campaign。
- 支持 `trials_per_case`（1-10），同一 Case 重复执行用于稳定性统计。
- 每条 Trial 独立 Session；多轮任务在 Trial 内共享 Session。
- 旧 `/api/v2/evaluation/runs` 创建/列表/详情/结果/取消/恢复/Markdown 接口保持兼容，仅新增可选字段。

## 5. 阶段 3：Verifier / Judge / 产物

- 确定性 Verifier（`app/services/evaluation/verifiers.py`）：
  - `exact_match` / `contains` / `keyword` / `regex` / `constraint`
  - `json_schema`（内置最小 Schema 校验，无额外依赖）
  - `tool_usage`（必需工具、禁用工具、调用顺序）
  - `artifact`（必需文件、文件内容包含、文件数量、禁用文件）
  - `mock_state`（Mock API / 业务系统最终状态：必需调用、结果文本、JSON 路径、禁用调用）
  - `project_verification`（工程验收工具 `verify_workspace_project` 的最终结果：构建/测试是否通过）
  - `hybrid`（确定性 Verifier 硬约束 + Judge 主观质量，硬失败不可被 Judge 覆盖）
  - `none`（未配置验证器）
- 工具能力硬约束：必需工具未成功执行、禁用工具被调用 → 直接判定验证失败。
- Judge（`app/services/evaluation/judge.py`）：
  - 输入包含最终回答、工具调用轨迹摘要、最终产物清单、执行状态；
  - 独立 Judge 模型配置（`EVALUATION_JUDGE_MODEL_CONFIG_ID` > `EVOLUTION_JUDGE_MODEL_CONFIG_ID` > 默认模型），支持用例级覆盖；
  - 记录模型配置 ID、Prompt 版本、评分理由与证据引用；
  - Judge 失败只影响 grading，Outcome 记 `inconclusive`，保留执行结果。
- Verifier 硬失败时不调用 Judge，避免高分覆盖硬失败。
- 产物采集：优先读取 Trial 真实工作区最终文件（路径逃逸防护、单文件/总量限额），工作区不可用时回退工具调用参数口径。
- 产物查看：`GET /run-items/{id}/artifact?path=...` 支持文本在线预览/二进制下载，带路径逃逸、大小限制与敏感信息脱敏；Trial 详情可直接打开。
- 支持 `POST /run-items/{id}/regrade`：只重新执行 Verifier/Judge，不重跑 Agent，评分历史写入 `eval_grades`。
- 人工复核：`POST /run-items/{id}/review` 支持 `needs_review` / `confirm_pass` / `confirm_fail` / `reject_score`，复核记录写入 `eval_grades`（kind=manual）与 `score_detail.manual_review`。

## 6. 阶段 4：能力画像、统计与版本对比

- 8 个能力维度：任务理解、规划与推理、工具调用、多工具协作、上下文与记忆、异常恢复、交付能力、可靠性与安全；用例可显式指定，未指定按任务类型固定映射。
- 复杂度 L1-L4 与能力维度正交统计。
- 聚合指标（纯客观、无 LLM）：
  - Trial 通过率、Case 通过率、有效样本/Trial 数；
  - pass@k / pass^k；
  - Wilson 95% 置信区间（按独立 Case 计算，重复 Trial 不重复计为独立样本）；
  - p50 / p95 耗时、Token 总量、单成功任务 Token 成本；
  - 结果/执行/验证分解、验证覆盖率、工具使用与错误率、异常恢复率（出现工具失败但最终通过的 Trial）；
  - 能力维度样本量、通过率、均分、`evidence_sufficient`（有效 Trial < 3 标记样本不足）。
- 版本对比接口 `GET /evaluation/compare`：按 Case 聚合重复 Trial，输出提升/回退/稳定通过/稳定失败、通过率差值与逐 Case 明细；非同套件版本会明确标记“仅供趋势参考”。
- 回归沉淀接口 `POST /evaluation/run-items/{id}/to-regression`：把失败/证据不足 Trial 的用例快照复制为回归用例（按输入去重），前端 Trial 详情可直接操作。
- 前端：
  - 能力总览（最新完成 Campaign 的能力维度、样本量、低通过率维度、复杂度分布）；
  - 测评套件管理（组合数据集、发布版本）；
  - 测评任务创建（数据集/套件、重复执行次数）；
  - 结果页展示结果分解、Case/Trial 通过率、pass@k/pass^k、置信区间、能力维度；
  - Trial 详情展示执行/验证/评分状态、最终产物、评分历史，并支持重新评分；
  - 版本对比对话框；
  - 数据集质量审计（确定性验证覆盖、仅 Judge、无评分、缺期望、未标能力、重复输入）。

## 7. 阶段 5：数据治理与工程质量

- 新增 `GET /evaluation/quality-audit`：纯客观统计每条数据集的有效验证器覆盖与标签完整性。
- 社区数据集种子支持 `capability` 字段（缺省按任务类型映射）。
- 社区数据集验证器覆盖提升：GSM8K 全部 20 条改为数值答案确定性验证；BBH 数值题 6 条改为确定性答案验证；Agent 工具链 16 条改为 `hybrid`（必需工具硬校验 + Judge）；Agent 多轮 5 条改为 `hybrid`（最终数值硬校验 + Judge）。当前 168 条中确定性/混合验证覆盖 75 条（44.6%），其余仍为 LLM Judge（BFCL/IFEval 等语义任务）。
- 默认社区能力套件：种子导入时自动维护「社区通用能力基准（默认）」套件，组合全部 12 个社区数据集，并在内容变化时发布新版本（测试验证 168 条 Case 快照）。
- 新增 Verifier 正例/反例测试（JSON Schema、artifact、mock_state、project_verification、hybrid 硬失败）与状态/恢复/Worker/API/人工复核自动化测试。
- 修复历史迁移中的 PostgreSQL 布尔默认值类型错误（`BOOLEAN DEFAULT 0` → `false`）及基线 downgrade 依赖顺序；CI 的 PostgreSQL job 增加独立迁移库 `upgrade head` + `downgrade base` 步骤。
- 新增迁移 `e9f0a1b2c3d4`，覆盖 Run 来源、套件、Campaign/Trial 字段、评分表与索引；SQLite回滚）；SQLite / MySQL / PostgreSQL 均已实测 upgrade（MySQL/PostgreSQL 额外实测 downgrade）。
- 使用与配置文档：`docs/测评系统_使用与Verifier手册.md`（用例编写、各 Verifier 配置示例、Worker/运行手册、常见问题）。

## 8. 数据库迁移与兼容方案

新增迁移：`backend/alembic/versions/e9f0a1b2c3d4_add_evaluation_campaign.py`

- `agent_runs.source`（默认 `chat`，索引）。
- 新表：`eval_suites`、`eval_suite_datasets`、`eval_suite_versions`、`eval_grades`。
- `eval_runs` 新增：`suite_id`、`suite_version_id`、`trials_per_case`、`environment`、`cancel_requested`、`worker_id`、`lease_expires_at`、`heartbeat_at`、`lease_version`、`attempt`。
- `eval_run_items` 新增：`dataset_id`、`trial_index`、`attempt`、`execution_status`、`verification_status`、`grading_status`、`outcome`、`snap_capability`。
- `eval_dataset_items` 新增：`capability`。
- 兼容策略：
  - 不删除旧表/旧列；旧 `status` 继续保留；
  - 旧结果新状态列为默认值时按旧 `status` 推导；
  - 历史 Campaign 缺少 AgentVersion 标记为版本未知，禁止猜测补写或静默恢复；
  - 旧 API 路径与响应字段保持兼容，仅新增可选字段。

## 9. 验证记录（真实执行结果）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 后端全量测试 | `cd backend && python -m pytest -q` | **1037 passed, 11 skipped**（94.81s） |
| 测评模块测试 | `python -m pytest tests/test_evaluation_module.py tests/test_evaluation_v2.py tests/test_evaluation_api_v2.py -q` | **63 passed** |
| SQLite 迁移 up/down | `pytest tests/test_agent_runtime_durable.py::test_alembic_migration_upgrade_downgrade -q` | **1 passed** |
| MySQL 8 迁移 | `TEST_MYSQL_MIGRATE_SYNC_DSN=...3308/ai_plat_migration_test pytest tests/test_migration_mysql.py -q` | **3 passed** |
| PostgreSQL 迁移 | 对 `ai_plat_migration_test` 执行 `alembic upgrade head` + `alembic downgrade base` | **exit 0（全部 revision 升级/回滚成功）** |
| 前端类型检查 + 构建 | `cd frontend && npm run build` | **通过**（vue-tsc + vite build） |

跳过项说明：11 个 skipped 为环境依赖测试（未配置的 MySQL/PostgreSQL 默认 DSN、可选外部服务等）；MySQL/PostgreSQL 迁移已通过显式本地服务单独验证，不在默认全量命令中重复执行。

## 10. 未完成事项与风险

以下内容**未完成或仅部分完成**，不得视为已交付：

1. **数据集逐条治理未全部完成**：已把 GSM8K/BBH 数值题、Agent 工具链/多轮任务提升为确定性或混合验证（75/168，44.6%），BFCL、IFEval 等语义任务仍以 Judge 为主。
2. **工程构建/Lint/测试未由测评模块独立执行**：已新增 `project_verification` Verifier 检查 Runtime `verify_workspace_project` 的最终结果；但 Evaluation 不会自行启动构建沙箱，工程任务需在用例中声明该必需工具。
3. **Mock API/数据库最终状态验证**：已新增 `mock_state` Verifier（必需调用、结果文本、JSON 路径、禁用调用）；尚未提供可视化状态配置器。
4. **Judge 校准平台未实现**：已提供人工复核（needs_review / confirm_pass / confirm_fail / reject_score）与评分历史；未实现校准集、多 Judge 一致性分析。
5. **未运行真实模型端到端测评**：自动化测试全部使用 Fake Runtime；本环境未发起真实模型 Campaign（会产生真实 Token 成本）。
6. **前端未拆成 7 个独立路由页**：能力总览/数据集/套件/任务/结果/Trace/对比以 Tab + 抽屉/对话框承载，信息结构与诊断链路已打通，但未做独立路由级页面拆分。
7. **未重建 Docker 镜像、未重启运行容器**：代码与构建产物已验证，部署需按第 11 节执行 `docker compose build` / `up -d`。
8. **CI 流水线已补充 PostgreSQL 迁移步骤，但未在本机执行 GitHub Actions**：SQLite/MySQL/PostgreSQL 迁移已在本地实际执行；CI 工作流仅完成配置修改。
9. **Suite Campaign 的 `eval_runs.dataset_id`** 仍保留为“主数据集锚点”（套件首个数据集）以兼容旧 API；Trial 归属数据集已单独记录，套件全量组成以 SuiteVersion 快照为准。
10. **Campaign 总预算为软上限**：在 Trial 之间检查，无法中断正在执行的单个 Trial（由单 Trial 超时控制）。

## 11. 部署说明

1. 数据库迁移（任选其一）：

```bash
cd backend
python -m alembic upgrade head
```

Docker 部署需重建后端镜像（后端启动入口会执行迁移）：

```bash
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml up -d
```

2. 新增可选配置（均有默认值，可按需写入 `.env`）：

```dotenv
EVALUATION_WORKER_ENABLED=true
EVALUATION_POLL_INTERVAL=5.0
EVALUATION_WORKER_BATCH=2
EVALUATION_LEASE_SECONDS=120
EVALUATION_HEARTBEAT_INTERVAL=20.0
EVALUATION_INFRA_RETRY_MAX=1
EVALUATION_JUDGE_MODEL_CONFIG_ID=0
```

3. 首次使用建议：
   - 在「数据集」导入或播种社区数据集；
   - 在「测评套件」组合数据集并发布版本；
   - 创建测评任务时选择套件与重复次数；
   - 查看「能力总览 / 测评结果」，对失败 Trial 打开详情 → Trace；
   - 如需复核评分，在 Trial 详情点击“重新评分”。

## 12. 目标核对表

| 任务书目标 | 状态 | 证据 |
|---|---|---|
| Campaign 固定 AgentVersion / SuiteVersion / 环境 | ✅ | `environment` 快照、Runtime 固定版本测试 |
| 每条 Trial 关联真实 AgentRun 与执行证据 | ✅ | `run_id`、事件/工具账本、工作区产物 |
| Agent 失败 / 基础设施 / Verifier / Judge 失败可区分 | ✅ | 四类状态 + Outcome 分解 + 测试 |
| 历史结果可查看、可重新评分 | ✅ | 旧数据兼容展示、`regrade` 接口与评分历史 |
| 人工复核与评分版本管理 | ✅（预留能力） | `review` 接口（needs_review/confirm_pass/confirm_fail/reject_score）+ `eval_grades` |
| 不同版本在相同用例下对比 | ✅ | 套件版本快照 + compare 接口/UI |
| 一次 Campaign 组合多个 Dataset | ✅ | Suite Campaign 测试 |
| 支持重复执行与稳定性统计 | ✅ | `trials_per_case`、pass@k/pass^k |
| 按能力维度与复杂度展示 | ✅ | `by_capability`、`by_complexity`、前端能力总览 |
| 展示样本量与证据覆盖，不对小样本过度推断 | ✅ | `effective_cases`、Wilson CI、`evidence_sufficient` |
| 工程/文档任务真实产物验证 | ✅（工程构建依赖 Runtime verifier） | 工作区最终文件采集 + artifact / project_verification 验证 |
| Mock API / 数据库最终状态验证 | ✅ | `mock_state` Verifier（调用/结果文本/JSON 路径/禁用调用） |
| 数据集有效验证器覆盖 | ✅（部分，75/168） | 确定性/混合验证覆盖 44.6%，质量审计可追踪 |
| 默认 Suite 可完整运行并生成报告 | ✅（快照/套件任务已测） | 「社区通用能力基准（默认）」12 数据集/168 Case + Suite Campaign 测试 |
| 从能力短板定位到 Case / Trial / Trace | ✅ | 能力总览 → 结果 → Trial 详情 → 执行轨迹 |
| 查看新旧版本提升与回退 | ✅ | Compare API + 对比对话框 |
| 失败案例沉淀为回归用例 | ✅ | Trial 详情“沉淀为回归用例” + 自动去重 |
| Campaign 持久化执行、取消、恢复 | ✅ | Evaluation Worker + Lease + 恢复测试 |
| Campaign 总耗时 / 总 Token 上限 | ✅（Trial 之间检查） | `campaign_timeout_s` / `campaign_max_tokens` + 超限跳过 |
| 进程重启不无条件重复执行 | ✅ | 恢复测试（终态 Run 不重跑） |
| Sandbox 隔离与敏感信息保护 | ✅（复用现有本地 Docker 能力） | Workspace 按用户/Agent/Session 隔离；工程验收走 `project_verifier`（network_mode none/read_only）；产物预览脱敏 |
| 数据库迁移与主要 CI 测试通过 | ✅（三库实测 + PG CI 步骤） | SQLite/MySQL/PG 实测；CI 新增 PG 迁移步骤 |
| 前端状态表达清晰、文档与实现一致 | ✅ | 构建通过 + 本报告 |
