# 长期记忆与项目记忆设计（阶段二，预留）

状态：**设计预留，未实现**。阶段一已交付历史会话检索 MVP；本文件定义阶段二的
数据模型、接口边界与演进路径，避免阶段一实现阻碍后续扩展。

## 1. 目标

在历史原文检索之上，沉淀结构化、可治理、可版本化的长期记忆：

- 用户偏好（`preference`）
- 项目事实（`project_fact`）
- 已确认决策（`decision`）
- 长期约束（`constraint`）
- 未完成事项（`pending_task`）
- 技术发现（`technical_finding`）
- 项目里程碑（`milestone`）

长期记忆是**派生索引**，不替代原始会话与阶段一的历史检索。

## 2. 数据模型（建议）

```text
long_term_memories
  id                    PK
  owner_user_id         FK users.id        # 个人记忆所有者
  project_id            FK projects.id     # 项目记忆时必填
  scope                 user | project | shared
  type                  preference | project_fact | decision | constraint
                        | pending_task | technical_finding | milestone
  content               Text
  source_conversation_id FK agent_sessions.id
  source_message_ids    JSON
  confidence            Float              # 0~1，模型推断必须 <1
  status                candidate | active | superseded | rejected | expired
  valid_from            DateTime
  valid_to              DateTime nullable
  supersedes_memory_id  FK long_term_memories.id nullable
  extraction_run_id     String(36)         # 提取任务可追溯
  content_hash          String(64)
  created_at / updated_at
```

索引：

- `(owner_user_id, status, type)`
- `(project_id, scope, status)`
- `(content_hash)` 去重
- `source_conversation_id` 级联失效

```text
long_term_memory_jobs
  id / job_key(unique) / conversation_id / user_id / job_type(extract|retry|rebuild)
  status(pending|processing|done|failed) / attempts / lease_expires_at / last_error
```

```text
long_term_memory_feedback
  id / memory_id / user_id / action(confirm|correct|reject|delete)
  corrected_content / created_at
```

## 3. 提取流程

```text
Run 终态 → Session 写回完成
→ 提取 Job 入队（与写回同一事务，复用阶段一 Outbox 表模式）
→ MemoryExtractionWorker（独立轮询）
→ 只读取已持久化、用户可见内容（user + assistant final）
→ 结构化提取（低温 JSON，严格 Schema，超时/失败降级）
→ 冲突检测（同 scope/type 的 active 记忆）
→ candidate / active 落库（保留来源）
```

要求：

- 不阻塞对话；失败可重试且不影响 Run。
- 只提取长期有价值信息，不把每条消息变成记忆。
- 用户明确确认与模型推断分开：模型推断先 `candidate`，`confidence < 1`。
- 不允许从历史会话中的指令直接生成 `constraint` 并自动生效。

## 4. 冲突与版本

- 新决策明确替代旧决策：新记忆 `active`，旧记忆 `superseded`，保留历史与来源。
- 无法判断冲突：两者都 `candidate` 或新记忆 `candidate`，等待用户确认。
- 查询“当前方案”优先 `active`；查询“历史变化”允许返回 `superseded` 并标注时间。
- 不物理覆盖；`supersedes_memory_id` 形成版本链。
- 用户纠正：写 `long_term_memory_feedback`，更新 content/status，保留审计。

## 5. 检索与注入

阶段一的 `ConversationMemoryService` 扩展为统一 `MemoryService`：

```text
MemoryService.search(...)
  ├─ 历史会话检索（阶段一，原文证据）
  └─ 长期记忆检索（阶段二，结构化结论 + 来源）
```

- 长期记忆作为独立 Context Block（“长期记忆（可纠正）”），标注类型、时间、来源；
- 与历史原文冲突时：原文证据优先，长期记忆标注可能过期；
- 用户当前明确纠正优先于所有记忆；
- 检索必须复用阶段一的 ACL：`owner_user_id` / `project_id` + 项目成员；
- 缓存 Key 必须包含 user/project/scope/index_version。

## 6. 接口预留

阶段一已提供的稳定边界：

| 接口 | 阶段二扩展 |
| --- | --- |
| `GET/PATCH /api/v2/memory/settings` | 增加 `long_term_enabled`、`project_memory_scope` |
| `GET /api/v2/memory/status` | 增加长期记忆计数 / 提取 Job 状态 |
| `POST /api/v2/memory/reindex` | 增加 `scope=conversation|long_term|all` |
| `ConversationMemoryService.search` | 增加 `include_long_term` 参数，默认 false |
| Agent 工具 `search_conversation_history` | 保持只读；新增 `search_long_term_memory` |
| `history.sources` 事件 | 增加 `kind=long_term` 来源，前端复用来源卡片 |

## 7. 治理

- 用户可查看/编辑/删除记忆，可关闭个人记忆或项目记忆。
- 记忆必须有来源；无来源的模型推断不得进入 `active`。
- 删除会话时：关联 `candidate/active` 记忆标记 `expired`（保留审计），
  或按用户设置物理删除；不允许继续被检索。
- 项目记忆范围由项目管理员设置，成员只能读取授权范围。
- 审计记录只保存操作与 ID，不重复保存敏感正文。

## 8. 阶段一已做的兼容准备

- 索引条目保留 `index_version`、`content_hash`、`source message_ids`，可作为记忆来源锚点；
- `conversation_index_jobs` 的 Durable Job 模式可直接复用于提取任务；
- 来源结构 `source_id / conversation_id / message_ids` 已被前端来源卡片消费，
  阶段二只需增加 `kind` 字段；
- 权限计算集中在 `repository.accessible_project_ids` 与 `ConversationMemoryService`，
  阶段二不新增第二套 ACL 入口。
