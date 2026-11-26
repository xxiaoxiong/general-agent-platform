"""确定性 Verifier 注册表（任务书 §4.4 / 阶段 3）。

Verifier 只验证客观事实，不调用 LLM：
- 文本匹配：exact_match / contains / keyword / regex；
- 结构化输出：json_schema（内置最小 Schema 校验，无额外依赖）；
- 约束清单：constraint（长度/章节/关键词/正则/链接/答案提取等）；
- 工具使用：tool_usage（必需工具、禁用工具、调用顺序）；
- 交付产物：artifact（文件存在、最小数量、文件内容包含）；
- none：未配置验证器（unscored，不得当作 Agent 失败）。

统一返回：
{"status": "passed"|"failed"|"unscored"|"error",
 "score": float|None, "passed": bool|None,
 "detail": {...}, "evidence": {...}}
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .scoring import ScoringError, normalize_text, score_output

VERIFIER_METHODS = (
    "exact_match", "contains", "keyword", "regex", "constraint",
    "json_schema", "tool_usage", "artifact", "mock_state",
    "project_verification", "none",
)


@dataclass
class VerifyContext:
    method: str
    config: Dict[str, Any] = field(default_factory=dict)
    input_text: str = ""
    expected: str = ""
    criteria: str = ""
    actual_output: str = ""          # 最终回答 + 交付物合并文本
    output_text: str = ""            # 最终回答原文
    tool_chain: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    workspace_text: str = ""


def _result(status: str, *, score: Optional[float] = None,
            passed: Optional[bool] = None, detail: Optional[Dict[str, Any]] = None,
            evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "status": status,
        "score": score,
        "passed": passed,
        "detail": detail or {},
        "evidence": evidence or {},
    }


# ────────────────────────────── JSON Schema ──────────────────────────────


def extract_json(text: str) -> Any:
    """从模型输出中提取 JSON（允许 ```json 代码块 / 前后说明文字）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("输出为空")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(raw)


def _validate_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """最小 JSON Schema 校验（type/required/properties/items/enum/长度/数值范围）。"""
    errors: List[str] = []
    expected_type = schema.get("type")
    if expected_type:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        type_ok = False
        for t in types:
            if t == "object" and isinstance(value, dict):
                type_ok = True
            elif t == "array" and isinstance(value, list):
                type_ok = True
            elif t == "string" and isinstance(value, str):
                type_ok = True
            elif t == "integer" and isinstance(value, int) and not isinstance(value, bool):
                type_ok = True
            elif t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                type_ok = True
            elif t == "boolean" and isinstance(value, bool):
                type_ok = True
            elif t == "null" and value is None:
                type_ok = True
        if not type_ok:
            return [f"{path}: 类型应为 {expected_type}，实际 {type(value).__name__}"]

    if "enum" in schema and value not in (schema.get("enum") or []):
        errors.append(f"{path}: 值不在枚举范围 {schema['enum']}")

    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}: 缺少必填字段 {key}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                errors.extend(_validate_schema(value[key], sub, f"{path}.{key}"))
    elif isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path}: 数组长度 {len(value)} < {schema['minItems']}")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 数组长度 {len(value)} > {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(_validate_schema(item, item_schema, f"{path}[{idx}]"))
    elif isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path}: 字符串长度 {len(value)} < {schema['minLength']}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            errors.append(f"{path}: 不匹配正则 /{pattern}/")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    return errors


def _verify_json_schema(ctx: VerifyContext) -> Dict[str, Any]:
    schema = ctx.config.get("schema")
    if not isinstance(schema, dict):
        raise ScoringError("json_schema 评分缺少 config.schema")
    try:
        data = extract_json(ctx.output_text or ctx.actual_output)
    except (ValueError, json.JSONDecodeError) as e:
        return _result("failed", score=0.0, passed=False,
                       detail={"method": "json_schema", "errors": [f"JSON 解析失败: {e}"]})
    errors = _validate_schema(data, schema)
    passed = not errors
    return _result(
        "passed" if passed else "failed",
        score=10.0 if passed else 0.0, passed=passed,
        detail={"method": "json_schema", "errors": errors[:20]},
        evidence={"json_keys": sorted(data.keys())[:20] if isinstance(data, dict) else None},
    )


# ────────────────────────────── 工具使用 ──────────────────────────────


def _verify_tool_usage(ctx: VerifyContext) -> Dict[str, Any]:
    cfg = ctx.config or {}
    chain = [dict(item) for item in (ctx.tool_chain or [])]
    names = [str(item.get("tool_name") or "") for item in chain]
    succeeded = {str(item.get("tool_name") or "") for item in chain
                 if item.get("status") == "succeeded"}
    required = [str(t) for t in (cfg.get("required_tools") or []) if str(t).strip()]
    forbidden = [str(t) for t in (cfg.get("forbidden_tools") or []) if str(t).strip()]
    sequence = [str(t) for t in (cfg.get("required_sequence") or []) if str(t).strip()]

    checks: List[Dict[str, Any]] = []
    for tool in required:
        checks.append({"check": "required_tool", "tool": tool,
                       "passed": tool in succeeded})
    for tool in forbidden:
        checks.append({"check": "forbidden_tool", "tool": tool,
                       "passed": tool not in names})
    if sequence:
        pos = 0
        for name in names:
            if pos < len(sequence) and name == sequence[pos]:
                pos += 1
        checks.append({"check": "required_sequence", "sequence": sequence,
                       "matched": sequence[:pos], "passed": pos == len(sequence)})
    if not checks:
        raise ScoringError("tool_usage 验证缺少 required_tools/forbidden_tools/required_sequence")

    hit = sum(1 for c in checks if c["passed"])
    ratio = hit / len(checks)
    passed = hit == len(checks)
    return _result(
        "passed" if passed else "failed",
        score=round(ratio * 10, 2), passed=passed,
        detail={"method": "tool_usage", "checks": checks,
                "tool_sequence": names[:100]},
        evidence={"succeeded_tools": sorted(succeeded)},
    )


# ────────────────────────────── 交付产物 ──────────────────────────────


def _match_path(path: str, pattern: str) -> bool:
    p = (path or "").replace("\\", "/").lstrip("./")
    pat = (pattern or "").replace("\\", "/").lstrip("./")
    return p == pat or p.endswith("/" + pat) or fnmatch.fnmatch(p, pat)


def _verify_artifact(ctx: VerifyContext) -> Dict[str, Any]:
    cfg = ctx.config or {}
    files = [dict(f) for f in (ctx.artifacts or []) if f.get("path")]
    paths = [str(f["path"]).replace("\\", "/") for f in files]
    checks: List[Dict[str, Any]] = []

    for pattern in (cfg.get("required_files") or []):
        matched = [p for p in paths if _match_path(p, str(pattern))]
        checks.append({"check": "required_file", "pattern": str(pattern),
                       "matched": matched[:5], "passed": bool(matched)})
    for rule in (cfg.get("file_contains") or []):
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("path") or "")
        expected = str(rule.get("contains") or "")
        target = next((p for p in paths if _match_path(p, pattern)), None)
        ok = False
        if target is not None:
            if ctx.workspace_text and f"[工作区文件: {target}]" in ctx.workspace_text:
                ok = expected in ctx.workspace_text
            else:
                ok = expected in ctx.actual_output
        checks.append({"check": "file_contains", "path": pattern,
                       "contains": expected, "passed": ok})
    min_files = cfg.get("min_files")
    if min_files is not None:
        checks.append({"check": "min_files", "expected": int(min_files),
                       "actual": len(paths), "passed": len(paths) >= int(min_files)})
    forbidden = [str(p) for p in (cfg.get("forbidden_files") or [])]
    for pattern in forbidden:
        matched = [p for p in paths if _match_path(p, pattern)]
        checks.append({"check": "forbidden_file", "pattern": pattern,
                       "matched": matched[:5], "passed": not matched})
    if not checks:
        raise ScoringError("artifact 验证缺少 required_files/file_contains/min_files")

    hit = sum(1 for c in checks if c["passed"])
    ratio = hit / len(checks)
    pass_mode = str(cfg.get("pass_mode") or "all").lower()
    if pass_mode == "threshold":
        passed = ratio >= float(cfg.get("min_ratio") or 0.8)
    else:
        passed = hit == len(checks)
    return _result(
        "passed" if passed else "failed",
        score=round(ratio * 10, 2), passed=passed,
        detail={"method": "artifact", "checks": checks,
                "file_count": len(paths)},
        evidence={"files": paths[:50]},
    )


# ────────────────────────────── Mock 最终状态 / 工程验收 ──────────────────────────────


def _json_path(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (IndexError, ValueError):
                return None
        else:
            return None
    return current


def _parse_result_json(text: str) -> Any:
    try:
        return extract_json(text)
    except (ValueError, json.JSONDecodeError):
        return None


def _verify_mock_state(ctx: VerifyContext) -> Dict[str, Any]:
    """验证 Mock API / Mock 业务系统的最终状态（基于工具结果证据）。

    config: {
      "required_calls": [
        {"tool": "create_order",
         "result_contains": ["\"status\": \"created\""],
         "result_json": {"data.status": "created"}}
      ],
      "forbidden_calls": ["delete_all"],
      "pass_mode": "all" | "threshold", "min_ratio": 0.8
    }
    """
    cfg = ctx.config or {}
    chain = [dict(item) for item in (ctx.tool_chain or [])]
    checks: List[Dict[str, Any]] = []
    for rule in (cfg.get("required_calls") or []):
        if not isinstance(rule, dict):
            continue
        tool = str(rule.get("tool") or "")
        calls = [c for c in chain
                 if c.get("tool_name") == tool and c.get("status") == "succeeded"]
        if not calls:
            checks.append({"check": "required_call", "tool": tool,
                           "passed": False, "detail": "未成功调用"})
            continue
        result_text = "\n".join(str(c.get("result") or "") for c in calls)
        contains = [str(x) for x in (rule.get("result_contains") or [])]
        missing = [text for text in contains if text not in result_text]
        checks.append({"check": "result_contains", "tool": tool,
                       "expected": contains, "missing": missing,
                       "passed": not missing})
        for path, expected in (rule.get("result_json") or {}).items():
            parsed = None
            for call in calls:
                parsed = _parse_result_json(str(call.get("result") or ""))
                if parsed is not None:
                    break
            actual = _json_path(parsed, str(path)) if parsed is not None else None
            passed = actual == expected
            checks.append({"check": "result_json", "tool": tool,
                           "path": str(path), "expected": expected,
                           "actual": actual, "passed": passed})
    for tool in [str(t) for t in (cfg.get("forbidden_calls") or [])]:
        hit = [c for c in chain if c.get("tool_name") == tool]
        checks.append({"check": "forbidden_call", "tool": tool,
                       "actual": len(hit), "passed": not hit})
    min_calls = cfg.get("min_calls")
    if isinstance(min_calls, int):
        checks.append({"check": "min_calls", "expected": min_calls,
                       "actual": len(chain), "passed": len(chain) >= min_calls})
    if not checks:
        raise ScoringError("mock_state 验证缺少 required_calls/forbidden_calls/min_calls")

    hit = sum(1 for c in checks if c["passed"])
    ratio = hit / len(checks)
    pass_mode = str(cfg.get("pass_mode") or "all").lower()
    if pass_mode == "threshold":
        passed = ratio >= float(cfg.get("min_ratio") or 0.8)
    else:
        passed = hit == len(checks)
    return _result(
        "passed" if passed else "failed",
        score=round(ratio * 10, 2), passed=passed,
        detail={"method": "mock_state", "checks": checks},
        evidence={"tool_calls": [c.get("tool_name") for c in chain][:50]},
    )


def _verify_project(ctx: VerifyContext) -> Dict[str, Any]:
    """验证工程构建/测试验收工具的最终结果（Runtime `verify_workspace_project`）。

    config: {
      "tool": "verify_workspace_project",
      "require_success": true,
      "require_result_contains": ["\"passed\": true"],
      "forbid_result_contains": ["\"passed\": false", "构建失败", "测试失败"]
    }
    """
    cfg = ctx.config or {}
    tool = str(cfg.get("tool") or "verify_workspace_project")
    chain = [dict(item) for item in (ctx.tool_chain or [])]
    calls = [c for c in chain if c.get("tool_name") == tool]
    succeeded = [c for c in calls if c.get("status") == "succeeded"]
    require_success = bool(cfg.get("require_success", True))
    checks: List[Dict[str, Any]] = [{
        "check": "tool_calls", "tool": tool, "actual": len(calls),
        "passed": bool(succeeded) if require_success else bool(calls),
    }]
    result_text = "\n".join(str(c.get("result") or "") for c in succeeded)
    required = [str(x) for x in (cfg.get("require_result_contains") or [])]
    if required:
        missing = [text for text in required if text not in result_text]
        checks.append({"check": "result_contains", "expected": required,
                       "missing": missing, "passed": not missing})
    forbidden = [str(x) for x in (cfg.get("forbid_result_contains") or [])]
    if forbidden:
        hit = [text for text in forbidden if text in result_text]
        checks.append({"check": "result_forbidden", "forbidden": forbidden,
                       "hit": hit, "passed": not hit})
    hit_count = sum(1 for c in checks if c["passed"])
    passed = hit_count == len(checks)
    return _result(
        "passed" if passed else "failed",
        score=10.0 if passed else 0.0, passed=passed,
        detail={"method": "project_verification", "checks": checks,
                "tool_calls": len(calls)},
        evidence={"tool": tool, "succeeded": len(succeeded)},
    )


# ────────────────────────────── 统一入口 ──────────────────────────────


async def verify_output(ctx: VerifyContext) -> Dict[str, Any]:
    """执行确定性验证。llm_judge 不属于 Verifier，由 Judge 单独执行。"""
    method = (ctx.method or "none").strip().lower()
    try:
        if method == "none":
            return _result("unscored", detail={"method": "none"})
        if method == "llm_judge":
            return _result("unscored", detail={"method": "llm_judge",
                                               "note": "由 Judge 评估"})
        if method == "json_schema":
            return _verify_json_schema(ctx)
        if method == "tool_usage":
            return _verify_tool_usage(ctx)
        if method == "artifact":
            return _verify_artifact(ctx)
        if method == "mock_state":
            return _verify_mock_state(ctx)
        if method == "project_verification":
            return _verify_project(ctx)
        # 文本/正则/关键词/约束：复用评分器的确定性实现
        scored = await score_output(
            None,  # 确定性方法不访问数据库
            method=method,
            config=ctx.config,
            input_text=ctx.input_text,
            expected=ctx.expected,
            criteria=ctx.criteria,
            actual_output=ctx.actual_output,
            output_text=ctx.output_text,
        )
        passed = scored.get("passed")
        status = "passed" if passed is True else (
            "failed" if passed is False else "unscored")
        detail = dict(scored.get("detail") or {})
        detail.setdefault("method", method)
        evidence = {
            "output_chars": len(ctx.output_text or ctx.actual_output or ""),
            "artifact_chars": len(ctx.workspace_text or ""),
        }
        return _result(status, score=scored.get("score"), passed=passed,
                       detail=detail, evidence=evidence)
    except ScoringError as e:
        return _result("error", detail={"method": method, "error": str(e)})
    except Exception as e:  # noqa: BLE001 验证器自身故障不得当作 Agent 失败
        return _result("error", detail={"method": method,
                                        "error": f"验证器异常: {e}"[:500]})


def is_deterministic_method(method: str) -> bool:
    return (method or "").strip().lower() in VERIFIER_METHODS
