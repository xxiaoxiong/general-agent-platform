# Conversation Memory 部署与运维手册（阶段一）

适用范围：历史会话索引（Turn / Segment）、自动预取、Agent 主动检索工具、来源引用、
用户/项目权限隔离、删除与排除一致性。长期记忆（阶段二）设计见
`docs/architecture/conversation-memory-phase2-design.md`。

## 1. 架构与真实数据流

```text
用户消息 / Run 终态写回（agent_sessions.messages，唯一事实来源）
        │  同一事务入队
        ▼
conversation_index_jobs（pending / processing / done / failed，Lease + attempts）
        │  ConversationIndexWorker（独立轮询，默认 2s）
        ▼
ConversationIndexEntry（turn / segment，content_hash、index_version、ACL 元数据）
        │  Embedding（fastembed / OpenAI 兼容）
        ▼
Qdrant Collection `gap_memory_<model>_<dim>`（payload: user_id/project_id/visibility_scope）

Run 首轮执行（_drive）：
语义决策 → 能力/工具解析 → Retrieval Planner（低温 JSON）→ Dense + BM25 → RRF
→ 回查会话/消息/ACL → Token 预算组装 → 系统提示「历史会话参考」
→ 发布 history.sources 事件 → DeepAgents 执行

Agent 执行中：search_conversation_history / get_conversation_source 工具
→ 同一 ConversationMemoryService（每次重算 ACL）
```

关键约束：

- **原始会话是事实来源**：索引只是派生数据；检索结果必须回查
  `agent_sessions` 与 `message_id` 仍存在、仍可读。
- **不是每轮无条件检索**：有已索引历史时才调用 Planner；Planner 超时/失败/输出非法
  一律降级为“无历史上下文”，不影响 Run。
- **不是关键词硬编码**：是否检索由模型结构化判断；关键词只用于检索打分（BM25）。
- **权限服务端强制**：Planner 的 `scope_hint` 不是授权，SQL / Qdrant Filter 由服务端生成。
- **历史内容不可信**：作为独立资料块注入，不覆盖系统指令、不继承工具授权。

## 2. 数据表

| 表 | 作用 |
| --- | --- |
| `conversation_index_entries` | 两级索引条目；`entry_key` 幂等；含 `content_hash` / `index_version` / `visibility_scope` / `is_deleted` / `is_excluded` / `embedding_status` |
| `conversation_index_jobs` | Durable 索引 Job；`job_key` 唯一；status / attempts / lease_expires_at / last_error |
| `conversation_memory_settings` | 用户开关：历史检索、自动预取、项目共享读取 |
| `conversation_memory_exclusions` | 用户显式排除的会话（不参与检索） |
| `conversation_memory_shares` | 会话显式共享到项目（可撤销，记录 shared_by） |

索引版本：`CONVERSATION_MEMORY_INDEX_VERSION` + Embedding 模型 + 配置维度，
例如 `cvm1:BAAI/bge-small-zh-v1.5:512`。检索只取当前版本；换模型/维度必须递增版本。

## 3. 索引规则

- 只索引用户可见内容：用户消息 + Assistant 最终回答；**不索引** system prompt、
  工具原始 payload、隐藏推理、`steering_run_id` 临时补充消息。
- 写入前统一脱敏（`redact_secrets`）+ 去控制字符；脱敏不能替代 ACL。
- Turn：一条 Assistant 回答 + 其最近的前一条用户消息；`entry_key=turn:<assistant_message_id>`。
- Segment：以当前回答为结尾的最近 N 轮（默认 3 轮），`entry_key=seg:<sha1(message_ids)>`；
  少于 2 轮不建 segment。
- 内容哈希相同、索引版本相同且已 `indexed` 的条目重复索引不重新 Embedding。
- Embedding 失败：条目保留并标记 `degraded`，BM25 仍可检索；下次重建会重试。
- 单条正文默认截断：Turn 4000 字符 / Segment 3000 字符（可配置）。

## 4. 检索规则与阈值

1. 候选范围（SQL）：`user_id = 当前用户`，或
   `visibility_scope = project AND project_id ∈ 当前用户可读项目`；
   过滤 `is_deleted / is_excluded / index_version`，按消息时间窗（默认 730 天）
   与扫描上限（默认 4000 条）取候选。
2. Dense：Qdrant 两路过滤（本人 / 项目共享）合并取最大分；低于门槛拒答。
3. BM25：Python 中文按字 + 双字 bigram；要求强匹配（长度 ≥2 的 token）且
   IDF 覆盖率 ≥ `KNOWLEDGE_MIN_LEXICAL_COVERAGE`（默认 0.2）。
4. RRF 融合后回查数据库：会话存在、消息仍存在、共享未撤销、项目仍可读；
   失效条目置 `is_deleted` 并跳过。
5. 排序：RRF / Reranker 分数为主，当前项目匹配 +0.01；时间只做同分排序，
   不单纯按新旧排序。
6. 上下文预算：`CONVERSATION_MEMORY_CONTEXT_TOKEN_BUDGET`（默认 1200）；
   超预算截断低排名片段；无结果返回空块。

低相关拒答（按 32 条评测集校准）：

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `CONVERSATION_MEMORY_MIN_DENSE_SCORE` | 0.5 | 绝对余弦下限 |
| `CONVERSATION_MEMORY_DENSE_STRONG_SCORE` | 0.62 | 达到即视为强相关（Dense-only 保留） |
| `CONVERSATION_MEMORY_DENSE_MARGIN` | 0.04 | 最高候选领先第二名的优势下限 |

短会话的无关语义相似度常见 0.42~0.55，仅靠单一绝对阈值会把无关查询全部放行；
当前规则要求“强相关”或“领先优势明显”，在评测集上 No-answer Accuracy = 100%。

## 5. 权限模型

| Scope | 可见范围 | 授权来源 |
| --- | --- | --- |
| User History | 仅会话所有者 | `agent_sessions.user_id` |
| Project History | 会话所有者显式共享到项目后的项目成员 | `conversation_memory_shares` + `project_members` |
| 共享撤销 | 立即不可见 | `revoked_at` + 条目 `visibility_scope=user` |

- 同一 Workspace / 项目成员**不会**自动看到其他成员私人会话。
- 项目切换会自动撤销共享（避免历史随会话带入新项目）。
- 删除会话：先置 `is_deleted`（检索立即不可见）→ 删除排除/共享记录 → 异步清 Qdrant。
- 排除会话：置 `is_excluded`，不影响原始消息，只影响检索。
- 伪造来源 ID：统一按不存在/无权限返回，不泄露存在性。

## 6. 配置表

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CONVERSATION_MEMORY_ENABLED` | true | 总开关；关闭后不自动预取、不注入历史工具 |
| `CONVERSATION_MEMORY_PLANNER_MODE` | llm | `llm` 模型判断；`off` 仅保留主动检索工具 |
| `CONVERSATION_MEMORY_PLANNER_TIMEOUT` | 25 | Planner 超时（秒；部署示例 30） |
| `CONVERSATION_MEMORY_PLANNER_MAX_TOKENS` | 400 | Planner 输出上限 |
| `CONVERSATION_MEMORY_TOP_K` | 5 | 最终返回条数（1~20） |
| `CONVERSATION_MEMORY_CONTEXT_TOKEN_BUDGET` | 1200 | 历史上下文预算（≥200） |
| `CONVERSATION_MEMORY_DENSE_TOP_K` / `LEXICAL_TOP_K` | 24 / 24 | 各路候选数 |
| `CONVERSATION_MEMORY_MIN_DENSE_SCORE` | 0.5 | Dense 绝对下限 |
| `CONVERSATION_MEMORY_DENSE_STRONG_SCORE` | 0.62 | Dense 强相关阈值 |
| `CONVERSATION_MEMORY_DENSE_MARGIN` | 0.04 | Dense 相对优势 |
| `CONVERSATION_MEMORY_MAX_SCAN_ENTRIES` | 4000 | BM25 扫描上限 |
| `CONVERSATION_MEMORY_LOOKBACK_DAYS` | 730 | 检索时间窗 |
| `CONVERSATION_MEMORY_INDEX_ENABLED` | true | 索引 Worker 开关 |
| `CONVERSATION_MEMORY_INDEX_INTERVAL` | 2 | Worker 轮询间隔（秒） |
| `CONVERSATION_MEMORY_INDEX_BATCH` | 20 | 单轮处理 Job 数 |
| `CONVERSATION_MEMORY_INDEX_MAX_ATTEMPTS` | 3 | Job 最大尝试次数 |
| `CONVERSATION_MEMORY_TURN_MAX_CHARS` | 4000 | Turn 正文上限 |
| `CONVERSATION_MEMORY_SEGMENT_TURNS` | 3 | Segment 轮数 |
| `CONVERSATION_MEMORY_SEGMENT_MAX_CHARS` | 3000 | Segment 正文上限 |
| `CONVERSATION_MEMORY_QDRANT_COLLECTION_PREFIX` | gap_memory | Qdrant Collection 前缀 |
| `CONVERSATION_MEMORY_INDEX_VERSION` | cvm1 | 索引代际前缀 |

Embedding 复用知识库配置：`KNOWLEDGE_EMBEDDING_PROVIDER / MODEL / DIMENSION /
BASE_URL / API_KEY / CACHE_DIR`，Qdrant 复用 `KNOWLEDGE_QDRANT_URL / API_KEY`。

## 7. 部署与迁移

```bash
# backend/ 目录
python -m alembic upgrade head        # a1b2c3d4e5f6_add_conversation_memory
python -m pytest tests/test_conversation_memory.py tests/test_conversation_memory_api.py -q

# Docker（项目根目录）
docker compose -f docker/docker-compose.yml build backend
docker compose -f docker/docker-compose.yml up -d backend
docker logs ai_plat_backend --tail 50
```

启动后：

- `Application startup complete` 表示索引 Worker 已启动；
- `GET /api/v2/memory/settings` 返回开关与索引计数；
- `POST /api/v2/memory/reindex` 触发用户全量重建（幂等，已有 pending 任务时复用）。

### 索引重建

```bash
# 用户自助（前端/接口）
curl -X POST http://localhost/api/v2/memory/reindex -H "Authorization: Bearer <token>"

# 单次评测/运维（隔离临时库，不触碰部署数据）
cd backend
python -m scripts.conversation_memory_eval \
  --report ../docs/conversation-memory-eval-report.md
```

重建是幂等扫描：已有条目同内容不重新 Embedding；会话中已删除的消息对应条目会被清理。
换 Embedding 模型/维度时必须递增 `CONVERSATION_MEMORY_INDEX_VERSION`，旧条目不会混用。

一致性扫描复用同一重建链路：逐会话对比 `agent_sessions.messages` 与索引条目，
删除来源已不存在的条目（`index_session(prune=True)`）；检索时的来源回查是第二道防线。

## 8. 故障与恢复

| 故障 | 行为 | 恢复 |
| --- | --- | --- |
| Embedding 服务不可用 | 索引条目 `degraded`，BM25 可用；Run 不受影响 | 服务恢复后重新 reindex |
| Qdrant 不可用 | Dense 降级/跳过，BM25 继续；Job 重试 | 恢复后 reindex / Worker 自动重试 |
| Planner 超时/非法输出 | 自动预取降级为无历史上下文 | 无需处理；可调大 timeout |
| Worker 崩溃 | Job 保持 processing，Lease 过期后被重新领取 | 自动恢复 |
| Job 连续失败 | attempts 超限标记 `failed` + `last_error` | `GET /memory/status` 查看；修复后 reindex |
| 删除时 Qdrant 失败 | 条目已置删除/行已删，检索不可见；Job 重试 | Worker 重试或手动 reindex |
| 部署重启 | pending Job 自动续跑；索引条目幂等 | 无需人工 |

## 9. 指标（Prometheus）

- `conversation_memory_index_jobs_total{job_type,status}`
- `conversation_memory_index_duration_seconds{job_type}`
- `conversation_memory_retrieval_duration_seconds{mode}`
- `conversation_memory_retrieval_results`
- `conversation_memory_retrieval_empty_total{reason}`
- `conversation_memory_planner_total{result}`
- `conversation_memory_context_tokens`
- `conversation_memory_blocked_sources_total`

建议告警：`blocked_sources_total` 持续增长（索引与原始会话不一致）、
`retrieval_empty_total` 异常升高（阈值过严/索引未完成）、Job failed 非零。

## 10. 排障

1. **检索无结果**：`GET /memory/status` 看 `indexed_entries / pending_jobs / failed_jobs`；
   若为 0，确认 Worker 是否启动、`CONVERSATION_MEMORY_INDEX_ENABLED=true`，
   再触发 reindex。
2. **历史来源点不开**：确认来源会话仍存在、当前用户仍有权限；删除/撤销共享后
   卡片会被服务端拒绝并返回 404。
3. **相关历史没被注入**：查看 `history.sources` 事件是否存在；若 Planner 返回
   `no_history` 属正常，可让用户明确说“继续上次……”。
4. **无关查询返回历史**：检查 `CONVERSATION_MEMORY_MIN_DENSE_SCORE` /
   `DENSE_STRONG_SCORE` / `DENSE_MARGIN` 是否被改小；改阈值后重跑评测集。
5. **Qdrant Collection 未创建**：首次索引成功后才会创建；确认 Embedding 状态不是
   `degraded`，Qdrant 可用。

## 11. 回滚

- 功能级：`CONVERSATION_MEMORY_ENABLED=false`（自动预取与工具均关闭，原有对话不受影响）。
- 仅关闭自动预取：`CONVERSATION_MEMORY_PLANNER_MODE=off`（Agent 主动工具仍可用）。
- 索引级：停止 Worker（`CONVERSATION_MEMORY_INDEX_ENABLED=false`），
  删除 Qdrant `gap_memory_*` Collection 与 5 张新表即可；原始会话不受影响。
- 代码级：回滚 `a1b2c3d4e5f6` 迁移（仅删除新表）。
