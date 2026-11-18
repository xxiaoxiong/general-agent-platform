"""进化顾问：评估聚合 → 优化方向报告 → 一键应用/回滚 → 前后验证闭环。

迭代对象与统一机制：
- prompt（system_prompt）/ param（max_iterations/temperature/max_tokens）/
  model（model_config_id）→ 修改 Agent 行 + ``publish_version`` 发布新版本
  （Run 只认最新版本快照，故这是唯一生效面；快照天然是回滚镜像）；
- tools_flow（工具与执行流程）→ 报告型建议（auto_applicable=False），由人工/
  开发代理实施。
"""
import difflib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import Agent, AgentVersion
from app.models.evolution import AgentEvaluation, EvolutionChangeLog, EvolutionReport

logger = logging.getLogger(__name__)

# 参数建议的合法范围（apply 时强校验）
PARAM_RANGES: Dict[str, tuple] = {
    "max_iterations": (1, 100),
    "max_tokens": (128, 128000),
    "temperature": (0.0, 2.0),
}
PARAM_TARGETS = tuple(PARAM_RANGES.keys())


class SuggestionError(Exception):
    pass


# ────────────────────────────── 规则初筛 ──────────────────────────────


def _rule_suggestions(agent: Agent, metrics: Dict[str, Any],
                      evidence: List[str]) -> List[Dict[str, Any]]:
    """基于指标阈值的确定性优化建议（LLM 不可用时的兜底，也作为补充）。"""
    out: List[Dict[str, Any]] = []

    def add(type_: str, target: str, current: Any, proposed: Any,
            rationale: str, impact: Dict[str, Any], priority: int):
        out.append({
            "type": type_, "target": target,
            "current": current, "proposed": proposed, "diff": "",
            "rationale": rationale,
            "evidence_run_ids": evidence[:5],
            "expected_impact": impact,
            "priority": priority,
            "auto_applicable": type_ != "tools_flow",
        })

    n = metrics.get("sample_count", 0)
    if not n:
        return out

    if metrics.get("tool_error_rate", 0) >= 0.2:
        add("prompt", "system_prompt", "(见当前系统提示词)",
            "在系统提示词中增加工具使用规范：调用前校验参数、失败后读错误信息再重试、"
            "同类问题避免重复调用同一工具。",
            f"工具调用错误率 {metrics.get('tool_error_rate')} 偏高（≥0.2）",
            {"metric": "tool_error_rate", "delta": "-30%"}, 4)

    if (metrics.get("avg_tool_repeat_calls") or 0) >= 1.0:
        add("prompt", "system_prompt", "(见当前系统提示词)",
            "在系统提示词中强调：同一工具相同参数只调用一次，复用已有结果。",
            f"平均重复工具调用 {metrics.get('avg_tool_repeat_calls')} 次/Run",
            {"metric": "avg_tool_repeat_calls", "delta": "-50%"}, 3)

    if metrics.get("limit_exceeded_rate", 0) >= 0.1:
        cur = agent.max_iterations or 10
        add("param", "max_iterations", cur, min(100, cur + 10),
            f"预算超限率 {metrics.get('limit_exceeded_rate')}（≥0.1），"
            "适当提高最大迭代次数或在提示词中要求更精简的执行路径",
            {"metric": "limit_exceeded_rate", "delta": "-40%"}, 3)

    if metrics.get("success_rate", 1.0) < 0.7:
        add("prompt", "system_prompt", "(见当前系统提示词)",
            "强化任务理解与完成标准：先复述目标与验收条件，执行后自检是否满足。",
            f"任务成功率 {metrics.get('success_rate')} 偏低（<0.7）",
            {"metric": "success_rate", "delta": "+15%"}, 5)

    if (metrics.get("avg_overall") or 10) < 6:
        add("prompt", "system_prompt", "(见当前系统提示词)",
            "提升回答质量：结论先行、给出依据、覆盖用户要求的全部要点。",
            f"综合评分 {metrics.get('avg_overall')} 偏低（<6）",
            {"metric": "avg_overall", "delta": "+1.5"}, 4)

    return out


# ────────────────────────────── LLM 顾问 ──────────────────────────────

ADVISOR_SYSTEM_PROMPT = """你是一名资深 Agent 优化专家。根据一段时间内某 Agent 的评估指标与问题清单，\
给出可执行的优化建议。迭代对象：
- prompt：改写 system_prompt（proposed 必须给出【完整的新 system_prompt 全文】）；
- param：调 max_iterations / temperature / max_tokens（proposed 为具体数值）；
- model：切换 model_config_id（proposed 为整数 model_config_id，仅当确有更好选择）；
- tools_flow：工具与执行流程改进（报告型，auto_applicable=false）。

只输出 JSON 数组（不要代码块、不要其他文字），每项：
{"type": "prompt|param|model|tools_flow", "target": "system_prompt|max_iterations|temperature|max_tokens|model_config_id|...",
 "current": 当前值, "proposed": 新值, "rationale": "依据指标的简短理由",
 "evidence_run_ids": [...], "expected_impact": {"metric": "受影响指标", "delta": "预期变化"},
 "priority": 1-5, "auto_applicable": true/false}
要求：最多 8 条，按 priority 降序；建议必须与给出的指标证据相关，不要空泛建议。"""


def _extract_json_array(text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise SuggestionError(f"advisor 输出不含 JSON 数组: {text[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise SuggestionError("advisor 输出不是数组")
    return data


async def _llm_suggestions(db: AsyncSession, agent: Agent,
                           metrics: Dict[str, Any],
                           by_version: List[Dict[str, Any]],
                           issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from app.services.evolution.judge import _pick_judge_model
    from app.services.model_service import ModelService
    from app.schemas.model_provider import ChatMessage, ChatRequest

    provider, mc, mc_id = await _pick_judge_model(db, _DummyRun(agent))
    payload = {
        "current_config": {
            "system_prompt": (agent.system_prompt or "")[:3000],
            "max_iterations": agent.max_iterations,
            "temperature": str(agent.temperature or "0.7"),
            "max_tokens": agent.max_tokens,
            "model_config_id": agent.model_config_id,
        },
        "metrics": metrics,
        "metrics_by_version": by_version,
        "top_issues": issues[:10],
    }
    request = ChatRequest(
        model_config_id=mc_id,
        messages=[ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
        system_prompt=ADVISOR_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=4000,
        stream=False,
    )
    resp = await ModelService.chat(provider, mc, request)
    suggestions = _extract_json_array(resp.get("content") or "")
    clean: List[Dict[str, Any]] = []
    for s in suggestions[:8]:
        if not isinstance(s, dict) or not s.get("type") or not s.get("target"):
            continue
        s.setdefault("auto_applicable", s.get("type") != "tools_flow")
        s.setdefault("priority", 3)
        if s.get("type") == "prompt" and isinstance(s.get("proposed"), str):
            s["diff"] = _prompt_diff(agent.system_prompt or "", s["proposed"])
        clean.append(s)
    return clean


class _DummyRun:
    """复用 judge._pick_judge_model 的最小鸭子类型（只需要 agent_config_snapshot）。"""

    def __init__(self, agent: Agent):
        self.agent_config_snapshot = {"model_config_id": agent.model_config_id}


def _prompt_diff(current: str, proposed: str) -> str:
    return "\n".join(list(difflib.unified_diff(
        (current or "").splitlines(), (proposed or "").splitlines(),
        fromfile="current", tofile="proposed", lineterm=""))[:80])


# ────────────────────────────── 报告生成 ──────────────────────────────


async def generate_report(db: AsyncSession, agent_id: int,
                          window_hours: int = 168,
                          created_by: Optional[str] = None) -> EvolutionReport:
    """生成优化方向报告（聚合评估 + 规则初筛 + LLM 顾问，best-effort）。"""
    from app.services.evolution.metrics import aggregate_eval_rows

    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise SuggestionError(f"Agent {agent_id} 不存在")

    window_end = datetime.utcnow()
    window_start = window_end - timedelta(hours=window_hours)
    evals: List[AgentEvaluation] = (await db.execute(
        select(AgentEvaluation).where(
            AgentEvaluation.agent_id == agent_id,
            AgentEvaluation.created_at >= window_start,
            AgentEvaluation.status == "computed",
        ).order_by(AgentEvaluation.created_at.desc())
    )).scalars().all()

    metrics = aggregate_eval_rows(evals)

    # 分版本对比（回归检测的数据源）
    ver_rows = (await db.execute(
        select(AgentVersion.id, AgentVersion.version_no).where(
            AgentVersion.agent_id == agent_id))).all()
    ver_map = {vid: vno for vid, vno in ver_rows}
    grouped: Dict[int, List[AgentEvaluation]] = {}
    for ev in evals:
        grouped.setdefault(ev.agent_version_id or 0, []).append(ev)
    by_version = [
        {"version_no": ver_map.get(vid), "agent_version_id": vid or None,
         **aggregate_eval_rows(rows)}
        for vid, rows in sorted(grouped.items(), key=lambda kv: ver_map.get(kv[0]) or 0)
    ]

    # 问题清单（judge issues 证据）
    issues: List[Dict[str, Any]] = []
    evidence: List[str] = []
    for ev in evals:
        evidence.append(ev.run_id)
        for issue in (ev.judge_scores or {}).get("issues") or []:
            if isinstance(issue, dict):
                issues.append({**issue, "run_id": ev.run_id})

    suggestions = _rule_suggestions(agent, metrics, evidence)
    try:
        llm_items = await _llm_suggestions(db, agent, metrics, by_version, issues)
        # LLM 建议在前，规则建议去重补充（同 type+target 只留优先级高者）
        merged: List[Dict[str, Any]] = []
        seen = set()
        for s in llm_items + suggestions:
            key = (s.get("type"), s.get("target"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(s)
        suggestions = sorted(merged, key=lambda s: -int(s.get("priority") or 0))[:10]
    except Exception as e:  # noqa: BLE001
        logger.warning("advisor LLM 建议生成失败（保留规则建议）: %s", e)

    summary = _render_summary(agent, metrics, by_version, issues, suggestions, window_hours)
    report = EvolutionReport(
        agent_id=agent_id,
        window_start=window_start,
        window_end=window_end,
        baseline_version_no=(max(ver_map.values()) if ver_map else None),
        status="draft",
        summary_text=summary,
        suggestions=suggestions,
        metrics_before=metrics,
        metrics_after={},
        created_by=created_by,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


def _render_summary(agent: Agent, metrics: Dict[str, Any],
                    by_version: List[Dict[str, Any]],
                    issues: List[Dict[str, Any]],
                    suggestions: List[Dict[str, Any]],
                    window_hours: int) -> str:
    lines = [
        f"# Agent「{agent.name}」优化报告（近 {window_hours} 小时）",
        "",
        "## 总体指标",
        f"- 样本数：{metrics.get('sample_count')}｜成功率：{metrics.get('success_rate')}"
        f"｜综合分：{metrics.get('avg_overall')}｜确定性分：{metrics.get('avg_deterministic_score')}",
        f"- 效率：平均 {metrics.get('avg_steps')} 步 / {metrics.get('avg_tool_calls')} 次工具调用"
        f" / {metrics.get('avg_tokens')} tokens / {metrics.get('avg_duration_s')}s",
        f"- 工具错误率：{metrics.get('tool_error_rate')}｜重复调用：{metrics.get('avg_tool_repeat_calls')}"
        f"｜超限率：{metrics.get('limit_exceeded_rate')}",
        "",
        "## 分版本对比",
    ]
    for v in by_version:
        lines.append(
            f"- v{v.get('version_no')}: n={v.get('sample_count')} 成功率={v.get('success_rate')}"
            f" 综合分={v.get('avg_overall')} 工具错误率={v.get('tool_error_rate')}")
    lines += ["", "## 主要问题"]
    if issues:
        for i in issues[:8]:
            lines.append(f"- [{i.get('dimension', '?')}] {i.get('evidence', '')}"
                         f" → {i.get('suggestion', '')}")
    else:
        lines.append("- （暂无 judge 问题清单）")
    lines += ["", "## 优化建议"]
    if suggestions:
        for i, s in enumerate(suggestions):
            lines.append(
                f"{i + 1}. **[{s.get('type')}/{s.get('target')}]**（优先级 {s.get('priority')}）"
                f" {s.get('rationale', '')}"
                f"　预期：{json.dumps(s.get('expected_impact'), ensure_ascii=False)}"
                f"　{'可一键应用' if s.get('auto_applicable') else '需人工实施'}")
    else:
        lines.append("- （暂无建议）")
    return "\n".join(lines)


def report_to_markdown(report: EvolutionReport) -> str:
    return report.summary_text or ""


# ────────────────────────── 应用 / 回滚 / 验证 ──────────────────────────


def _write_agent_field(agent: Agent, target: str, value: Any,
                       *, strict: bool = True) -> Any:
    """把建议值写入 Agent 行字段，返回 before 值。

    strict=True（应用建议）：范围/格式非法抛 SuggestionError；
    strict=False（回滚还原）：跳过格式校验（值来自历史 before）。
    """
    if target == "system_prompt":
        if strict and (not isinstance(value, str) or len(value.strip()) < 10):
            raise SuggestionError("prompt 建议的 proposed 必须是完整提示词文本")
        before = agent.system_prompt or ""
        agent.system_prompt = value
        return before
    if target in PARAM_RANGES:
        lo, hi = PARAM_RANGES[target]
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise SuggestionError(f"{target} 建议值非数值: {value!r}")
        if not (lo <= num <= hi):
            raise SuggestionError(f"{target} 建议值 {num} 超出允许范围 [{lo}, {hi}]")
        before = getattr(agent, target)
        if target == "temperature":
            agent.temperature = str(num)
        elif target == "max_tokens":
            agent.max_tokens = int(num)
        else:
            setattr(agent, target, int(num))
        return before
    if target == "model_config_id":
        before = agent.model_config_id
        agent.model_config_id = int(value)
        return before
    raise SuggestionError(f"不支持的应用目标: {target}")


async def apply_suggestion(db: AsyncSession, report_id: int, index: int,
                           applied_by: Optional[str] = None) -> Dict[str, Any]:
    """一键应用建议：改 Agent 行 + 发布新版本 + 变更审计。"""
    from app.assistant.versions import publish_version

    report = (await db.execute(
        select(EvolutionReport).where(EvolutionReport.id == report_id)
    )).scalar_one_or_none()
    if report is None:
        raise SuggestionError(f"报告 {report_id} 不存在")
    suggestions = report.suggestions or []
    if not (0 <= index < len(suggestions)):
        raise SuggestionError(f"建议序号 {index} 越界")
    suggestion = suggestions[index]
    if not suggestion.get("auto_applicable", True):
        raise SuggestionError("该建议为报告型（tools_flow），需人工实施")
    change_type = suggestion.get("type")
    target = suggestion.get("target")
    proposed = suggestion.get("proposed")
    if change_type not in ("prompt", "param", "model"):
        raise SuggestionError(f"不支持的应用类型: {change_type}")

    agent = (await db.execute(
        select(Agent).where(Agent.id == report.agent_id))).scalar_one_or_none()
    if agent is None:
        raise SuggestionError(f"Agent {report.agent_id} 不存在")

    # model 切换前置校验：目标模型必须存在且启用
    if target == "model_config_id":
        from app.models.model_provider import ModelConfig
        mc = (await db.execute(
            select(ModelConfig).where(ModelConfig.id == int(proposed))
        )).scalar_one_or_none()
        if mc is None or not mc.is_active:
            raise SuggestionError(f"model_config_id {proposed} 不存在或未启用")

    # no-op 防护：建议值与当前值相同则拒绝，避免空转发布版本/变更审计
    if target == "system_prompt":
        unchanged = (agent.system_prompt or "").strip() == str(proposed or "").strip()
    elif target == "model_config_id":
        unchanged = str(agent.model_config_id) == str(int(proposed))
    else:
        try:
            unchanged = float(getattr(agent, target)) == float(proposed)
        except (TypeError, ValueError):
            unchanged = False
    if unchanged:
        raise SuggestionError("建议值与当前值相同，无需应用")

    before = _write_agent_field(agent, target, proposed)
    version = await publish_version(db, report.agent_id, created_by=None)
    change = EvolutionChangeLog(
        report_id=report.id,
        agent_id=report.agent_id,
        change_type=change_type,
        target=target,
        before={"value": before},
        after={"value": proposed},
        applied_version_no=version.version_no,
        status="applied",
        validation={"baseline": report.metrics_before or {}},
        applied_by=applied_by,
    )
    db.add(change)
    report.status = "applied"
    await db.commit()
    await db.refresh(change)
    return {
        "change_id": change.id,
        "new_version_no": version.version_no,
        "change_type": change_type,
        "target": target,
    }


async def rollback_change(db: AsyncSession, change_id: int,
                          applied_by: Optional[str] = None) -> Dict[str, Any]:
    """回滚：还原 before 值 + 再发布版本（历史只增不减）。"""
    from app.assistant.versions import publish_version

    change = (await db.execute(
        select(EvolutionChangeLog).where(EvolutionChangeLog.id == change_id)
    )).scalar_one_or_none()
    if change is None:
        raise SuggestionError(f"变更 {change_id} 不存在")
    if change.status == "reverted":
        raise SuggestionError("该变更已回滚")

    agent = (await db.execute(
        select(Agent).where(Agent.id == change.agent_id))).scalar_one_or_none()
    if agent is None:
        raise SuggestionError(f"Agent {change.agent_id} 不存在")

    before_value = (change.before or {}).get("value")
    _write_agent_field(agent, change.target, before_value, strict=False)
    version = await publish_version(db, change.agent_id, created_by=None)

    change.status = "reverted"
    change.reverted_at = datetime.utcnow()
    revert_row = EvolutionChangeLog(
        report_id=change.report_id,
        agent_id=change.agent_id,
        change_type=change.change_type,
        target=change.target,
        before={"value": (change.after or {}).get("value")},
        after={"value": before_value},
        applied_version_no=version.version_no,
        status="applied",
        validation={"baseline": change.validation or {}},
        revert_of_change_id=change.id,
        applied_by=applied_by,
    )
    db.add(revert_row)
    report = (await db.execute(
        select(EvolutionReport).where(EvolutionReport.id == change.report_id)
    )).scalar_one_or_none()
    if report is not None:
        report.status = "rolled_back"
    await db.commit()
    return {
        "restored_version_no": version.version_no,
        "revert_change_id": revert_row.id,
        "change_id": change.id,
    }


async def check_validations(db: AsyncSession) -> int:
    """验证闭环：变更应用后的新版本样本足够时，对比基线指标并标记验证结果。"""
    min_runs = int(getattr(settings, "EVOLUTION_VALIDATION_MIN_RUNS", 5) or 5)
    changes = (await db.execute(
        select(EvolutionChangeLog).where(EvolutionChangeLog.status == "applied")
    )).scalars().all()
    done = 0
    for change in changes:
        version = (await db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == change.agent_id,
                AgentVersion.version_no == change.applied_version_no,
            ))).scalar_one_or_none()
        if version is None:
            continue
        evals = (await db.execute(
            select(AgentEvaluation).where(
                AgentEvaluation.agent_version_id == version.id,
                AgentEvaluation.status == "computed",
            ))).scalars().all()
        if len(evals) < min_runs:
            continue
        from app.services.evolution.metrics import aggregate_eval_rows
        after = aggregate_eval_rows(evals)
        baseline = (change.validation or {}).get("baseline") or {}
        delta = {
            key: _delta(after.get(key), baseline.get(key))
            for key in ("success_rate", "avg_overall", "avg_steps",
                        "tool_error_rate", "avg_duration_s")
        }
        overall_delta = delta.get("avg_overall") or 0.0
        success_delta = delta.get("success_rate") or 0.0
        if overall_delta >= 0.3 or success_delta >= 0.05:
            verdict = "improved"
        elif overall_delta <= -0.5 or success_delta <= -0.1:
            verdict = "regressed"
        else:
            verdict = "neutral"
        change.validation = {
            **(change.validation or {}),
            "after": after,
            "delta": delta,
            "verdict": verdict,
            "sample_count": len(evals),
        }
        change.status = "validated"
        report = (await db.execute(
            select(EvolutionReport).where(EvolutionReport.id == change.report_id)
        )).scalar_one_or_none()
        if report is not None:
            report.metrics_after = after
            report.validated_at = datetime.utcnow()
            if verdict == "improved":
                report.status = "validated"
        done += 1
    if done:
        await db.commit()
    return done


def _delta(after: Optional[float], before: Optional[float]) -> Optional[float]:
    if after is None or before is None:
        return None
    return round(after - before, 4)
