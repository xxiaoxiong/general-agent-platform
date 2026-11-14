# Phase 5 交付说明：旧架构清理与发布收口

> 分支 `refactor/agent-product-v3` · 2026-11-14

## 交付内容

| 批次 | Commit | 内容 |
| --- | --- | --- |
| Phase 5 | `207dd02` | 删旧 ReAct Engine（10 文件 + agent_service 垫片）+ AGENT_RUNTIME_ENABLED 开关 + 记忆双轨 + monitor 重写（-5011 行） |

## 删除清单（全仓引用扫描后执行）

- `app/services/agent/` 全模块：engine / checkpoint / context_manager /
  hallucination / memory_manager / output_parser / prompt_builder /
  tool_executor / trace（旧 ReAct Engine、文本解析式 Tool Calling）
- `app/services/agent_service.py`（兼容垫片）
- `AGENT_RUNTIME_ENABLED` 回滚开关（紧急回滚改走 Git/镜像版本回滚，§0.6）
- 旧测试：test_agent_optimizations、test_output_parser（被测对象随模块删除）

## 行为迁移

| 原路径 | 新路径 |
| --- | --- |
| v1 chat 旧引擎分支 | Durable Runtime（无条件） |
| MemoryManager SQLite 注入 + LLM 记忆抽取 | Store MemoryMiddleware（/memory/AGENTS.md，用户 namespace 隔离） |
| 记忆管理端点（/agents/memories） | memory_service（业务记忆库保留） |
| /monitor/performance（旧 Trace） | 新 Runtime 口径（agent_runs/工具账本/事件量，A11） |
| 生产校验（runtime 开关条件） | 生产一律禁 Memory Checkpointer |

## 保留决策

- **LegacyEventAdapter 保留**：v1 chat SSE 旧业务入口（P3-07 保留旧入口）
  仍消费旧契约；随 v1 入口退役一并移除。
- 旧业务入口（AI 对话 / Agent 管理 / 工作流 / 知识库）全部保留。

## 全局验收对照（§13）

| 验收 | 状态 | 证据 |
| --- | --- | --- |
| UX-01 默认助手即开即用 | ✅ | test_api_v2_workspace |
| UX-02/03 自然语言创建、配置门槛低 | ✅ | test_api_v2_assistants |
| UX-05/07/08/09 对话过程/取消/重连/审批 | ✅ | test_api_v2_runs + 前端 RunTimeline/ApprovalPanel |
| AG-04 异常不伪造成功 | ✅ | test_perf_concurrency 故障注入 |
| AG-06 大输出受限 | ✅ | test_context_limit + AT-13 |
| AG-07 完成条件 | ✅ | test_task_verification |
| AG-08 未授权能力拒绝 | ✅ | test_capability_registry（fail-closed） |
| AG-09 延迟实测 | ✅ | 8 并发 0.88s / 单 run avg 103ms / 故障收敛 0.08s |
| 不再维护旧 ReAct Loop / 平行 Checkpoint / 平行 Memory / 平行 LLM Trace | ✅ | 旧模块全删（-5011 行） |

## 测试

- 全量回归：**403 passed, 6 skipped**（6 skip = 3 真实模型 E2E + 3 MySQL 迁移）
- 前端：vue-tsc + vite build 通过

## 发布前清单（§15.1）

1. MySQL 迁移链升级验证（TEST_MYSQL_MIGRATE_SYNC_DSN，含 `c1d2e3f4a5b6`）；
2. 目标环境配置 AGENT_CHECKPOINT_DSN=postgresql（多 Worker）与
   AGENT_STORE_DSN；生产拒绝弱密钥（启动校验）；
3. 真实模型探测回写 verified 状态（P0-06 API）；
4. §15.2 安全验收：SSRF 拦截、SQL 只读、路径防护、密钥脱敏（既有用例覆盖）。
