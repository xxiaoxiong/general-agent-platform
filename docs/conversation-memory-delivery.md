# Conversation Memory 交付说明（阶段一）

任务书：`docs/General_Agent_Platform_Conversation_Memory_实施任务书.md`
交付范围：阶段 0 代码审查 + 阶段 1 历史会话检索 MVP；阶段 2 长期记忆完成设计预留。
状态：**已完成并真实部署验证**。

## 1. 完成内容

| 任务书要求 | 交付结果 |
| --- | --- |
| 审查会话存储、权限、RAG、Agent 入口、前端 | [conversation-memory-current-state.md](architecture/conversation-memory-current-state.md) |
| 会话索引模型、迁移、索引 Worker | 5 张新表 + `b6c7d8e9f0a1` 迁移 + 独立 `ConversationIndexWorker` |
| 增量索引、更新、删除、重建 | 消息写回同事务入队；删除先置标记再异步清 Qdrant；`POST /memory/reindex` 幂等重建 |
| Retrieval Planner | 模型驱动低温 JSON；超时/非法输出安全降级；不是关键词硬编码 |
| 混合检索、重排、上下文预算 | Dense(Qdrant) + BM25 + RRF + 可追溯回查 + 独立 Token 预算 |
| 普通 Agent 自动预取 | `_drive` 语义决策后、`build_agent` 前；历史可消解关键歧义 |
| Agent 主动检索工具 | `search_conversation_history` / `get_conversation_source`（只读、ToolPolicy 登记） |
| 来源引用与前端跳转 | `history.sources` 事件 + 来源卡片 + 打开原会话并定位消息 |
| User / Project / Shared 权限 | SQL + Qdrant Filter + 来源回查；项目共享显式、可撤销；越权 0 |
| 自动化功能/质量/并发/安全测试 | 14 个记忆单元/安全用例 + 1 个 API 用例 + 2 个前端用例 + 全量回归 |
| ≥30 条质量测试集与结果 | 32 条标注集；Recall@5 100%、No-answer 100%、权限违规 0 |
| 部署、迁移、重建、故障恢复 | [conversation-memory-deployment.md](conversation-memory-deployment.md) |
| 实际测试报告 | 本文件第 5~7 节 + [评测报告](conversation-memory-eval-report.md) |
| 阶段二长期记忆设计与接口预留 | [conversation-memory-phase2-design.md](architecture/conversation-memory-phase2-design.md) |

## 2. 真实调用链

```text
Run 终态提交
  → SessionResultOutbox 写回 Assistant 消息
  → 同事务 enqueue conversation_index_jobs(job_key=index:<session>:<message>)
ConversationIndexWorker（独立循环，默认 2s）
  → claim CAS + lease
  → 读取 agent_sessions.messages
  → Turn（用户+助手）/ Segment（最近 N 轮）
  → 脱敏 + 内容哈希 + 幂等 upsert conversation_index_entries
  → fastembed BGE-small-zh → Qdrant gap_memory_<model>_<dim>

下一 Run（普通 Agent 首轮）
  → 语义决策（Decision Architecture）
  → 若决策为 ambiguous：先做历史检索；有足够背景则继续，否则仍澄清
  → Retrieval Planner（低温 JSON，need_history / queries）
  → ConversationMemoryService：SQL ACL 候选 + Qdrant Dense + BM25 → RRF
  → 回查会话/消息存在性、共享未撤销、项目仍可读
  → build_history_context（不可信资料块 + Token 预算）
  → 系统提示注入 + 发布 history.sources
  → DeepAgents 执行
  → 前端来源卡片：标题/时间/范围/片段；点击打开原会话并定位 message_id
```

## 3. 数据与迁移

新增表：

- `conversation_index_entries`：Turn/Segment 两级索引、`content_hash`、`index_version`、
  `visibility_scope`、`is_deleted`、`is_excluded`、`embedding_status`；
- `conversation_index_jobs`：Durable Job（pending/processing/done/failed、attempts、lease）；
- `conversation_memory_settings`：历史检索 / 自动预取 / 项目共享读取开关；
- `conversation_memory_exclusions`：会话排除；
- `conversation_memory_shares`：会话显式共享到项目（可撤销）。

迁移：`backend/alembic/versions/b6c7d8e9f0a1_add_conversation_memory.py`
（`down_revision=f9a1b2c3d4e5`；升级/降级均幂等检查表与索引）。
已在真实 MySQL 容器执行 `f9a1b2c3d4e5 → b6c7d8e9f0a1`。

## 4. 变更文件清单

### 后端新增

- `app/models/conversation_memory.py`
- `app/services/conversation_memory/`：`repository.py`、`indexer.py`、`worker.py`、
  `planner.py`、`retrieval.py`、`context_builder.py`、`service.py`、`tools.py`、`__init__.py`
- `app/api/chat/memory.py`
- `alembic/versions/b6c7d8e9f0a1_add_conversation_memory.py`
- `scripts/conversation_memory_eval.py`
- `tests/test_conversation_memory.py`、`tests/test_conversation_memory_api.py`
- `tests/fixtures/conversation_memory_eval.json`（32 条标注查询）

### 后端修改

- `app/core/config.py`：`CONVERSATION_MEMORY_*` 配置与启动校验
- `app/models/__init__.py`：注册新模型
- `app/main.py`：挂载 `/api/v2/memory`、启动/停止索引 Worker
- `app/api/chat/conversations.py`：删除会话清理派生索引；切换项目撤销共享
- `app/api/common/schemas.py`、`app/api/run/core.py`：Run 级 `history_enabled`
- `app/services/agent_runtime/service/delivery.py`：写回同事务入队索引 Job
- `app/services/agent_runtime/service/stream.py`：自动预取、历史消解歧义、`history.sources`
- `app/services/agent_runtime/service/config.py`：历史工具面装配
- `app/services/agent_runtime/factory.py`：系统提示注入历史资料块
- `app/services/agent_runtime/context.py`：`history_context` / `history_sources`
- `app/services/agent_runtime/event_protocol.py`、`events.py`：`history.sources` 映射与存储上限
- `app/services/agent_runtime/tools/policy.py`：两个只读工具风险登记
- `app/services/tools/__init__.py`：历史工具延迟导入与 Run Context 分发
- `app/observability/metrics.py`：9 项 Conversation Memory 指标
- `tests/conftest.py`：测试进程关闭后台索引 Worker

### 前端

- 新增 `src/features/run/HistorySourcesCard.vue`
- `src/features/run/blocks.ts`：`history.sources` 归约 + `HistorySourcesBlock`
- `src/features/run/ToolStep.vue`：工具来源中的历史条目展示
- `src/views/workspace/WorkspaceHome.vue`：来源卡片、消息 `data-message-id`、跳转
- `src/views/workspace/useThreadScroll.ts`：`focusLinkedMessage`
- `src/views/workspace/useChatWorkspace.ts`：消息 ID 保留、`message` query 定位
- `src/views/workspace/chatState.ts`、`ConversationComposer.vue`：会话级「记忆」开关
- `src/views/workspace/chatDisplay.ts`：`ChatMessage.messageId`
- `src/api/conversations.ts`：`memoryApi`；`src/api/runs.ts`：`history_enabled`
- `tests/history-sources.test.mjs`

### 配置/文档

- `docker/docker-compose.yml`、`docker/.env.example`、`docker/.env`
- `AGENTS.md`：目录、命令、架构约定、陷阱 41~45
- 本文件、`docs/conversation-memory-deployment.md`、
  `docs/architecture/conversation-memory-current-state.md`、
  `docs/architecture/conversation-memory-phase2-design.md`、
  `docs/conversation-memory-eval-report.md`

## 5. 自动化测试结果（真实执行）

| 命令 | 结果 |
| --- | --- |
| `cd backend && python -m pytest -q` | **1143 passed, 13 skipped**（122.23s） |
| `python -m pytest tests/test_conversation_memory.py -q` | 14 passed |
| `python -m pytest tests/test_conversation_memory_api.py -q` | 1 passed |
| `python -m pytest tests/test_api_v2_runs.py tests/test_decision_architecture.py tests/test_goal_mode_steering.py ... -q` | 98 passed |
| `cd frontend && npm test` | 69 passed |
| `cd frontend && npm run build` | 通过（vue-tsc + vite） |
| `python -m alembic heads` | 单一 head `b6c7d8e9f0a1` |
| 临时 SQLite `alembic upgrade head` | 迁移链通过 |

覆盖的关键场景：

- Turn + Segment 构建与幂等（重复索引不重复 Embedding）；
- 跨用户隔离、项目共享/撤销、伪造来源 ID、私有会话不可见；
- 会话删除与排除立即不可见；Qdrant/DB 清理 Job；
- 来源回查会话/消息不存在时拒绝；
- Planner 合法/非法输出、模型不可用降级；
- 上下文预算、空结果不渲染；
- 工具在用户关闭历史检索后 fail-closed；
- `history.sources` 事件映射与不被 400 字符截断；
- 索引 Worker 处理 index / delete Job；
- **歧义决策 + 历史背景**：服务端先检索，有背景则继续执行（不再只输出澄清）；
- API 设置、重建、排除、分享与越权 404。

## 6. 质量评测（32 条标注集）

报告：[conversation-memory-eval-report.md](conversation-memory-eval-report.md)
（数据：`docs/conversation-memory-eval-result.json`）

| 指标 | 结果 |
| --- | --- |
| Recall@5 | **100.00%** |
| Precision@5 | **66.86%** |
| Source Accuracy | **100.00%** |
| Unsupported Memory Rate | **0.00%** |
| No-answer Accuracy | **100.00%** |
| Permission Violation | **0** |
| Retrieval Latency | P50 0ms / P95 16ms / Avg 7.34ms（本地 SQLite+Qdrant） |
| Context Token Cost | Avg 217.2 / Max 456 |

覆盖率：续接、隐含偏好、决策查找、旧方案原因、跨会话/跨时间、相似项目区分、
新旧决策冲突、无答案、相似噪声、精确原文、项目共享、权限隔离，共 13 类。
阈值 `MIN_DENSE_SCORE=0.5` / `DENSE_STRONG_SCORE=0.62` / `DENSE_MARGIN=0.04`
由该评测集校准；修改阈值后必须重跑。

## 7. 真实 Docker 部署与端到端验证

环境：`docker compose -f docker/docker-compose.yml`，多 Worker（uvicorn 2 workers），
MySQL + Qdrant + fastembed BGE-small-zh。

### 7.1 部署

- 后端/前端镜像重建成功；
- 启动日志显示迁移 `f9a1b2c3d4e5 → b6c7d8e9f0a1` 与 `Application startup complete`；
- 容器内配置核对：`CONVERSATION_MEMORY_ENABLED=true`、
  `PLANNER_MODE=llm`、`PLANNER_TIMEOUT=30`、阈值 0.5/0.62/0.04、索引 Worker 开启。

### 7.2 历史记忆真实链路

会话 213：

- 用户：`请记住：项目代号是「海燕」，部署窗口是每周三 22:00。只回复“已记录”。`
- 助手：`已记录`
- 索引状态：`indexed_entries=1`，Job `done`。

跨会话 Run（会话 215，真实模型 kimi-k3.1-pro）：

- 用户：`继续之前的讨论：我们定的项目代号和部署窗口是什么？请直接回答。`
- `task_decision`：`clear / deep_agent`；
- `history_sources` 事件：2 条来源，Planner `need_history=true`，
  queries `["项目代号","部署窗口"]`，`fallback=false`；
- 最终回答：`继续之前的讨论：项目代号是「海燕」，部署窗口是每周三 22:00。`
- 来源卡片前端实测：显示「历史上下文来源」，按会话合并（标题/时间/本人历史/片段），
  点击来源跳转 `workspace?conversation=<id>&message=<message_id>`，无页面错误。

歧义查询（会话 216）：`继续上次那个，把部署窗口再说一遍。` 同样正确回答，
说明历史背景可用时不会退回“缺少上下文”的空泛澄清。

排除验证：

- 排除会话 213 后仍能回答，因为 215/216 的回答本身也含事实（符合预期）；
- 同时排除 213/215/216/217 后再次提问，助手明确要求用户提供项目名称，
  **没有编造历史事实**；
- 恢复排除后再次检索正常。

删除一致性：

- 删除会话 211/212 后，索引条目立即不可见；
- `delete_conversation` Job `done`，DB 条目与 Qdrant points 清理成功。

### 7.3 最终镜像复验

- 最终后端镜像重建并重启，`Application startup complete`；
- 会话 219 再次跨会话提问，正确回答「海燕 / 每周三 22:00」；
- `history.sources` 事件：5 条来源（同一事实在多次验证会话中重复出现）、
  `planner.need_history=true`、`stats.budget_used=432`、`stats.injected_items=5`；
- `/metrics` 已暴露 `conversation_memory_*` 指标（索引/检索/Planner/上下文/拦截来源）。

## 8. 已知限制

1. 自动预取只接入普通 Agent 首轮（`attempt<=1`）；团队/文档执行器保持原有语义，
   恢复/续跑（`messages=None` 或 `resume`）不重新预取，已有上下文在 Checkpoint 中。
2. 项目共享当前提供 API 与权限链路，未做大规划的项目共享管理 UI；
   前端提供会话级「记忆」开关，不提供逐条记忆编辑。
3. 全文侧 BM25 在应用内计算，默认最多扫描 4000 条候选（`MAX_SCAN_ENTRIES`）；
   超大个人历史需调大窗口或后续引入数据库全文索引。
4. 评测集为 32 条人工构造的合成语料；正式上线前应补充真实企业语料并持续采样。
5. Precision@5 为 66.86%：Top-K 会包含同主题的 Segment/相邻轮次；这是多级索引的
   预期行为，不影响 Recall 与来源可追溯性。
6. 自动预取会新增一次低温 Planner 模型调用（默认超时 25s/部署 30s）；
   模型慢时会增加首 Token 延迟，但失败只降级、不会让 Run 失败。
7. 阶段二长期记忆未实现，仅完成数据模型与接口预留。

## 9. 回滚

- 功能级：`CONVERSATION_MEMORY_ENABLED=false`（自动预取与工具关闭，原对话不受影响）；
- 仅关闭自动预取：`CONVERSATION_MEMORY_PLANNER_MODE=off`；
- 停止索引：`CONVERSATION_MEMORY_INDEX_ENABLED=false`；
- 数据级：回滚 `b6c7d8e9f0a1`（仅删除 5 张新表）并删除 Qdrant `gap_memory_*` Collection；
  原始会话消息不受影响。
