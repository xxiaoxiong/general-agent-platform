"""测评 Judge：基于完整执行证据的主观质量评估（任务书 §4.4）。

与旧版只读最终回答不同，Judge 输入包含：
- 任务输入 / 期望 / 评分标准；
- 最终回答；
- 工具调用轨迹摘要（工具、状态、参数摘要）；
- 最终交付产物清单（工作区文件）；
- 执行状态（是否完成、是否超限等）。

Judge 失败只影响 grading_status，不覆盖 Agent 执行结果；调用方按
inconclusive 记录最终 Outcome，并保留执行证据。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.model_provider import ChatMessage, ChatRequest
from .scoring import (
    DEFAULT_PASS_THRESHOLD, ScoringError, _extract_json, _pick_judge_model,
)

JUDGE_PROMPT_VERSION = "eval-judge-v2"

JUDGE_SYSTEM_PROMPT = """你是一名严格的 Agent 评测裁判。你将看到评测用例（任务输入、期望输出、评分标准）、\
被测 Agent 的最终回答，以及执行证据（工具调用轨迹、最终交付文件、执行状态）。

请判断 Agent 是否真正完成了任务要求，并只输出一个 JSON 对象，不要输出任何其他文字：
{"score": 0-10, "passed": true 或 false, "reasoning": "简短评分理由", "evidence_refs": ["引用的证据，如工具名/文件名"]}

要求：
1. 结果导向：最终交付物和最终回答是主要依据，工具调用只是证据；
2. 执行证据显示任务未完成（如工具失败、文件缺失、状态异常）时不得判为通过；
3. 不得因为表述风格不同而扣分，语义正确即可；
4. 评分要有区分度；证据不足时在 reasoning 中明确说明。"""


def _evidence_payload(
    *,
    output_text: str,
    execution_status: str,
    tool_chain: Optional[List[Dict[str, Any]]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    max_tools: int = 30,
    max_artifacts: int = 40,
) -> Dict[str, Any]:
    tools = [
        {
            "tool": str(t.get("tool_name") or ""),
            "status": str(t.get("status") or ""),
            "args": str(t.get("args") or "")[:300],
            "error": str(t.get("error") or "")[:300] or None,
        }
        for t in (tool_chain or [])[:max_tools]
    ]
    files = [
        {
            "path": str(f.get("path") or ""),
            "size": f.get("size"),
            "sha256": f.get("sha256"),
        }
        for f in (artifacts or [])[:max_artifacts]
    ]
    return {
        "execution_status": execution_status,
        "tool_calls": tools,
        "artifacts": files,
        "output_chars": len(output_text or ""),
    }


async def judge_trial(
    db: AsyncSession,
    *,
    input_text: str,
    expected: str,
    criteria: str,
    actual_output: str,
    output_text: str,
    execution_status: str,
    tool_chain: Optional[List[Dict[str, Any]]] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """执行 Judge 评分；失败抛 ScoringError（调用方记为 grading error / inconclusive）。"""
    from app.services.model_service import ModelService
    from app.models.model_provider import ModelConfig, ModelProvider
    from sqlalchemy import select

    cfg = dict(config or {})
    provider = None
    mc = None
    mc_id = 0
    override_id = int(cfg.get("judge_model_config_id") or 0)
    if override_id:
        mc = (await db.execute(
            select(ModelConfig).where(ModelConfig.id == override_id))).scalar_one_or_none()
        if mc is not None:
            provider = (await db.execute(
                select(ModelProvider).where(ModelProvider.id == mc.provider_id)
            )).scalar_one_or_none()
            mc_id = mc.id
    if provider is None or mc is None:
        provider, mc, mc_id = await _pick_judge_model(db)
    threshold = float(cfg.get("pass_threshold") or DEFAULT_PASS_THRESHOLD)
    evidence = _evidence_payload(
        output_text=output_text, execution_status=execution_status,
        tool_chain=tool_chain, artifacts=artifacts,
    )
    # 交付物正文另行截断附加，保证 Judge 能看到真实产物内容
    artifact_excerpt = str(actual_output or "")[:8000]
    user_prompt = (
        f"# 任务输入\n{input_text[:4000] or '（无）'}\n\n"
        f"# 期望输出\n{expected[:4000] or '（无明确期望，以任务达成度为准）'}\n\n"
        f"# 评分标准\n{criteria[:2000] or '（无附加标准）'}\n\n"
        f"# 最终回答与交付物\n{artifact_excerpt or '（空）'}\n\n"
        f"# 执行证据\n{json.dumps(evidence, ensure_ascii=False)[:8000]}"
    )
    request = ChatRequest(
        model_config_id=mc_id,
        messages=[ChatMessage(role="user", content=user_prompt)],
        system_prompt=JUDGE_SYSTEM_PROMPT,
        temperature=0.1,
        max_tokens=1200,
        stream=False,
    )
    last_err: Optional[Exception] = None
    for _attempt in (1, 2):
        try:
            resp = await ModelService.chat(provider, mc, request)
            data = _extract_json(resp.get("content") or "")
            score = round(max(0.0, min(10.0, float(data.get("score")))), 2)
            passed = bool(data.get("passed")) if isinstance(data.get("passed"), bool) \
                else score >= threshold
            refs = data.get("evidence_refs")
            return {
                "score": score,
                "passed": passed,
                "detail": {
                    "method": "llm_judge",
                    "reasoning": str(data.get("reasoning") or "")[:3000],
                    "evidence_refs": refs if isinstance(refs, list) else [],
                    "judge_model_config_id": mc_id,
                    "prompt_version": JUDGE_PROMPT_VERSION,
                    "pass_threshold": threshold,
                },
                "evidence": evidence,
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ScoringError(f"llm_judge 评分失败: {last_err}")
