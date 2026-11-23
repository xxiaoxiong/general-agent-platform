# API 去冗与模块化重组实施方案

## Context（背景与目标）

项目存在历史遗留的 v1/v2 双 API 分层：v1 为传统平台模块，v2 为智能体工作台+运维。调研发现三类问题：

1. **冗余/死代码**：`backend/app/api/v1/agents.py`（820 行）是大杂烩——智能体 CRUD、旧聊天契约（chat/invoke/sessions）、run 生命周期（与 v2/runs.py 重复实现）、死端点（memories/tools/monitor 前端与测试零调用）。前端 100% 走 v2 的 /runs、/conversations，v1 旧契约仅测试在用。
2. **文件组织混乱**：`app/api/v1`、`app/api/v2` 共 28 个文件平铺，v1/v2 命名误导（实际是"传统模块"与"工作台"之分）；前端 `api/index.ts` 混装 9 个模块、`extensions.ts` 混装 3 个域、`v2.ts` 混装 4 个域。
3. **超大文件职责混杂**：`agent_runtime/service.py` 2603 行、前端 `WorkspaceHome.vue` 1701 行、`Evaluation.vue` 1442 行（测评+自进化两模块混装）。

**用户已确认的决策**：
- 旧 chat/invoke/sessions 契约：**删除并迁移测试**到 v2 契约
- run 域：**全部并入 v2/runs**（reconcile 迁入；v1 run 端点全删）
- **含超大文件拆分**（service.py、WorkspaceHome.vue、Evaluation.vue）
- **目录按模块重组，HTTP URL 前缀不变**（/api/v1/*、/api/v2/* 全部保留，仅删死端点/重复端点）

**计划外说明（请评审时确认）**：v1 的 `POST /agents/runs/{id}/approve` 与 v2 `POST /runs/{id}/resume`（空 decisions 默认全批）语义完全重复，按"去冗"原则**直接删除不再迁入**，仅迁 `reconcile`。

---

## 一、v1/agents.py 去冗（URL 级删除）

端点处置清单（证据：前端仅 `frontend/src/api/extensions.ts:93` 调 `GET /agents`；测试调用见 test_api_smoke.py / test_api_idempotency_session.py；e2e `backend/tests/e2e/team_mode.cjs:620` 调 DELETE）：

| 端点 | 处置 |
|---|---|
| `POST/GET{/id}/PUT/DELETE /agents`（CRUD） | 迁到 v2/assistants（URL 换 `/api/v2/assistants`）；**v2 补 `DELETE /assistants/{id}`**（现缺失，e2e 清理需要） |
| `GET /agents`（列表） | 前端 `agentOptionApi`（extensions.ts）改调 `GET /api/v2/assistants`（返回 `{id,name}` 兼容，`assistant_to_dict` 已含） |
| `POST /agents/{id}/chat`、`/invoke`、`/sessions*`、`/messages` | **删除**（旧聊天契约，前端零调用）；测试迁移见第五节 |
| `/agents/memories`×4、`GET /agents/tools` | **删除**（前端/测试/e2e 全零调用，真死代码） |
| `/agents/monitor/*`×3 | **删除**（同上） |
| `/agents/runs/{id}` GET/events/cancel/resume | **删除**（v2/runs 已有等价端点） |
| `/agents/runs/{id}/reconcile` | **迁入** v2/runs（`POST /api/v2/runs/{run_id}/reconcile`，保留超管鉴权语义） |
| `/agents/runs/{id}/approve` | **删除**（v2 resume 已覆盖，见上方说明） |

最终 `app/api/v1/agents.py` **整文件删除**。同步：`backend/tests/e2e/team_mode.cjs` 清理调用改 `DELETE /api/v2/assistants/{id}`。

## 二、v2/runs 拆子包 + 收编 reconcile

`app/api/v2/runs.py`（618 行→收编后 ~900 行）拆为包（URL 不变）：

```
app/api/run/            # URL 前缀 /api/v2/runs（router prefix 定义不变）
  __init__.py           # 聚合 router 并 re-export（保持 from app.api.run import router 可用）
  core.py               # create / status / events / cancel / resume / input / retry
  admin.py              # reconcile（迁自 v1）/ project-repair
  review.py             # document-review context / image 两端点
  common.py             # _get_run_or_404 / _load_agent_config / _drain_legacy_stream / _attachment_note 等共享helper
```

reconcile 处理器从 v1/agents.py:703-728 迁入 `admin.py`（含 `RunReconcileRequest`），鉴权保持 `require_superuser`。注：v1 `approve` 删除后，v2 `resume` 是唯一审批入口（与项目记忆"CAS 决策/409 冲突"语义一致，已在 v2 resume 实现）。

## 三、API 目录按模块重组（URL 不变）

`main.py` 用 `include_router(x.router, prefix=API_PREFIX/API_V2_PREFIX)` 显式装配（main.py:237-264），URL = include 前缀 + router 前缀 + 路径，与包路径解耦——**重组零 URL 影响**。目标树（`app/api/deps.py` 留根）：

```
app/api/
  deps.py
  identity/    auth.py  users.py  audit.py                    → /api/v1/*
  model/       models.py  prompts.py  asr.py                   → /api/v1/*
  knowledge/   knowledge.py  databases.py                     → /api/v1/*
  workflow/    workflows.py  app_market.py                    → /api/v1/*
  assistant/   assistants.py  capabilities.py                 → /api/v2/*
  chat/        conversations.py  files.py                     → /api/v2/*
  run/         （第二节的子包）                                 → /api/v2/runs
  document/    documents.py（documents+profiles 两 router）    → /api/v2/*
  workspace/   workspace.py  artifacts.py                     → /api/v2/*
  ops/         admin.py  evaluation.py  evolution.py  skills.py  mcp.py → /api/v2/*
  common/      schemas.py  serializers.py                     （无路由）
```

- 前 4 个文件夹 = 原 v1 模块，后 6 个 = 原 v2 模块，文件夹名按产品域命名，不再出现 v1/v2 包名。
- `v2/schemas.py`、`v2/serializers.py` 的相对导入（`from .schemas import ...`）改为 `from app.api.common.schemas import ...`。
- 反向依赖修正：`services/agent_runtime/service.py:1871`、`team_project_files.py:1290`、`tests/test_api_v2_artifacts_admin.py`、`tests/test_team_project_files.py` 中 `from app.api.v2.artifacts import ...` 改新路径。
- `main.py` 装配 import 与 include 全量更新；`v1/__init__.py`、`v2/__init__.py` 删除（v2 的契约注释并入 AGENTS.md）。

## 四、service.py 拆包（Mixin 混入，测试零改动）

`app/services/agent_runtime/service.py` 2603 行改为**包**，既有导入路径 `from app.services.agent_runtime.service import ...` 全部保留（`__init__.py` re-export）。关键依据：`test_agent_runtime_durable.py` 直调 `svc._drive`/`svc._check_decisions`/`svc._record_approval_decision` 等私有方法，8+ 测试文件 import `AgentRuntimeService`/`ApprovalInvalid`/`compute_request_hash`——Mixin 混入保持 `self` 互调与实例方法查找不变，**服务层测试零改动**。

```
app/services/agent_runtime/service/
  __init__.py     # re-export：AgentRuntimeService / compute_request_hash / Approval* / get_runtime_service
  core.py         # class AgentRuntimeService(五个 Mixin) 与 _RunLimitExceeded
  _common.py      # _field/_extract_text/_derive_title/_action_from_req/_synthetic_terminal_event/
                  # _last_user_input/compute_request_hash（供 mixin 与 core 共用，避免循环导入）
  lifecycle.py    # start_run / start_run_with_session / lookup_idempotent / idempotent_replay_stream /
                  # run_to_completion / cancel_run / get_run / get_thread_state / _start / _begin_run /
                  # _recovery_check / _run_thread_id / _apply_hitl_wait_policy / _extract_snapshot_actions /
                  # _current_batch_action_ids / _build_resume_decisions
  stream.py       # stream_run / resume_run / replay_events / _drive(526行暂不再拆) / _bus_stream /
                  # _synthesize_terminal / _replay_db_stream / _consume_stream / _read_completion_review /
                  # _retain_interrupted_completion / _publish_completion_artifacts / _handle_interrupt /
                  # _close_team_board / _fail / _load_prior_final_text / _with_document_preview /
                  # _load_budget / _flush_budget
  approval.py     # check_decisions / _check_decisions / _record_approval_requests /
                  # _record_approval_decision / _match_approval_row / _expand_decisions / reconcile_run
  delivery.py     # _write_back_session / _deliver_outbox / _settle_outbox / deliver_pending_outbox
  config.py       # _load_agent_config / _load_model / _resolve_tool_specs / _load_mcp_runtime /
                  # _skill_files / _db_hint / _commit / _enrich_run_trace
```

注意：`agent_runtime/lifecycle.py` 已存在（RunHandle），故混入放 `service/` 包内不撞名。`check_decisions`（v2 resume 在用）与 `reconcile_run` 归 approval.py。

## 五、其余整理

1. **`app/assistant/` → `app/services/assistant/`**（业务逻辑归位 services 层）：机械改 import，涉及 `api/v2/{assistants,capabilities,conversations,runs,workspace}`、`services/{model_service,agent_runtime/{completion,context,model_adapter,service,workspace_delivery},evaluation/runner,evolution/evolution_advisor}`、6 个测试文件。
2. **`services/audit*` 分包**：`audit.py`、`audit_capture.py`、`audit_context.py`、`audit_http.py` → `services/audit/` 包（`__init__` re-export），同步 `main.py:30-31` 与相关测试 import。
3. **前端 `src/api/` 按模块拆分**（`utils/request.ts` 的 `createRequest` 复用不动）：
   - `index.ts` 拆出：`auth.ts users.ts audit.ts models.ts prompts.ts knowledge.ts databases.ts workflows.ts apps.ts`；`index.ts` 保留为薄 barrel re-export（既有 `from '@/api'` 调用零改动）。
   - `extensions.ts` 拆为 `assistants.ts`（agentOptionApi 改调 `/api/v2/assistants`）、`mcp.ts`、`skills.ts`。
   - `v2.ts` 拆为 `workspace.ts conversations.ts runs.ts artifacts.ts`（RunEventEnvelope/流订阅工具归 `runs.ts`）。
   - 更新调用方 import：`WorkspaceHome.vue`、`DocumentPreviewPanel.vue`、`FilePreviewPanel.vue`、`features/run/*`（`@/api/v2` → 各模块文件）、`Evaluation.vue`、`McpManage.vue`、`SkillManage.vue`（`@/api/extensions` → 新文件）。
4. **前端超大组件拆分**：
   - `views/workspace/WorkspaceHome.vue`（1701 行）：抽出 composables `useConversations.ts`（会话列表/切换/创建/删除/消息加载，约 L345-510 状态群）、`useRunExecution.ts`（发送/运行流订阅/取消/恢复/审批/activeRuns/pendingDecisions，L472+ 运行态群）、`useDocumentTask.ts`（文档任务/预览/输入请求）；WorkspaceHome.vue 保留编排与模板。
   - `views/system/Evaluation.vue`（1442 行）：拆为 tab 外壳 + `views/system/evaluation/{EvalRunsPanel,EvalDatasetsPanel,EvalResultDialog}.vue` + `views/system/evolution/{EvolutionOverviewPanel,EvolutionChangesPanel,EvolutionBriefingPanel}.vue`（state 迁入各面板，外壳只留 tab 切换与公共筛选）。
5. **测试迁移**（详见下表），`AGENTS.md` 目录结构一节同步更新（历史文档 docs/historyFile 不动）。

| 测试文件 | 迁移 |
|---|---|
| test_api_smoke.py | TestChatFlow（chat SSE/invoke）→ v2 `POST /runs` + `GET /runs/{id}/events`（幂等/事件重放）；同步 invoke 覆盖转服务层 `run_to_completion`；runs 状态/事件用例改 `/api/v2/runs/*`；TestReconcileAuthz 改 `/api/v2/runs/{id}/reconcile`；TestAgentListIsolation 改 `/api/v2/assistants`（语义为"自己的+公开的"） |
| test_api_idempotency_session.py | chat 幂等（409 冲突/复用）→ v2 `POST /runs` + Idempotency-Key；sessions → v2 `/conversations`；`POST /agents` → `POST /api/v2/assistants` |
| test_api_v2_assistants.py | 增补 `DELETE /assistants/{id}` 用例 |
| test_document_api.py、test_document_runtime.py 等 | 仅 import 路径适配（第三节） |
| e2e/team_mode.cjs | `DELETE /api/v1/agents/{id}` → `/api/v2/assistants/{id}` |

## 执行顺序

1. 第一节（v1/agents.py 去冗）+ 第二节（run 子包）—— 先完成端点整合，跑单测确认基线
2. 第五节测试迁移（test_api_smoke / test_api_idempotency_session / e2e）
3. 第三节目录重组 + 第五节-1/2（assistant、audit 包迁移）
4. 第四节 service.py 拆包
5. 第五节-3/4 前端 api 拆分 + 大组件拆分
6. AGENTS.md 更新、记忆归档

每步完成后跑受影响测试；全部完成后跑全量回归。

## 验证

1. `cd backend && python -m pytest -q` 全量通过（当前 ~550 passed/6 skipped，数量不减）。
2. `cd frontend && npm run build`（vue-tsc 类型检查）通过。
3. URL 兼容抽查：`docker compose -f docker/docker-compose.yml build backend frontend && up -d`，容器内冒烟 `GET /api/v1/models/providers`（v1 前缀）、`GET /api/v2/workspace/bootstrap`（v2 前缀）均 200；工作台发起一次对话跑通 SSE（含工具步骤展开）。
4. 确认 OpenAPI（/docs）中 `/api/v1/agents/*` 死端点消失、`/api/v2/runs/{id}/reconcile` 出现、其余 URL 不变。

## 风险提示

- 测试迁移是本次最大工作量：chat/invoke 的幂等与 SSE 重放语义在 v2 契约下等价可测（create_run 的 `lookup_idempotent`、`GET /runs/{id}/events` 的 after_seq 回放均存在），但断言细节（X-Run-Id 头、非流式返回结构）需逐个改写。
- service.py 拆包必须整包搬运同一 `AgentRuntimeService` 类的方法到 Mixin，不做任何逻辑改动；搬完后立即跑 `test_agent_runtime_durable.py`（对私有方法最敏感）验证。
- Docker 镜像烘焙代码：改完需重建镜像才生效（AGENTS 陷阱 3）。
