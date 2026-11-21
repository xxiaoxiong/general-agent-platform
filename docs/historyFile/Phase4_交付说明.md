# Phase 4 交付说明：Harness 质量、容错与性能

> 分支 `refactor/agent-product-v3` · 2026-11-14

## 交付内容

| 批次 | Commit | 内容 |
| --- | --- | --- |
| P4-01/02 | `9e0bffc` | Task Contract（用户目标→结构化契约，确定性推导）+ Verification（按任务类型验证器 + 证据收集 + 补充执行） |
| P4-03 | `ff8e548` | 工具瞬态失败按风险策略重试（safe_retry 重试 / 非幂等不盲目重放）+ run.retrying / run.recovering |
| P4-04 | `bcc4db4` | 大输出受限进上下文 + 会话工作区卸载（AG-06） |
| P4-05/06 | `7bf3317` | 单次工具超时 + 版本快照预算绑定 + 并发/故障注入/性能实测 |

## 核心机制

### Task Contract + Verification（§9）

```
用户目标 → build_task_contract(确定性: capability > 工具 > 关键词)
        → {task_type, completion_checks, forbidden}
执行 → 收集证据（工具账本 + 最终文本）
     → verify_task（零模型调用）
     → passed / partial / failed
```

- 验证器（§9 验证方式表）：answer_present / citations_present /
  artifact_produced / computation_evidence / db_evidence / exec_evidence /
  side_effect_evidence；
- 验证失败产生结构化事件（verification.started/completed），不允许模型
  说"已完成"就通过；复杂任务不通过 → run.retrying + 补充执行（限轮次），
  验证结论随 summary 交付；
- 简单问答不加验证负担（不额外调模型）。

### Retry / Recovery（§9 工具失败按风险策略）

| 契约 retry_policy | 瞬态失败 | 语义 |
| --- | --- | --- |
| safe_retry（只读） | 运行内有限重试 | 线性退避 + run.retrying |
| idempotent_key | 运行内不重发 | 账本重放层复用同一幂等键（T-05） |
| none（非幂等） | 绝不重放 | 结果未知 → 人工核对 |

### Context Engineering（AG-06）

单次工具输出 >8000 字符自动卸载到会话工作区 `tool-outputs/`
（用户/Agent/会话隔离），上下文只留 2000 字符预览 + 文件路径。

## 验收对照（§13.2 AG）

| 验收项 | 状态 |
| --- | --- |
| AG-04 异常/超时/预算不泄漏、不伪造成功 | ✅ 故障注入测试：模型故障→failed 明确错误；预算耗尽→limit_exceeded |
| AG-06 大输出/大文件不无控制进上下文 | ✅ limit_for_context 卸载 + AT-13 既有 DeepAgents 卸载 |
| AG-07 复杂任务有完成条件 | ✅ Task Contract + verify_task |
| AG-08 未授权能力被拒绝 | ✅ Phase 2 resolve_capabilities fail-closed |
| AG-09 延迟与成功率实测 | ✅ 实测：8 并发 0.88s / 单 run 平台开销 avg 103ms p95 94ms / 故障收敛 0.08s / 恢复成功率 3/3 |

## 测试

- 新增：test_task_verification（14）、test_tool_retry（10）、
  test_context_limit（4）、test_perf_concurrency（6）
- 全量回归：**468 passed, 6 skipped**

## 关键修复

- **LangGraph config 陷阱**（消融实验定位）：configurable 同时携带
  `run_id`+`user_id` 时后续 astream 被静默跳过（0 chunk、模型不调用、
  无异常）——补充执行 config 只含 thread_id；
- 版本快照 `execution_budget` 此前未被 Runtime 遵守 → 已绑定。

## 未完成 / 风险

- Verification 补充执行当前一轮为主（AGENT_VERIFICATION_MAX_ROUNDS 可调）；
  多轮自主修复属后续增强；
- 性能实测基于 FakeModel（平台开销口径）；真实模型端到端延迟依赖部署环境，
  发布前需在目标环境跑一轮 §15.1 部署验收。
