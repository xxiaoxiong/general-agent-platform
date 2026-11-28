# Agent Team Runtime 部署与迁移说明

## 1. 前置条件

- 数据库：MySQL 8（生产）/ SQLite（测试）；迁移由 Alembic 管理。
- Checkpoint：生产使用 PostgreSQL（`AGENT_CHECKPOINT_DSN`），多 Worker 共享。
- 模型：Team Leader/Member 均要求 `supports_function_call=true` 的 Chat 模型。

## 2. 数据表与迁移

本次新增 6 张表：

| 表 | 用途 |
|---|---|
| `agent_team_runs` | 团队 Run（`team_run_id` = `agent_runs.id`，1:1） |
| `agent_team_members` | 成员身份、Profile/模型快照、独立 Thread、预算与用量 |
| `agent_team_tasks` | Task Board（revision CAS、租约、owner、结果 Artifact） |
| `agent_team_task_dependencies` | 任务依赖（团队内唯一） |
| `agent_team_messages` | 持久化消息（幂等键、Inbox/Ack） |
| `agent_team_artifacts` | 成员成果（内容/预览/大小/SHA256） |

迁移版本：`c8d9e0f1a2b3`（前序 `b6c7d8e9f0a1`）。

```bash
cd backend
python -m alembic upgrade head
# 回退（会删除 6 张团队表；历史 Run 不受影响）
python -m alembic downgrade b6c7d8e9f0a1
```

Docker 入口脚本已先执行 `alembic upgrade head` 再启动 uvicorn；多 Worker 首启安全。

## 3. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_TEAM_RUNTIME_ENABLED` | `true` | 总开关；`false` 回滚到旧版团队执行路径 |
| `AGENT_TEAM_MAX_MEMBERS` | `4` | 单团队最大成员数（1~12） |
| `AGENT_TEAM_MAX_TASKS` | `4` | Leader 规划任务数上限（2~6，受成员数约束） |
| `AGENT_TEAM_MAX_PARALLEL` | `4` | 单轮并行成员数 |
| `AGENT_TEAM_MEMBER_MAX_MODEL_CALLS` | `8` | 成员模型调用预算 |
| `AGENT_TEAM_MEMBER_MAX_TOOL_CALLS` | `12` | 成员工具调用预算 |
| `AGENT_TEAM_MEMBER_TIMEOUT` | `300` | 成员单次执行时限（秒，0=不限） |
| `AGENT_TEAM_TASK_LEASE_SECONDS` | `120` | 任务认领租约 |
| `AGENT_TEAM_TASK_LEASE_HEARTBEAT` | `30` | 任务续租间隔（<租约 1/2） |
| `AGENT_TEAM_MAX_TASK_RETRIES` | `2` | 任务自动/显式重试上限 |
| `AGENT_TEAM_MODEL_MAX_CONCURRENCY` | `4` | 同模型并发上限（0=不限） |
| `AGENT_TEAM_MODEL_MIN_INTERVAL` | `0` | 同模型相邻调用最小间隔（秒，限流保护） |
| `AGENT_TEAM_MESSAGE_WAIT_SECONDS` | `30` | `wait_for_message` 最长等待 |
| `AGENT_TEAM_ARTIFACT_MAX_BYTES` | `200000` | Artifact 单条内容上限 |
| `AGENT_TEAM_ARTIFACT_PREVIEW_CHARS` | `2000` | Artifact 预览长度 |

`docker/.env` 与 `docker/.env.example` 已包含以上配置；`docker-compose.yml` 全部透传。

## 4. 部署

```bash
# 仓库根目录
docker compose -f docker/docker-compose.yml build backend frontend
docker compose -f docker/docker-compose.yml up -d backend frontend
docker logs ai_plat_backend --tail 50      # 确认迁移 + Application startup complete
```

验证点：

```bash
# 1) 迁移 head
cd backend && python -m alembic heads       # c8d9e0f1a2b3 (head)

# 2) 表存在（容器内）
docker exec ai_plat_mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N \
  -e "USE ai_plat; SHOW TABLES LIKE 'agent_team_%';"

# 3) 团队快照 API（需登录 token；仅 Run 所有者可读）
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8000/api/v2/runs/<run_id>/team
```

## 5. 回滚

1. **功能回滚（推荐）：** `AGENT_TEAM_RUNTIME_ENABLED=false` 后重启 backend。
   - 旧版团队路径恢复（一次性 Leader 规划 + 简化 worker）；
   - 新表与历史团队数据保留，不影响普通 Agent/文档图/软件项目图。
2. **数据回滚：** `alembic downgrade b6c7d8e9f0a1` 删除团队表。
   - 仅在没有需要保留的团队运行历史时使用；
   - 先备份 `agent_team_*` 表。

## 6. 排障

| 现象 | 排查 |
|---|---|
| 团队 Run 启动即失败 | 检查迁移是否执行；`AGENT_TEAM_RUNTIME_ENABLED`；Leader 模型是否支持工具调用 |
| 成员失败 `ModelCallLimitExceededError` | 成员模型/工具预算过小或模型在空转；查看 `agent_team_members.last_error` 与事件中的工具序列，适当提高 `AGENT_TEAM_MEMBER_MAX_*` |
| 成员失败 `ToolCallLimitExceededError` | 模型单轮批量调用超过预算；提高 `AGENT_TEAM_MEMBER_MAX_TOOL_CALLS` 或收紧工具面（角色 `tools` 白名单） |
| 供应商 429 / 空响应循环 | 配置 `AGENT_TEAM_MODEL_MIN_INTERVAL`（建议 1~2 秒）、降低并发；成员时限会中止无进展执行 |
| 任务一直 `ready` 不执行 | 检查依赖是否完成/被跳过；租约是否过期；`AGENT_TEAM_MAX_PARALLEL` |
| 任务完成被拒（租约冲突） | 旧成员租约已回收，属于预期保护；重新认领会生成新 lease_token |
| 断线重连无团队进度 | 使用 `GET /runs/{id}/team` 服务端快照；事件回放 `GET /runs/{id}/events` |
| 综合回答是规划文本 | 检查 Leader 阶段线程是否隔离（`...:synthesis`）；规划中断时 `rotate_thread` 是否执行 |

## 7. 确定性 E2E（可选）

仓库提供受控 OpenAI 兼容端点，用于不依赖外部模型验证完整 Docker 链路：

```bash
python backend/tests/e2e/team_mock_model.py --port 9999
# 在模型管理写入 provider base_url=http://host.docker.internal:9999/v1
# 创建 Run 时传 model_config_id=<mock 模型 id>、team_mode=true
```

该端点只用于链路验证，不代表真实 LLM 质量；真实模型测试记录见交付说明。
