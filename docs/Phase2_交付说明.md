# Phase 2 交付说明：Agent Domain / Model / Capability / Memory

> 分支 `refactor/agent-product-v3` · 2026-11-13

## 交付内容

| 批次 | Commit | 内容 |
| --- | --- | --- |
| P2-01 | `441f01c` | AgentVersion 不可变发布版本（快照含 §4.1 全部字段）+ Run 绑定 `agent_version_id`；迁移 `a9b0c1d2e3f4` |
| P2-02 | `2bbba0c` | Capability Registry：7 能力包、resolve/validate/build 接口、资源绑定门禁、fail-closed |
| P2-03 | `1712c6e` | ModelRouter（能力矩阵 + 确定性选模 + 禁止静默切换）、ModelClientFactory（连接复用）、探测回写验证状态；迁移 `b0c1d2e3f4a5` |
| P2-04 | `6e7062e` | 长期记忆：StoreBackend `/memory/` 路由（用户 namespace 隔离）+ MemoryMiddleware（AGENTS.md）+ `memory_v2.db` 八步迁移脚本 |
| P2-05/06 | `f1cb815` | Skills Registry（scope/version/来源/审计/外部目录）+ SubAgentRegistry（Profile 注册/白名单收窄/仅注册名委派） |

## 契约（Phase 3 API 依赖）

- **AgentVersion.snapshot**：`system_prompt / model_policy / capability_ids / tools /
  skill_sources / subagent_profiles / resource_bindings / memory_policy /
  execution_budget / security_policy / created_by / published_at`；
  `config_from_version()` 还原运行时配置。
- **Capability**：`resolve_capabilities(capability_ids, resource_bindings)` →
  tools/skill_sources/subagent_profiles/hitl/network/model_requirements；
  未登记能力与缺失资源绑定一律拒绝。
- **ModelRouter**：`select_model(candidates, requirements, preferred_id, require_verified)`；
  绑定模型不兼容 = 报错而非切换。
- **Memory**：`/memory/AGENTS.md`（StoreBackend，namespace `("user_memory", user_id)`）。

## 验收对照（§Phase 2 验收）

| 验收项 | 状态 |
| --- | --- |
| Agent 使用能力包 | ✅ `_resolve_tool_specs` 按 capability_ids 解析（显式 enabled=false 优先） |
| 模型按能力自动选择 | ✅ `_load_model` 未绑定模型走 Router（tool_calling 约束）；不兼容明确报错 |
| 长期记忆跨 Session 持久化 | ✅ Store 路由 + 跨 Session/用户隔离测试；迁移工具 dry-run/apply/对账 |
| Skills 可管理 | ✅ 注册表元数据/启停/scope/审计 + 外部目录 |
| SubAgent 安全委派 | ✅ Profile 注册 + 白名单 ∩ 父授权 + 空交集不暴露 |
| 所有能力受授权 | ✅ resolve fail-closed + validate_capability_policy + ToolPolicy |

## 测试

- 新增：test_agent_versions（7）、test_capability_registry（13）、
  test_model_router（11）、test_memory_store（5）、test_skills_subagents（11）
- 全量回归：**401 passed, 6 skipped**（6 skip = 3 真实模型 E2E + 3 MySQL 迁移，
  提供环境变量后通过；迁移在 MySQL 8.0.46 真实验证 upgrade 路径）

## 环境依赖项（部署验收）

- `AGENT_STORE_DSN`（postgresql）配置后跑真实迁移脚本导入 `memory_v2.db`（观察期保留旧库）；
- 真实模型探测（P0-06 API）在配置供应商后执行并回写 verified 状态。

## 未完成 / 风险

- Skills/SubAgent 的**数据库级管理界面**（启停/版本发布 UI）→ Phase 3 管理中心；
- 能力包驱动的完整资源授权（知识库/数据源的用户归属校验细化）→ Phase 3 workspace API；
- `MemoryManager` 旧 API 仍在用（迁移完成后 Phase 5 删除）。
