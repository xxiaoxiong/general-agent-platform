# Conversation Memory 历史会话检索质量评测报告

- 生成时间：2026-11-27 21:48:05
- 评测集：`D:\MyPersonalFile\MyStudyFile\AI\MyCode\AI-GeneralPlat\backend\tests\fixtures\conversation_memory_eval.json`（32 条人工标注查询）
- 检索模式：`hybrid`，Top-K=5
- Embedding：`BAAI/bge-small-zh-v1.5`（维度 512），状态分布 {'indexed': 26}
- 索引：26 条（构建耗时 0.45s，总准备耗时 0.98s）

## 核心指标

| 指标 | 结果 | 说明 |
| --- | --- | --- |
| Recall@K | 100.00% | 有答案查询中 Top-K 命中期望消息的比例 |
| Precision@K | 66.86% | Top-K 返回结果中与期望消息相关的比例 |
| Source Accuracy | 100.00% | 返回来源可回查原文且权限有效 |
| Unsupported Memory Rate | 0.00% | 无来源支持的返回比例 |
| No-answer Accuracy | 100.00% | 无相关历史时保持空结果的比例 |
| Permission Violation | 0 | 跨用户/跨项目越权命中数（必须为 0） |
| Retrieval Latency | P50 0.0 ms / P95 16.0 ms / Avg 7.34 ms | 单次检索总耗时 |
| Context Token Cost | Avg 217.2 / Max 456 | 注入历史上下文的 Token 估算 |

## 分类结果

| 类别 | 查询数 | 命中 / 有答案 | 无答案正确 |
| --- | --- | --- | --- |
| 历史信息不存在 | 4 | 0 / 0 | 4 / 4 |
| 已确认技术决策 | 8 | 8 / 8 | 0 / 0 |
| 新旧决策冲突 | 2 | 2 / 2 | 0 / 0 |
| 旧方案与决策原因 | 1 | 1 / 1 | 0 / 0 |
| 明确续接历史任务 | 3 | 3 / 3 | 0 / 0 |
| 未说之前但依赖历史偏好 | 1 | 1 / 1 | 0 / 0 |
| 权限隔离 | 1 | 0 / 0 | 1 / 1 |
| 相似但不相关 | 1 | 0 / 0 | 1 / 1 |
| 相似项目区分 | 3 | 3 / 3 | 0 / 0 |
| 精确原文定位 | 3 | 3 / 3 | 0 / 0 |
| 语义改写 | 1 | 1 / 1 | 0 / 0 |
| 跨会话综合 | 1 | 1 / 1 | 0 / 0 |
| 跨时间综合 | 1 | 1 / 1 | 0 / 0 |
| 项目共享历史 | 2 | 2 / 2 | 0 / 0 |

## 失败与低分样例

无。

## 权限与来源校验

- 来源回查：100.00% 可回查原文；Unsupported Memory Rate=0.00%
- 越权命中：0 条（全部被服务端 ACL 拦截）
- 覆盖：本人历史跨用户、项目共享撤销后、伪造来源 ID、私有会话直接查询。

## 局限与说明

- 评测集为人工构造的合成语料，数量与领域覆盖有限；正式上线前应补充真实企业语料。
- Recall/Precision 以 `message_ids` 交集判定；多轮 segment 命中同一消息按相关处理。
- Context Usefulness 未做 LLM 人工评分，本报告以 Recall@K + Source Accuracy 作为客观代理指标。
- 无答案正确率受 Embedding 语义泛化影响；相似但不相关查询可能出现少量误召回。
