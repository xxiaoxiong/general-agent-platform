# Phase 1 可观测性交付说明

> Langfuse + OpenTelemetry + Prometheus · 分支 `refactor/agent-product-v3`
> 2026-11-13

## 交付内容

| 批次 | Commit | 内容 |
| --- | --- | --- |
| P1-01 | `80ad5d4` + `6693f48` | Langfuse Python SDK v5.2.0 接入：CallbackHandler 合并、确定性 trace_id（`Langfuse.create_trace_id(seed="run:{run_id}")`，跨 Worker/恢复同 Trace）、平台 Span（agent_run/worker_claim/session_writeback）、mask 脱敏、sample_rate 采样、批量缓冲、shutdown 冲刷 |
| P1-02 | `263cf5e` | OTel：TracerProvider + OTLP gRPC BatchSpanProcessor、FastAPI/HTTPX/Redis/SQLAlchemy instrumentation、W3C TraceContext；各组件独立降级 |
| P1-03 | `d9240c4` | 平台 Span 补齐：hitl_approval；Graph/Model/Tool/SubAgent 由 CallbackHandler 自动嵌套 |
| P1-04 | 本批 | mask 大输出截断（>2000 字符），叠加既有：敏感字段脱敏、采样、异步批量有界缓冲、导出失败不影响执行 |
| P1-05 | `d9240c4` | 任务书 §5.6 全部 15 个 Prometheus 指标 + `/metrics` 端点 + Grafana 8 类面板（`docker/grafana/ai-generalplat-runtime.json`） |

## Trace 结构（任务书 §5.3 对齐）

```text
Trace: run:{run_id}（确定性派生，跨进程一致）
 └── agent_run（platform_span）
      ├── worker_claim / hitl_approval / session_writeback（platform_span）
      ├── Agent Graph（Langfuse CallbackHandler 自动嵌套）
      │    ├── Model Invocation（含 Token/延迟）
      │    ├── Tool Call / Result
      │    └── SubAgent
      └── ModelUsageCallback（预算计数，独立于 Trace）
```

## 指标汇集点

| 指标 | 采集点 |
| --- | --- |
| agent_runs_total / duration | `service._drive` finally |
| agent_queue_wait / recovery | `worker.claim_run` |
| agent_active_runs | `worker._poll_loop` 每轮刷新 |
| agent_tool_calls_total | `EventBus.publish`（工具终态事件，覆盖 adapter/内部/子代理） |
| agent_model_calls / tokens | `ModelUsageCallback._finish` |
| agent_sse_reconnect_total | SSE 回放端点（after_seq/Last-Event-ID） |
| agent_lease_lost_total | `service._drive` RunLeaseLost 分支 |
| event_lag / checkpoint_write / tool_duration | 定义就绪，随 Phase 4 Retry/Recovery 与持久化重构补采集点 |

## 验证

- 单测：`tests/test_observability_langfuse.py`（11）+ `tests/test_otel_tracing.py`（5）+ `tests/test_metrics.py`（3）
- 全量回归：353 passed, 6 skipped（本批后 354）
- 环境依赖项（需真实服务，属部署验收）：
  - 配置 `LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL` 后，一次真实 Run 的完整 Trace 验收（§Phase 1 验收）——参照真实模型 E2E 同口径披露环境；
  - OTLP 端点接 Prometheus/Grafana 后的面板渲染验收。

## 隐私（§5.5 对齐）

- mask 递归脱敏：api_key/secret/token/password/authorization/cookie 等字段一律 `***REDACTED***`；
- 超长内容（>2000 字符）截断后才入 Trace（大文件/大工具输出不入 Trace）；
- Trace 导出失败仅丢观测数据（Langfuse SDK 后台批量 + OTel BatchSpanProcessor），不影响 Agent 执行；
- 采样：`LANGFUSE_TRACING_SAMPLE_RATE`；保留期/清理/访问权限由 Langfuse 服务端配置（自托管部署项）。
