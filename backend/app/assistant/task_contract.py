"""Task Contract（P4-01）：结构化任务目标与完成条件。

任务书 §9：准确度不能只靠换模型或加 System Prompt——复杂任务必须建立
可验证的完成条件（用户目标 → 结构化契约 → 执行 → 收集证据 → Verification）。

设计约束：
- 确定性推导（关键词 + 能力包），不调用模型（契约生成零成本、可测试）；
- 按任务类型声明完成检查项（§9 验证方式表），简单问答不加重验证负担
  （"不要每个简单问题都额外调用一次模型"）；
- 契约只描述"查什么"，不描述"怎么执行"——执行仍由 DeepAgents 规划。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 任务类型（§9 验证方式表）
TASK_QA = "qa"                    # 普通知识问答
TASK_RESEARCH = "research"        # 网络研究
TASK_DOC = "doc"                  # 文档生成
TASK_DATA = "data"                # Excel / 数据分析
TASK_DB = "db"                    # 数据库分析
TASK_CODE = "code"                # 代码开发
TASK_BUSINESS = "business"        # 业务操作
TASK_OTHER = "other"

# 能力包 → 任务类型（能力包是更强的信号，优先于关键词）
_CAPABILITY_TASK_TYPES = {
    "web_research": TASK_RESEARCH,
    "doc_processing": TASK_DOC,
    "data_analysis": TASK_DATA,
    "knowledge": TASK_QA,
    "coding": TASK_CODE,
    "business_automation": TASK_BUSINESS,
}

# 关键词 → 任务类型（无能力包信号时的兜底）
_KEYWORD_TASK_TYPES = [
    (r"研究|调研|竞品|行业|市场|新闻|核实|来源|文献", TASK_RESEARCH),
    (r"报告|文档|PPT|Word|PDF|方案|白皮书|写作|撰写|生成.*文件", TASK_DOC),
    (r"Excel|表格|统计|指标|图表|数据分析|汇总", TASK_DATA),
    (r"SQL|数据库|数据表|查询数据", TASK_DB),
    (r"代码|编程|脚本|重构|函数|接口实现|测试用例|bug|报错", TASK_CODE),
    (r"邮件|webhook|定时|自动化|审批|下单|外部系统|工单", TASK_BUSINESS),
]

# 各任务类型的完成检查项（§9 验证方式表 → 结构化检查）
_TASK_CHECKS = {
    TASK_QA: ["answer_present"],
    TASK_RESEARCH: ["answer_present", "citations_present"],
    TASK_DOC: ["answer_present", "artifact_produced"],
    TASK_DATA: ["answer_present", "computation_evidence"],
    TASK_DB: ["answer_present", "db_evidence"],
    TASK_CODE: ["answer_present", "exec_evidence"],
    TASK_BUSINESS: ["answer_present", "side_effect_evidence"],
    TASK_OTHER: ["answer_present"],
}

# 判定为"复杂任务"（启用 Verification 补充执行）的类型
COMPLEX_TASK_TYPES = frozenset({
    TASK_RESEARCH, TASK_DOC, TASK_DATA, TASK_DB, TASK_CODE, TASK_BUSINESS,
})


@dataclass
class TaskContract:
    """一次 Run 的结构化任务契约。"""

    goal: str                                  # 用户目标（原始输入摘要）
    task_type: str = TASK_OTHER                # 任务类型
    completion_checks: List[str] = field(default_factory=list)  # 完成检查项 ID
    forbidden: List[str] = field(default_factory=list)          # 禁止操作（预留）
    source: str = "keyword"                    # 推导来源：capability / keyword / default

    @property
    def is_complex(self) -> bool:
        return self.task_type in COMPLEX_TASK_TYPES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "task_type": self.task_type,
            "completion_checks": list(self.completion_checks),
            "forbidden": list(self.forbidden),
            "source": self.source,
        }


def infer_task_type(
    user_input: str,
    capability_ids: Optional[Sequence[str]] = None,
    *,
    tool_names: Optional[Sequence[str]] = None,
) -> tuple:
    """推断任务类型（capability > 已用工具 > 关键词 > other）。返回 (type, source)。"""
    # 1) 能力包信号（排除 general/knowledge 这类无强类型指向的包）
    for cid in (capability_ids or []):
        t = _CAPABILITY_TASK_TYPES.get(cid)
        if t and t != TASK_QA:
            return t, "capability"

    # 2) 已用工具信号（执行事实比关键词更可靠）
    tools = set(tool_names or [])
    if tools & {"db_query", "db_count", "db_aggregate", "db_schema"}:
        return TASK_DB, "tools"
    if tools & {"python_exec"}:
        return TASK_CODE, "tools"
    if tools & {"web_search", "fetch_webpage", "http_get"}:
        return TASK_RESEARCH, "tools"
    if tools & {"http_post", "send_email_tool", "webhook_notify"}:
        return TASK_BUSINESS, "tools"

    # 3) 关键词
    for pattern, t in _KEYWORD_TASK_TYPES:
        if re.search(pattern, user_input or ""):
            return t, "keyword"

    return TASK_OTHER, "default"


def build_task_contract(
    user_input: str,
    *,
    capability_ids: Optional[Sequence[str]] = None,
    tool_names: Optional[Sequence[str]] = None,
    goal_limit: int = 200,
) -> TaskContract:
    """用户目标 → 结构化任务契约（确定性，不调用模型）。"""
    goal = (user_input or "").strip()[:goal_limit]
    task_type, source = infer_task_type(user_input, capability_ids, tool_names=tool_names)
    return TaskContract(
        goal=goal,
        task_type=task_type,
        completion_checks=list(_TASK_CHECKS.get(task_type, ["answer_present"])),
        source=source,
    )
