"""LLM-as-judge：对单次 Run 的执行过程与结果做维度评分。

- 输入取平台事实（DB 的 input_payload / final_text / 确定性指标），不取
  Langfuse Trace 内容（mask 有 2000 字符截断）；
- 输出结构化 JSON（解析失败重试 1 次，再失败抛 JudgeError，由评估器标记
  failed 不阻塞）；
- judge 模型：EVOLUTION_JUDGE_MODEL_CONFIG_ID 指定，缺省复用 Run 的模型配置，
  再缺省取任一 active chat 模型。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import AgentRun
from app.models.model_provider import ModelConfig, ModelProvider
from app.schemas.model_provider import ChatMessage, ChatRequest

logger = logging.getLogger(__name__)

JUDGE_DIMENSIONS: Dict[str, str] = {
    "task_completion": "任务完成度：是否完整达成用户目标",
    "answer_quality": "答案质量：相关性、准确性、完整性、表达清晰度",
    "tool_selection": "工具选择合理性：是否选对工具、无重复/无效调用",
    "efficiency": "执行效率：步骤与资源使用是否精简",
    "groundedness": "事实性：是否有编造/幻觉，结论是否有依据",
    "format_compliance": "格式遵循：是否符合用户要求的输出格式",
}

JUDGE_SYSTEM_PROMPT = """你是一名严格的 Agent 执行评估专家。你将看到一次 AI Agent 运行的用户任务、\
最终回答和执行统计指标。请按以下维度打分（0-10，可带一位小数）：
""" + "\n".join(f"- {k}: {v}" for k, v in JUDGE_DIMENSIONS.items()) + """

要求：
1. 只输出一个 JSON 对象，不要输出任何其他文字，不要用代码块包裹；
2. 结构：{"dimensions": {"task_completion": 0-10, "answer_quality": 0-10, \
"tool_selection": 0-10, "efficiency": 0-10, "groundedness": 0-10, \
"format_compliance": 0-10}, "verdict": "pass" 或 "fail", "issues": \
[{"dimension": "维度名", "evidence": "简短证据", "suggestion": "改进建议"}], \
"confidence": 0-1};
3. verdict：任务实际达成目标为 pass，未达成为 fail；
4. issues 列出主要问题（不超过 5 条），每条给出面向 prompt/参数/工具/模型的改进建议；
5. 打分要有区分度，不要都给 7-8 分。"""


class JudgeError(Exception):
    pass


def _extract_json(text: str) -> Dict[str, Any]:
    """从模型输出提取 JSON 对象（容错：代码块 / 前后杂文）。"""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise JudgeError(f"judge 输出不含 JSON: {text[:200]}")
    return json.loads(m.group(0))


def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
    dims = data.get("dimensions")
    if not isinstance(dims, dict):
        raise JudgeError("judge 输出缺少 dimensions")
    clean_dims: Dict[str, float] = {}
    for key in JUDGE_DIMENSIONS:
        val = dims.get(key)
        if not isinstance(val, (int, float)):
            raise JudgeError(f"judge 维度 {key} 缺失或非数值")
        clean_dims[key] = round(float(val), 2)
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in ("pass", "fail"):
        raise JudgeError(f"judge verdict 非法: {verdict}")
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    confidence = data.get("confidence")
    try:
        confidence = round(max(0.0, min(1.0, float(confidence))), 2)
    except (TypeError, ValueError):
        confidence = 0.5
    overall = round(sum(clean_dims.values()) / len(clean_dims), 2)
    return {
        "dimensions": clean_dims,
        "verdict": verdict,
        "issues": issues[:5],
        "confidence": confidence,
        "overall": overall,
    }


def _task_text(run: AgentRun) -> str:
    """从输入快照取用户任务文本。"""
    payload = run.input_payload or {}
    if isinstance(payload, dict):
        messages = payload.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content") or "")[:4000]
        return str(payload.get("task") or payload.get("input") or "")[:4000]
    return str(payload)[:4000]


async def _pick_judge_model(db: AsyncSession, run: AgentRun) -> Tuple[ModelProvider, ModelConfig, int]:
    """选择 judge 模型：显式配置 > Run 模型 > 任一 active chat 模型。"""
    config_id = int(getattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0) or 0)
    if not config_id:
        snapshot = run.agent_config_snapshot or {}
        config_id = int(snapshot.get("model_config_id") or 0)
    mc: Optional[ModelConfig] = None
    if config_id:
        mc = (await db.execute(
            select(ModelConfig).where(ModelConfig.id == config_id))).scalar_one_or_none()
    if mc is None:
        mc = (await db.execute(
            select(ModelConfig).where(
                ModelConfig.is_active == True,  # noqa: E712
                ModelConfig.model_type == "chat",
            ).limit(1))).scalar_one_or_none()
    if mc is None:
        raise JudgeError("无可用 judge 模型配置")
    provider = (await db.execute(
        select(ModelProvider).where(ModelProvider.id == mc.provider_id))).scalar_one_or_none()
    if provider is None:
        raise JudgeError(f"judge 模型供应商不存在（model_config_id={mc.id}）")
    return provider, mc, mc.id


async def judge_run(db: AsyncSession, run: AgentRun,
                    deterministic: Dict[str, Any]) -> Dict[str, Any]:
    """对 Run 执行 LLM-as-judge 评估；失败重试 1 次后抛 JudgeError。"""
    from app.services.model_service import ModelService

    provider, mc, mc_id = await _pick_judge_model(db, run)
    user_prompt = (
        f"# 用户任务\n{_task_text(run) or '（无输入快照）'}\n\n"
        f"# Agent 最终回答\n{(run.final_text or '（无最终回答）')[:6000]}\n\n"
        f"# 执行统计（确定性指标）\n{json.dumps(deterministic, ensure_ascii=False)}\n\n"
        f"# 执行错误（如有）\n{run.error or '（无）'}"
    )
    request = ChatRequest(
        model_config_id=mc_id,
        messages=[ChatMessage(role="user", content=user_prompt)],
        system_prompt=JUDGE_SYSTEM_PROMPT,
        temperature=0.1,
        max_tokens=2000,
        stream=False,
    )
    last_err: Optional[Exception] = None
    for _attempt in (1, 2):
        try:
            resp = await ModelService.chat(provider, mc, request)
            data = _validate(_extract_json(resp.get("content") or ""))
            data["judge_model_config_id"] = mc_id
            return data
        except JudgeError as e:
            last_err = e
            logger.warning("judge_run(%s) 第 %s 次解析失败: %s", run.id, _attempt, e)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("judge_run(%s) 第 %s 次调用失败: %s", run.id, _attempt, e)
    raise JudgeError(f"judge_run({run.id}) 评估失败: {last_err}")
