# Langfuse 深度集成与 Agent 自进化闭环实施计划

## Context

目标（用户已确认）：
1. **深度集成**：将平台 Agent Runtime 与 Docker 中自托管的 Langfuse（已运行，web 容器 `langfuse-deploy-langfuse-web-1`，宿主机 http://localhost:3100，compose 目录 `D:\MyPersonalFile\MyStudyFile\AI\CompanyProject\Langfuse_Deploy`，v3.224.2）打通——完整执行过程（Graph/Model/Tool/SubAgent/审批/恢复 Span 树）与效果（结果、用量、评分）可在 Langfuse 查看；平台 Run 详情内嵌"查看 Langfuse Trace"链接。
2. **自进化闭环（平台内置）**：拉取执行数据 → 评估（确定性指标 + LLM-as-judge）→ 生成优化方向报告 → 一键应用 prompt/参数/模型迭代 → 前后指标对比验证 → 回滚。迭代对象覆盖 prompt、运行参数、工具与流程、模型选择，指标覆盖主流 agent 评测维度（成功率、质量、工具准确率、效率、健壮性、成本/延迟、HITL、回归 A/B）。

现状：Phase 1 可观测性代码已就绪（[langfuse_tracing.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/observability/langfuse_tracing.py)：SDK v5 Client、确定性 trace_id、CallbackHandler、platform_span、mask 脱敏），但 `LANGFUSE_*` 密钥未配置（全链路 no-op）、docker-compose 未透传 Langfuse 环境变量、无评估/进化机制、平台无 Trace 入口。

**关键已验证事实**：
- Run 执行只认最新 `AgentVersion` 快照（[versions.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/assistant/versions.py) `ensure_version` L123 仅在无版本时发 v1，`config_from_version` L140）；改 Agent 行后必须 `publish_version`（L84）才对 Run 生效 → **apply/回滚统一走"改 Agent 行 + publish_version"机制**。
- trace 富化的单一漏斗：[service.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/agent_runtime/service.py) `_commit`（L1899，终态公共提交点）；`get_run` 序列化（L556）可干净追加 trace 字段。
- SDK v5.2.0 无 `update_current_trace`；可用 `propagate_attributes(as_baggage=True)`、`client.api.trace.create`（upsert）、`client.create_score`、`client.get_trace_url`、`client.async_api`（拉 trace/observations，优于裸 httpx）。
- 平台自身调 LLM 的封装可复用：[model_service.py](file:///d:/MyPersonalFile/MyStudyFile/AI/MyCode/AI-GeneralPlat/backend/app/services/model_service.py) `ModelService.chat` L199。
- 迁移用 alembic；后台周期任务仿 `RunWorker._poll_loop` 在 lifespan 挂 asyncio task；多 Worker（UVICORN_WORKERS=2）需 Redis `SET NX EX` 锁 + DB UNIQUE 约束双保险。

---

## Phase A — 打通 + 平台可见性

| 文件 | 改动 |
|---|---|
| `docker/docker-compose.yml` | backend 增加 `networks: [default, langfuse]`（`langfuse: external: true, name: langfuse-deploy_default`）；透传 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL`（容器内 `http://langfuse-deploy-langfuse-web-1:3000`）+ `LANGFUSE_PUBLIC_URL`（浏览器可达 `http://localhost:3100`） |
| `docker/.env`、`docker/.env.example` | 增加 LANGFUSE_* 条目（example 带说明） |
| `backend/.env` | 本地开发填 `LANGFUSE_BASE_URL=http://localhost:3100` + pk-lf/sk-lf 密钥 |
| `backend/app/core/config.py` | 新增 `LANGFUSE_PUBLIC_URL: str = ""`（缺省回落 BASE_URL） |
| `backend/app/observability/langfuse_tracing.py` | 新增 `enrich_run_trace(run_id, name, user_id, session_id, task, final_text, metadata)`（`client.api.trace.create` upsert，全 try/except 不影响业务）；`trace_url(run_id)`（`get_trace_url`）；`run_context(...)`（`propagate_attributes(as_baggage=True)`） |
| `backend/app/services/agent_runtime/service.py` | `_commit(finished=True)` 成功后调 `enrich_run_trace`（含 status/error/budget/耗时）；`get_run` 响应加 `langfuse_trace_id`/`langfuse_trace_url` |
| `backend/app/services/agent_runtime/worker.py` | Run 执行段包 `run_context`（trace_name=agent 名） |
| `backend/app/api/v2/admin.py` | L65 `links.langfuse` 改为 `LANGFUSE_PUBLIC_URL` 绝对地址 |
| `frontend/src/api/v2.ts`、`src/views/workspace/WorkspaceHome.vue` | `RunInfo` 加 trace 两字段；助手消息 meta 与 run 详情加"查看 Langfuse Trace"外链 |

**密钥获取**：在 Langfuse UI（http://localhost:3100）创建项目 `AI-GeneralPlat` 并生成 pk-lf/sk-lf（必要时用浏览器自动化完成；登录凭据查 `Langfuse_Deploy/DEPLOYMENT_REPORT.md`）。

## Phase B — 评估引擎（新目录 `backend/app/services/evolution/`）

- **`langfuse_api.py`**：基于 `client.async_api` 的 `fetch_trace_bundle(trace_id)`（trace + observations）与 `write_scores(trace_id, scores)`（`create_score` 回写，Langfuse UI 可见评分）。
- **`metrics.py`**：`compute_deterministic(run, db)` 纯函数——从 `agent_runs`（status/budget_*/时长）、`agent_tool_executions`（错误/重试/重复）、`agent_run_events`（retrying/limit）、`agent_approvals`（HITL 分布）算指标。
- **`judge.py`**：`judge_run(...)` 用 `ModelService.chat`（judge 模型走 ModelProvider/ModelConfig），输入取 DB `input_payload/final_text`（不取截断的 trace），输出 JSON（schema 见下），解析失败重试 1 次后标 failed；结果写 `agent_evaluations` + 回写 Langfuse score。
- **`evaluator.py`**：lifespan 周期循环，扫终态未评估 Run，`INSERT UNIQUE(run_id)` 抢占 + Redis 锁防多 Worker 重复；Langfuse 无 trace 时降级仅算确定性指标（`no_trace`）。
- **新模型 `backend/app/models/evolution.py` + alembic 迁移**：

```
agent_evaluations: id PK, run_id VARCHAR(36) UNIQUE FK, agent_id(idx), agent_version_id(idx),
  session_id, status(pending/computed/failed), success BOOL, overall NUMERIC(5,2),
  deterministic JSON, judge_scores JSON, judge_model_config_id, langfuse_trace_id,
  error TEXT, attempts INT, evaluated_at, created_at/updated_at
evolution_reports: id PK, agent_id(idx), window_start/window_end, baseline_version_no,
  status(draft/confirmed/applied/validated/rolled_back), summary_text TEXT, suggestions JSON,
  metrics_before JSON, metrics_after JSON, created_by, validated_at, created_at/updated_at
evolution_change_log: id PK, report_id FK idx, agent_id idx,
  change_type(prompt/param/model/tools_flow), target, before JSON, after JSON,
  applied_version_no, status(applied/validated/reverted), validation JSON,
  revert_of_change_id NULL, applied_by/at, reverted_at
```

## Phase C — 自进化闭环

- **`evolution_advisor.py`**：`generate_report(agent_id, window)` —— 聚合窗口内评估（分版本对比）→ 规则初筛（工具错误率高/步数超预算/某版本成功率下降…）+ LLM 顾问 → 结构化 suggestions 落 `evolution_reports`。
- **`backend/app/api/v2/evolution.py`**（注册进 main.py，全部 `Depends(get_current_superuser)`）：

| 端点 | 说明 |
|---|---|
| `GET /evolution/overview?agent_id&hours` | 成功率/均分/步数/token/延迟/工具错误率/HITL 率 + 分版本对比 |
| `GET /evolution/evaluations` | 评估列表（含 trace_url） |
| `POST /evolution/evaluations/trigger` | 手动触发评估 |
| `POST /evolution/reports/generate` | 生成进化报告（含建议） |
| `GET /evolution/reports[/{id}]` | 报告列表/详情 |
| `POST /evolution/reports/{id}/suggestions/{idx}/apply` | 一键应用 → `{change_ids, new_version_no}` |
| `POST /evolution/changes/{id}/rollback` | 回滚 → 还原 before + 再发版 |
| `GET /evolution/reports/{id}/markdown` | Markdown 导出（供开发代理外部消费迭代方向） |
| `GET /evolution/changes` | 变更审计（before/after、验证结果） |

- **apply/回滚（统一 AgentVersion 机制）**：apply = 事务内 ① 写 Agent 行（prompt→`system_prompt`；param→`temperature`/`max_iterations` 等带范围校验；model→`model_config_id` 校验 active；tools_flow→报告项）② `publish_version` ③ 写 change_log。回滚 = `before` 还原 Agent 行 + 再 `publish_version` + 标 `reverted`（历史只增不减，快照双保险）。
- **验证闭环**：apply 后按 `applied_version_no` 圈定新 Run（`agent_runs.agent_version_id`），累计 ≥N（默认 5）条评估后自动对比 baseline → `validation` JSON，标 `validated` 或提示"建议回滚"。
- **前端 `src/views/system/Evolution.vue`** + 路由 `system/evolution`（`requiresSuperuser: true`，仿 AuditLog）+ `src/api/evolution.ts`：概览卡片、评估表（trace 外链）、报告详情（建议 + diff + 一键应用/回滚）、before/after 对比。

**Judge JSON**：`{"dimensions":{"task_completion","answer_quality","tool_selection","efficiency","groundedness","format_compliance"}(0-10), "verdict":"pass|fail", "issues":[{dimension,evidence,suggestion}], "confidence":0-1}`
**Advisor suggestion JSON**：`[{type:"prompt|param|model|tools_flow", target, current, proposed, diff, rationale, evidence_run_ids, expected_impact, priority, auto_applicable, allowed_range?}]`

## 指标 → 数据源 → 优化杠杆

| 指标 | 数据源 | 杠杆 |
|---|---|---|
| 任务成功率 | `agent_runs.status` + judge 任务完成度 | prompt |
| 答案质量/幻觉/格式 | judge LLM | prompt |
| 工具准确率（错误/重复/选错） | `agent_tool_executions` | prompt + tools/flow |
| 效率（步数/迭代/token） | `budget_*`、`summary` | param + prompt |
| 健壮性/恢复 | `agent_run_events`、`attempt`、`recovery_metadata` | param + tools |
| 成本/延迟 | Langfuse observations + started/finished_at | model 切换 |
| HITL | `agent_approvals` | param（审批策略） |
| 回归 A/B | 按 `agent_version_id` 分组对比全部 | 验证闭环判定 |

## 验证

1. **单测**：`test_evolution_metrics.py`、`test_evolution_judge.py`（mock chat）、`test_evolution_langfuse_api.py`（mock async_api/create_score）、`test_evolution_advisor_apply.py`（apply→新版本、rollback→还原）、`test_evolution_evaluator_claim.py`（唯一约束去重）、`test_api_v2_evolution.py`（权限/分页/全链）；迁移 upgrade/downgrade。
2. **回归**：全量 pytest（现基线 354 passed）。
3. **E2E（真实 Langfuse）**：配置密钥 → 跑一次真实 Run → Langfuse UI 确认完整 Span 树 + trace 名/user/session/结果 → 触发评估确认 score 落 Langfuse → 平台 Run 详情链接可跳转 → 生成报告 → apply 一条 prompt 建议 → 新 Run 绑定新版本 → 验证对比生效 → 回滚恢复。

## 风险要点

- `create_score` fire-and-forget，验收前 `client.flush()`；采样率建议保持 1.0（评估用途）。
- 多 Worker 周期任务：Redis 锁 + UNIQUE(run_id) 双保险；evaluator 每轮独立 session，LLM 调用不持有长事务。
- judge 输入取 DB 全文（trace 有 2000 字符 mask 截断）；时间口径统一 UTC。
- apply 幂等：重复 apply 同建议 409；回滚后再 apply 生成新 change 行。
