# Conversation Memory 现状审查报告（阶段 0）

审查范围：`backend/app/models`、`backend/app/api`、`backend/app/services/agent_runtime`、
`backend/app/services/knowledge`、`frontend/src/features/run`、`frontend/src/views/workspace`。
本报告只描述当前真实代码与最小改造方案，不修改既有行为。

## 1. 原始会话存储结构与读取入口

- 会话表：`agent_sessions`（`backend/app/models/agent.py`）
  - 主键 `id`；`user_id` 会话所有者；`agent_id` 绑定助手；`project_id` 可选项目归属；
    `title` / `pinned_at` / `version`（消息写入乐观锁）/ `active_run_id`（单会话执行权）。
  - **消息是单个 JSON 列 `messages`**，每个元素包含 `message_id`、`role`（user/assistant）、
    `content`、`created_at`、`attachments`、`source_run_id`（Assistant 写回幂等键）、
    `steps`（工具轨迹，前端回放用，不进入索引）。
- 读取入口：
  - 列表 `GET /api/v2/conversations`（窄列投影 + JSON 函数算条数，避免 MySQL sort buffer）。
  - 详情 `GET /api/v2/conversations/{id}`、消息 `GET /api/v2/conversations/{id}/messages`。
  - 全部按 `user_id == current_user.id` 过滤；不存在与无权限统一 404。
- 写入入口（两条）：
  - 用户消息：`SessionRepository.append_messages(...)`，在 `_begin_run` 业务事务内与 Run 创建一起提交。
  - Assistant 消息：Run 终态 → `session_result_outbox` → `_deliver_outbox` 写回，
    与 `mark_delivered` 同事务；`source_run_id` 去重、`version` CAS 合并。
- 删除：`DELETE /api/v2/conversations/{id}` 行锁校验无活跃 Run → 解除评测/Run 外键 →
  删除会话行；当前**没有派生索引清理链路**（本轮补齐）。

## 2. 用户 / 会话 / 项目 / Workspace 权限关系

- 用户：`users.owner_id` 是平台主要隔离维度；`is_superuser` 仅用于运维端点。
- 会话：只属于 `user_id`，同一项目成员**不会**因为共享 Project 自动看到彼此会话。
- 项目：`projects.owner_id` + `project_members(user_id, role)`；角色 `viewer/operator/admin`，
  统一入口 `services/sandbox/acl.py::project_role / require_project_access`。
- Workspace/Sandbox 资源：必须经项目 ACL；知识库 ACL（`services/knowledge/acl.py`）复用了项目角色。
- 结论：历史检索必须沿用同一套 ACL；`scope_hint` 只能表达检索意图，服务端重新计算范围。

## 3. 可复用的检索设施

- Embedding：`services/knowledge/embedding.py`
  - `get_embedding_provider()`（fastembed 本地 BGE-small-zh / OpenAI 兼容）、`embed_query()`、
    `get_reranker()`（可选 CrossEncoder）；维度校验、失败可重试语义已具备。
- 向量库：`services/knowledge/vector_store.py`
  - 共享 `AsyncQdrantClient`（服务地址 / 本地目录 / `:memory:`），`ensure_collection`、
    `upsert`、`search`、`delete_points`、`count` 均按 collection 名参数化，可直接复用于会话索引。
- 混合检索参考实现：`services/knowledge/retrieval.py`
  - Dense(Qdrant) + BM25(MySQL 内容表) → RRF → 内容哈希去重 → 可选 Rerank → 预算组装；
    其中 BM25 为 Python 实现（中文按字 + 双字 bigram），会话索引沿用同一 tokenizer。
- Token 预算：`services/knowledge/tokens.py::count_tokens`。
- 结论：**不引入新向量数据库**；新增独立 Qdrant collection（不污染知识库 collection），
  全文侧使用新增的会话索引表做 BM25。

## 4. 会话删除时派生数据如何清理

现状：只删除 `agent_sessions` 行，`agent_run_events` / Run 账本按 Run 查询保留，
没有向量/摘要缓存需要清理。本轮新增派生数据后，删除顺序固定为：

1. 同一事务内把该会话的索引条目置 `is_deleted=true`（检索立即不可见）；
2. 删除排除/分享记录（避免残留授权）；
3. 入队 `delete_conversation` 索引 Job；
4. Worker 删除 Qdrant 中该会话的 points。

即使 Qdrant 短暂残留，检索也会回查 `agent_sessions` 与索引行，已删除条目不会返回。

## 5. 最合适的 Agent 检索接入位置

- 执行入口：`services/agent_runtime/service/stream.py::_drive`
  - 已有：加载配置 → 语义决策（Decision Architecture）→ 工具解析 → ToolPolicy →
    Checkpointer/Store → Work Orchestrator 或 DeepAgents。
- 自动预取插入点：**语义决策之后、工具解析与 Agent 构建之前**。
  - 语义决策可能改变任务类型/约束，历史检索 Prompt 需要看到最终决策；
  - 位于 `build_agent` 之前，历史上下文可以进入系统提示；
  - 失败只降级为“无历史上下文”，不阻塞 Run（任务书 §6.5）。
- 主动检索工具接入点：
  - 工具规格：`service/config.py::_resolve_tool_specs`（按开关自动补齐，模型不可自行授权）；
  - 执行：`services/tools/__init__.py::ToolExecutor` 的 `_CONTEXT_HANDLERS`（需要 Run 身份）；
  - 风险登记：`agent_runtime/tools/policy.py`（未登记默认拒绝）；
  - 事件：`ToolAdapterFactory` 已把 `ToolResult.metadata.sources` 透传到 `tool.completed`。

## 6. 前端如何打开指定会话并定位消息

- 路由：`/workspace?conversation=<id>&run=<run_id>`；
  `useChatWorkspace.openLinkedConversation()` 监听 query 并加载会话；
  `useThreadScroll.focusLinkedRun()` 按 `[data-run-id]` 滚动到对应助手消息。
- 消息渲染：`WorkspaceHome.vue` 用户消息 `#q-<idx>`，助手消息 `[data-run-id]`；
  当前**没有 message_id 锚点**，本轮补充 `data-message-id` 与 `focusLinkedMessage()`，
  来源卡片跳转使用 `conversation` + `message` 两个 query。
- 事件归约：`features/run/blocks.ts::applyRunEvent` 已支持未知事件忽略；
  新增 `history.sources` 事件只会被新版本前端渲染，旧客户端不受影响。

## 7. 最小索引与异步任务改造方案

- 新增表（`agent_sessions` 不动字段）：
  - `conversation_index_entries`：两级索引（turn / segment）、ACL 元数据、内容哈希、
    `index_version`、`is_deleted` / `is_excluded`、`embedding_status`。
  - `conversation_index_jobs`：Durable Outbox（pending/processing/done/failed、attempts、
    lease_expires_at、last_error），消息写入/删除同事务入队。
  - `conversation_memory_settings`：用户开关（历史检索 / 自动预取）。
  - `conversation_memory_exclusions`：用户显式排除会话。
  - `conversation_memory_shares`：会话显式共享到项目（可撤销，可审计创建人）。
- 异步链路：
  - 写入/删除与 Job 入队同一事务（可靠，不依赖进程内内存事件）；
  - 独立 `ConversationIndexWorker` 轮询 Job（默认 2s），CAS 领取 + lease 过期回收；
  - Embedding 失败 → 条目 `embedding_status=degraded`，BM25 仍可用；
  - Job 有 attempts 上限与 `last_error`，服务重启后 pending 自动续跑。
- 索引版本：`cvm1:{embedding_model}:{dimension}`；条目携带版本，检索只取当前版本，
  未来换模型可按版本重建，不静默混用维度。

## 8. 依赖、迁移、性能与安全风险

| 风险 | 结论 / 处理 |
| --- | --- |
| 新增依赖 | 无：复用 fastembed / qdrant-client / SQLAlchemy |
| 数据库迁移 | 只新增表（`Base.metadata.create_all` 也会建），仍提供 Alembic 迁移供生产升级 |
| MySQL 大 JSON 排序 | 索引表不读取 `agent_sessions.messages` 做排序；BM25 查询按时间窗 + 条数上限 |
| 索引阻塞对话 | 消息写入只入队 Job；Embedding 在独立 Worker 执行，不占用请求/Worker 主循环 |
| 跨用户越权 | 检索 SQL 强制 `user_id` 或“显式共享 + 项目成员”；来源二次回查会话与消息存在性 |
| 历史注入攻击 | 历史内容作为独立不可信资料块注入，不继承工具授权、不覆盖系统指令 |
| 删除一致性 | 先置 `is_deleted` 再异步清 Qdrant；查询始终回查原始行 |
| 检索质量 | 30+ 条标注集评测 Recall@K / Source Accuracy / 权限违规；低相关不注入 |
| 索引膨胀 | Turn + Segment 两级、按内容哈希去重、每用户候选窗口上限 |

## 结论

现有会话 JSON 存储、Outbox 写回、Qdrant + Embedding + BM25、Run 事件协议与项目 ACL
足以支撑历史会话检索 MVP，无需新增强依赖或重做 RAG。按本方案新增独立索引表与 Worker，
把自动预取接入 `_drive`，把主动检索注册为受 ToolPolicy 约束的只读工具，前端新增来源卡片
与消息锚点即可满足任务书阶段一验收。
