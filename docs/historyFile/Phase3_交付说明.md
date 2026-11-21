# Phase 3 交付说明：产品工作台（默认助手 + Builder + API v2 + Chat 重构）

> 分支 `refactor/agent-product-v3` · 2026-11-14

## 交付内容

| 批次 | Commit | 内容 |
| --- | --- | --- |
| P3-01 | `634cdf1` | 默认通用助手（懒创建/general 能力包/owner 隔离）+ `agents.capability_ids/is_default`（迁移 `c1d2e3f4a5b6`）+ `/api/v2/workspace/bootstrap` + `/api/v2/capabilities` |
| P3-02 | `79fdbcd` | 自然语言 Builder（描述→草稿：名称/指令/能力包/资源提示/风险说明）+ Assistants v2 CRUD/创建即发布/publish + Conversations v2 |
| P3-03 | `708f861` | Runs v2（POST /runs / X-Run-Id / 幂等）+ §7.1 新事件协议（event_protocol.py）+ SSE `id:/event:/data:` + Last-Event-ID 续传 |
| P3-D | `e83924d` | Artifacts 安全下载（不透明令牌+归属+路径包含校验）+ admin/observability（新 Runtime 口径，仅超管） |
| P3-07 | `3ef008d` | Workflow Agent 节点统一 Durable Runtime（删旧引擎分支） |
| P3-04/05/06 | `498478a` | 统一智能体工作台 WorkspaceHome + RunTimeline + ApprovalPanel + v2 事件流消费（断线重连/Last-Event-ID）+ 取消走后端 |
| A13/A15 | `5338194` | 旧 AgentChat 页停止=后端真实取消 + SSE 解析诊断 |

## 契约（§7 API v2）

- `GET /api/v2/workspace/bootstrap`：默认助手（UX-01 懒创建幂等）+ 能力包 + 工作区配置
- `POST /api/v2/assistants/draft` → `POST /assistants`：草稿→确认→创建即发布 v1
- `POST /api/v2/runs`：创建 Run（X-Run-Id 头 / Idempotency-Key / 版本快照配置）
- `GET /api/v2/runs/{id}/events`：v2 事件流（§7.1 信封 `event/run_id/sequence/timestamp/trace_id/data`）
- `POST /api/v2/runs/{id}/cancel | /resume`：取消意图先记录；审批决策携带 action_id
- `GET /api/v2/artifacts/{id}` / `GET /api/v2/admin/observability`

### v2 事件协议（§7.1，内部事件 → 对外名称）

| 内部 | v2 | 说明 |
| --- | --- | --- |
| run_started | run.started | |
| model_delta | assistant.delta | 增量进最终回答区（§8.3 不展示原始推理） |
| tool_started/completed/failed | tool.started/completed/failed | tool_name/summary/耗时 |
| plan_updated | plan.updated | todos |
| approval_required | approval.required | actions 含 action_id |
| run_waiting_approval | run.status_changed | status=waiting_approval，流边界 |
| run_completed/failed/cancelled/suspended | run.* | 终止事件 |

## 验收对照（§13.1 UX）

| ID | 验收项 | 状态 |
| --- | --- | --- |
| UX-01 | 默认助手 | ✅ bootstrap 懒创建，登录即对话（test_bootstrap_*） |
| UX-02 | 创建助手 | ✅ 一段自然语言生成草稿（test_draft_from_natural_language） |
| UX-03 | 配置门槛 | ✅ 创建不要求 Prompt/温度/迭代数/工具（AssistantCreate 全可选） |
| UX-04 | 能力包 | ✅ 7 能力包选择 + fail-closed 校验（未登记/缺资源绑定拒绝） |
| UX-05 | 对话过程 | ✅ RunTimeline 计划/工具/状态/错误（v2 事件映射测试） |
| UX-06 | 文件 | ✅ 产物下载（归属+路径包含校验，穿越拒绝） |
| UX-07 | 取消 | ✅ 新旧两页停止均调后端 /cancel；终态明确反馈不伪造 |
| UX-08 | 重连 | ✅ consumeRunEvents 指数退避重连 + Last-Event-ID 续传（集成测试断言不重不漏） |
| UX-09 | 审批 | ✅ ApprovalPanel 按 action_id 决策；幂等/冲突/非法显式映射 |
| UX-10 | 多 Session | ✅ 会话切换互斥 + 事件写入带 token 防串扰 |

## 测试

- 新增：test_api_v2_workspace（6）、test_api_v2_assistants（15）、
  test_api_v2_runs（11，含 §7.1 协议单测）、test_api_v2_artifacts_admin（7）
- 全量回归：**434 passed, 6 skipped**（skip 口径同 Phase 2）
- 前端：`vue-tsc + vite build` 通过

## 未完成 / 风险

- AgentChat.vue（1143 行）旧页仅做 A13/A15 修复，完整拆分迁移至工作台后
  旧页随 Phase 5 清理；
- v2 Run 事件流为 DB 轮询回放（跨进程安全），模型增量批量持久化下
  实时性 ≤ delta_batch_size/轮询间隔，Phase 4 P4-05 优化；
- verification.* / run.retrying / run.recovering / artifact.created 事件名已在
  协议映射预留，Phase 4 Verification/Retry 落地时接入。
