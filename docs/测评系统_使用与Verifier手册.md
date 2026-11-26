# 测评系统使用与 Verifier 手册

> 配套架构与交付说明见 `docs/Agent测评系统重构_交付报告.md`。  
> 适用页面：系统管理 → Agent 测评。

## 1. 概念与数据关系

| 概念 | 说明 |
|---|---|
| Dataset | 一组测试用例（输入、期望、约束、能力维度） |
| Eval Suite | 多个 Dataset 的组合，可发布不可变版本 |
| Campaign | 一次完整测评任务（对应页面「测评任务」） |
| Trial | 某条 Case 的一次实际执行（重复执行时每轮一个 Trial） |
| Attempt | Trial 因基础设施故障进行的保守重试（Agent 自身失败不重试） |
| Grade | Verifier / Judge / 人工复核的评分记录（历史保留） |

Campaign 创建时固定 AgentVersion、SuiteVersion、模型与 Runtime 环境；之后 Agent 发布新版本不会改变已创建任务。

## 2. 数据集与用例

1. 「数据集」页新建数据集，设置默认评分方式。
2. 添加用例时填写：
   - 输入、期望输出、评分说明；
   - 复杂度 L1-L4（任务难度）；
   - 任务类型；
   - 能力维度（空则按任务类型自动映射）；
   - 必需工具、期望最少步数；
   - 多轮任务的后续用户消息（每行一条）。
3. 可导入 JSONL 或一键导入社区数据集；导入后可在「质量审计」查看确定性验证覆盖率、缺期望、未标能力、重复输入等客观问题。

### 复杂度与能力维度

- 复杂度表示任务多难：L1 基础、L2 有界、L3 复合、L4 长程。
- 能力维度表示任务测量什么：任务理解、规划与推理、工具调用、多工具协作、上下文与记忆、异常恢复、交付能力、可靠性与安全。
- 不要用执行步数、Token 数或工具调用次数推断复杂度。

## 3. 评分方式（Verifier / Judge）

用例级 `scoring_method` 覆盖数据集默认；以下为 `scoring_config` 示例。

### 文本与结构化

```json
{"scoring_method": "exact_match"}
{"scoring_method": "contains", "scoring_config": {"keywords": ["蓝绿部署"]}}
{"scoring_method": "keyword", "scoring_config": {"keywords": ["A", "B"], "match": "all"}}
{"scoring_method": "regex", "scoring_config": {"pattern": "最终答案[:：]\\s*42"}}
{"scoring_method": "json_schema", "scoring_config": {"schema": {
  "type": "object", "required": ["answer"],
  "properties": {"answer": {"type": "integer"}}
}}}
```

### 约束清单（constraint）

所有约束确定性计算；`pass_mode=all` 全部满足，`threshold` 按比例。

```json
{"scoring_method": "constraint", "scoring_config": {
  "pass_mode": "all",
  "constraints": [
    {"type": "min_length", "value": 1500},
    {"type": "max_length", "value": 40000},
    {"type": "min_headings", "value": 6},
    {"type": "min_urls", "value": 3},
    {"type": "contains", "value": "蓝绿部署"},
    {"type": "not_contains", "value": "TODO"},
    {"type": "keyword_all", "keywords": ["需求分析", "总体架构"]},
    {"type": "regex", "pattern": "回滚预案"},
    {"type": "answer", "values": ["375"]}
  ]
}}
```

`answer` 未配置 `pattern` 时提取回答中最后一个数字；也可配置 `pattern` 指定答案捕获位置。

### 工具使用（tool_usage）

```json
{"scoring_method": "tool_usage", "scoring_config": {
  "required_tools": ["calculator"],
  "forbidden_tools": ["delete_file"],
  "required_sequence": ["calculator", "write_file"]
}}
```

必需工具必须成功执行；禁用工具任何调用都会失败。

### 交付产物（artifact）

```json
{"scoring_method": "artifact", "scoring_config": {
  "required_files": ["main.py", "README.md"],
  "file_contains": [{"path": "main.py", "contains": "def main"}],
  "min_files": 2,
  "forbidden_files": ["*.tmp"],
  "pass_mode": "all"
}}
```

验证的是 Trial 工作区最终文件，不是工具调用参数。工作区不可用时回退工具调用口径。

### Mock 最终状态（mock_state）

```json
{"scoring_method": "mock_state", "scoring_config": {
  "required_calls": [
    {"tool": "create_order", "result_contains": ["created"],
     "result_json": {"data.status": "created"}}
  ],
  "forbidden_calls": ["delete_all"],
  "pass_mode": "all"
}}
```

### 工程验收（project_verification）

```json
{"scoring_method": "project_verification", "scoring_config": {
  "tool": "verify_workspace_project",
  "require_success": true,
  "require_result_contains": ["\"passed\": true"],
  "forbid_result_contains": ["\"passed\": false", "构建失败", "测试失败"]
}}
```

工程任务应在用例中声明 `required_tools: ["verify_workspace_project"]`，由 Runtime 的隔离工程验收容器执行构建/测试，测评侧检查最终结果。

### 混合评分（hybrid）

先执行确定性 Verifier 硬约束，再执行 Judge 主观质量；硬失败不会调用 Judge，也不会被 Judge 高分覆盖。

```json
{"scoring_method": "hybrid", "scoring_config": {
  "verifier": {
    "method": "constraint",
    "config": {"constraints": [{"type": "not_contains", "value": "PWNED"}]}
  },
  "pass_threshold": 6
}}
```

### LLM Judge

```json
{"scoring_method": "llm_judge", "scoring_config": {
  "pass_threshold": 6,
  "judge_model_config_id": 0
}}
```

Judge 输入包含最终回答、工具轨迹、最终产物与执行状态；输出 JSON（score/passed/reasoning/evidence_refs）。Judge 故障只会使结果变为 `inconclusive`，不会丢失执行结果。

## 4. 套件与测评任务

1. 「测评套件」新建套件，按执行顺序选择多个数据集。
2. 点击「发布版本」固定一版内容（含用例快照与 digest）。
3. 「测评任务」新建：
   - 选择单数据集或测评套件；
   - 选择 Agent；
   - 设置每条 Case 重复次数（1-10，用于 pass@k / pass^k）；
   - 可选设置单 Trial 超时、Campaign 总耗时 / 总 Token 上限。
4. 任务执行中可取消；服务重启后 Evaluation Worker 按租约恢复，已完成的 AgentRun 不会重复执行。

## 5. 状态与失败定位

| 状态 | 含义 |
|---|---|
| execution_status | Agent 执行：succeeded / agent_failed / timeout / infra_error / cancelled |
| verification_status | 确定性验证：passed / failed / unscored / error |
| grading_status | Judge / 人工：graded / unscored / error / needs_review |
| outcome | 最终结果：pass / fail / inconclusive / infra_error / cancelled / unscored |

- `inconclusive`：Agent 已完成，但评分服务不可用或证据不足；不计入 Agent 失败分母。
- `infra_error`：模型服务、网络、Sandbox 等基础设施故障。
- `unscored`：没有配置评分器，不代表失败。

结果页支持按能力维度筛选；Trial 详情查看执行事实、工具链、最终产物、评分历史与 Trace。

## 6. 重新评分、人工复核与回归

- **重新评分**：只重新执行 Verifier / Judge，不重跑 Agent，可用于更换评分方式或修复 Judge 配置。
- **人工复核**：标记需复核、人工判通过/失败、拒绝当前评分；记录写入 `eval_grades` 与 `score_detail.manual_review`。
- **沉淀回归用例**：把失败/证据不足 Trial 的用例快照复制到「回归用例（自动沉淀）」数据集，按输入去重。

## 7. Worker 与配置

```dotenv
EVALUATION_WORKER_ENABLED=true
EVALUATION_POLL_INTERVAL=5.0
EVALUATION_WORKER_BATCH=2
EVALUATION_LEASE_SECONDS=120
EVALUATION_HEARTBEAT_INTERVAL=20.0
EVALUATION_INFRA_RETRY_MAX=1
EVALUATION_JUDGE_MODEL_CONFIG_ID=0
```

- 多 Worker 通过数据库租约互斥；单进程一次执行一个 Campaign。
- 基础设施故障仅在 Trial 没有任何工具调用时保守重试（默认 1 次）。
- 存在成功工具调用/外部副作用证据时不自动重放。

## 8. 常用运行流程（Runbook）

1. 「数据集」→ 导入社区数据或创建自有数据集；
2. 「质量审计」检查验证器覆盖与标签完整性；
3. 「测评套件」→ 组合数据集 → 发布版本；
4. 「测评任务」→ 选择套件与 Agent → 设置重复次数/预算 → 创建；
5. 「测评结果」查看通过率、Case/Trial 稳定性、能力维度与问题用例；
6. 打开失败 Trial → 查看执行/验证/评分状态 → Trace 诊断；
7. 对评分存疑的 Trial 重新评分或人工复核；
8. 将确认的失败用例沉淀为回归用例；
9. 新版本发布后用同一套件版本再次测评 → 「对比」查看提升与回退。

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| Judge 故障，结果 inconclusive | 检查 Judge 模型配置/密钥；修复后在 Trial 详情重新评分 |
| 结果 infra_error | 检查模型服务、网络与 Runtime 日志；存在工具调用副作用时不会自动重试 |
| 历史任务无法恢复 | 历史任务缺少固定 AgentVersion，系统拒绝猜测版本；请新建测评任务 |
| 工程任务验证失败 | 确认用例声明 `verify_workspace_project`，并检查 project_verifier 容器与队列 |
| Campaign 被标记失败且提示 Token/耗时超限 | 调整 `campaign_max_tokens` / `campaign_timeout_s` 后新建任务 |
| 数据集无法删除 | 已被测评任务或套件引用，先解除引用 |

## 10. 安全约束

- Hidden Expected、参考答案、Verifier 内部逻辑不发送给被测 Agent。
- 产物在线预览会做敏感信息脱敏（API Key/Bearer/密码/token/URL userinfo）。
- 工作区按用户/Agent/Session 隔离，产物路径做逃逸校验。
- 工程验收使用隔离容器（无网络、只读根文件系统），任务结束先采集产物与测试结果。
