"""测评评分器：按用例评分方式对 Agent 实际输出打分。

评分方式（用例级覆盖数据集默认）：
- exact_match：归一化后与期望完全一致；
- contains：期望文本（或 config.keywords 任一）出现在实际输出中；
- keyword：config.keywords 全部/任一命中（config.match = all/any，默认 all）；
- regex：config.pattern 正则命中实际输出；
- llm_judge：LLM 裁判按期望与评分标准打分（0~10 + 通过判定 + 理由）；
- constraint：客观约束清单校验（长度/章节/关键词/正则/代码块/链接/答案提取等，
  全部确定性计算，无 LLM、无主观判断），score = 10 × 通过约束数/约束总数；
- none：不评分，只收集输出（score/passed 均为 None）。

统一返回：{"score": 0~10 或 None, "passed": bool 或 None, "detail": {...}}。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.model_provider import ModelConfig, ModelProvider
from app.schemas.model_provider import ChatMessage, ChatRequest

logger = logging.getLogger(__name__)

SCORING_METHODS = ("exact_match", "contains", "keyword", "regex",
                   "constraint", "llm_judge", "none")

# llm_judge 默认通过线（0~10）
DEFAULT_PASS_THRESHOLD = 6.0

JUDGE_SYSTEM_PROMPT = """你是一名严格的评测裁判。你将看到一条评测用例（任务输入、期望输出、评分标准）\
和被测 Agent 的实际输出。请判断实际输出是否达成本用例的要求。

要求：
1. 只输出一个 JSON 对象，不要输出任何其他文字，不要用代码块包裹；
2. 结构：{"score": 0-10, "passed": true 或 false, "reasoning": "简短评分理由"};
3. score 为 0-10 的整数或一位小数，10 表示与期望完全达成；
4. passed：实际达成用例要求为 true，未达成为 false；
5. 评分要有区分度，语义正确但表述不同的答案不应扣分。"""


class ScoringError(Exception):
    pass


def normalize_text(text: str) -> str:
    """归一化：去首尾空白、折叠连续空白、转小写、去首尾引号/句号。"""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.strip("。．.！!？?、,，;；:：'\"“”‘’`")
    return t


def _binary(score: bool, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {"score": 10.0 if score else 0.0, "passed": bool(score), "detail": detail}


async def _llm_judge(db: AsyncSession, *, input_text: str, expected: str,
                     criteria: str, actual: str,
                     config: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 裁判打分；输出 JSON 解析失败重试 1 次后抛 ScoringError。"""
    from app.services.model_service import ModelService

    provider, mc, mc_id = await _pick_judge_model(db)
    threshold = float(config.get("pass_threshold") or DEFAULT_PASS_THRESHOLD)
    user_prompt = (
        f"# 任务输入\n{input_text[:4000] or '（无）'}\n\n"
        f"# 期望输出\n{expected[:4000] or '（无明确期望，以任务达成度为准）'}\n\n"
        f"# 评分标准\n{criteria[:2000] or '（无附加标准）'}\n\n"
        f"# 实际输出\n{actual[:6000] or '（空）'}"
    )
    request = ChatRequest(
        model_config_id=mc_id,
        messages=[ChatMessage(role="user", content=user_prompt)],
        system_prompt=JUDGE_SYSTEM_PROMPT,
        temperature=0.1,
        max_tokens=1000,
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
            return {
                "score": score,
                "passed": passed,
                "detail": {
                    "method": "llm_judge",
                    "reasoning": str(data.get("reasoning") or "")[:2000],
                    "judge_model_config_id": mc_id,
                    "pass_threshold": threshold,
                },
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("eval llm_judge 第 %s 次失败: %s", _attempt, e)
    raise ScoringError(f"llm_judge 评分失败: {last_err}")


def _extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ScoringError(f"裁判输出不含 JSON: {text[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict) or "score" not in data:
        raise ScoringError(f"裁判输出缺少 score: {text[:200]}")
    return data


async def _pick_judge_model(db: AsyncSession) -> Tuple[ModelProvider, ModelConfig, int]:
    """选择 judge 模型：EVOLUTION_JUDGE_MODEL_CONFIG_ID > 任一 active chat 模型。"""
    config_id = int(getattr(settings, "EVOLUTION_JUDGE_MODEL_CONFIG_ID", 0) or 0)
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
        raise ScoringError("无可用 judge 模型配置")
    provider = (await db.execute(
        select(ModelProvider).where(ModelProvider.id == mc.provider_id))).scalar_one_or_none()
    if provider is None:
        raise ScoringError(f"judge 模型供应商不存在（model_config_id={mc.id}）")
    return provider, mc, mc.id


def _check_constraint(c: Dict[str, Any], actual: str) -> Tuple[bool, str]:
    """单条客观约束校验（纯字符串/正则运算）。返回 (是否通过, 约束描述)。"""
    ctype = str(c.get("type") or "").strip()
    value = c.get("value")
    if ctype == "min_length":
        ok = len(actual) >= int(value or 0)
        desc = f"输出长度 ≥ {value}（实际 {len(actual)}）"
    elif ctype == "max_length":
        ok = len(actual) <= int(value or 0)
        desc = f"输出长度 ≤ {value}（实际 {len(actual)}）"
    elif ctype == "min_headings":
        n = len(re.findall(r"(?m)^#{1,6}\s+\S", actual))
        ok = n >= int(value or 0)
        desc = f"markdown 标题数 ≥ {value}（实际 {n}）"
    elif ctype == "min_code_blocks":
        n = actual.count("```") // 2
        ok = n >= int(value or 0)
        desc = f"代码块数 ≥ {value}（实际 {n}）"
    elif ctype == "min_urls":
        n = len(re.findall(r"https?://", actual))
        ok = n >= int(value or 0)
        desc = f"引用链接数 ≥ {value}（实际 {n}）"
    elif ctype == "contains":
        ok = str(value or "") in actual
        desc = f"包含「{value}」"
    elif ctype == "not_contains":
        ok = str(value or "") not in actual
        desc = f"不包含「{value}」"
    elif ctype == "keyword_all":
        kws = [str(k) for k in (c.get("keywords") or []) if str(k).strip()]
        hit = [k for k in kws if k in actual]
        ok = len(hit) == len(kws) and bool(kws)
        desc = f"关键词全部包含 {kws}（命中 {hit}）"
    elif ctype == "keyword_any":
        kws = [str(k) for k in (c.get("keywords") or []) if str(k).strip()]
        hit = [k for k in kws if k in actual]
        ok = bool(hit)
        desc = f"关键词任一包含 {kws}（命中 {hit}）"
    elif ctype == "regex":
        pattern = str(c.get("pattern") or "")
        try:
            m = re.search(pattern, actual, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            raise ScoringError(f"constraint regex 表达式非法: {e}")
        ok = bool(m)
        desc = f"正则匹配 /{pattern[:80]}/"
    elif ctype == "answer":
        values = [normalize_text(str(v)) for v in (c.get("values") or []) if str(v).strip()]
        pattern = str(c.get("pattern") or "").strip()
        extracted = None
        if pattern:
            try:
                m = re.search(pattern, actual, re.IGNORECASE | re.MULTILINE)
            except re.error as e:
                raise ScoringError(f"constraint answer 正则非法: {e}")
            if m:
                extracted = m.group(1) if m.groups() else m.group(0)
        else:
            nums = re.findall(r"-?\d+(?:\.\d+)?", actual)
            extracted = nums[-1] if nums else None
        ok = bool(values) and normalize_text(extracted or "") in values
        desc = f"提取答案「{extracted}」∈ {values}"
    else:
        raise ScoringError(f"constraint 未知约束类型: {ctype}")
    return ok, desc


def _score_constraints(config: Dict[str, Any], actual: str) -> Dict[str, Any]:
    """客观约束清单校验（长程任务评分）：全部确定性计算。

    config.constraints: [{type, value/pattern/keywords/values}, ...]
    config.pass_mode: "all"（默认，全部满足才通过）或 "threshold"（比例达标即可）；
    config.min_ratio: threshold 模式下的通过比例（默认 0.8）。
    score = 10 × 通过约束数/约束总数。
    """
    constraints = [dict(c) for c in (config.get("constraints") or []) if isinstance(c, dict)]
    if not constraints:
        raise ScoringError("constraint 评分缺少 constraints")
    checks: List[Dict[str, Any]] = []
    for c in constraints:
        ok, desc = _check_constraint(c, actual)
        checks.append({"type": str(c.get("type") or ""), "passed": bool(ok), "desc": desc})
    hit = sum(1 for c in checks if c["passed"])
    ratio = hit / len(checks)
    pass_mode = str(config.get("pass_mode") or "all").lower()
    if pass_mode == "threshold":
        min_ratio = float(config.get("min_ratio") or 0.8)
        passed = ratio >= min_ratio
    else:
        passed = hit == len(checks)
    return {
        "score": round(ratio * 10, 2),
        "passed": passed,
        "detail": {
            "method": "constraint",
            "pass_mode": pass_mode,
            "total_constraints": len(checks),
            "passed_constraints": hit,
            "constraint_ratio": round(ratio, 4),
            "checks": checks,
        },
    }


def resolve_method(item_method: Optional[str], dataset_method: Optional[str]) -> str:
    """评分方式解析：用例级覆盖数据集默认；非法取值回落 llm_judge。"""
    method = (item_method or dataset_method or "llm_judge").strip().lower()
    return method if method in SCORING_METHODS else "llm_judge"


async def score_output(
    db: AsyncSession,
    *,
    method: str,
    config: Optional[Dict[str, Any]],
    input_text: str,
    expected: str,
    criteria: str,
    actual_output: str,
) -> Dict[str, Any]:
    """对实际输出评分。返回 {"score", "passed", "detail"}（none 方法前两者为 None）。"""
    config = config or {}
    actual = actual_output or ""

    if method == "none":
        return {"score": None, "passed": None, "detail": {"method": "none"}}

    if method == "exact_match":
        ok = normalize_text(actual) == normalize_text(expected)
        return _binary(ok, {"method": "exact_match", "expected": expected[:500]})

    if method == "contains":
        keywords = [str(k) for k in (config.get("keywords") or []) if str(k).strip()]
        if keywords:
            hit = [k for k in keywords if normalize_text(k) in normalize_text(actual)]
            return _binary(bool(hit), {"method": "contains", "hit_keywords": hit})
        ok = bool(normalize_text(expected)) and normalize_text(expected) in normalize_text(actual)
        return _binary(ok, {"method": "contains", "expected": expected[:500]})

    if method == "keyword":
        keywords = [str(k) for k in (config.get("keywords") or []) if str(k).strip()]
        if not keywords and expected:
            keywords = [k.strip() for k in re.split(r"[,，;；|\n]", expected) if k.strip()]
        if not keywords:
            raise ScoringError("keyword 评分缺少 keywords（config.keywords 或 expected）")
        norm_actual = normalize_text(actual)
        hit = [k for k in keywords if normalize_text(k) in norm_actual]
        match_mode = str(config.get("match") or "all").lower()
        ok = (len(hit) == len(keywords)) if match_mode == "all" else bool(hit)
        return _binary(ok, {"method": "keyword", "match": match_mode,
                            "hit_keywords": hit, "total_keywords": len(keywords)})

    if method == "regex":
        pattern = str(config.get("pattern") or expected or "").strip()
        if not pattern:
            raise ScoringError("regex 评分缺少 pattern（config.pattern 或 expected）")
        try:
            m = re.search(pattern, actual, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            raise ScoringError(f"regex 表达式非法: {e}")
        return _binary(bool(m), {"method": "regex", "pattern": pattern[:200],
                                 "matched": (m.group(0)[:200] if m else None)})

    if method == "constraint":
        return _score_constraints(config, actual)

    if method == "llm_judge":
        return await _llm_judge(db, input_text=input_text, expected=expected,
                                criteria=criteria, actual=actual, config=config)

    raise ScoringError(f"未知评分方式: {method}")
