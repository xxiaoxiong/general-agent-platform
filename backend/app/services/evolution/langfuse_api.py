"""Langfuse Public API 读写封装（评估闭环专用）。

- 拉取：GET /api/public/traces/{id}、GET /api/public/observations?traceId=...
- 回写：POST /api/public/scores（单对象；SDK v5 create_score 的批量数组与
  Langfuse server v3 不兼容，实测 400，故统一走 REST 单对象）

所有函数 best-effort：Langfuse 未配置/不可达时静默降级（返回 None/空表），
绝不影响评估主流程（确定性指标不依赖 Langfuse）。
"""
import logging
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.core.config import settings
from app.observability.langfuse_tracing import build_trace_web_url

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _auth() -> Optional[tuple]:
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        return None
    return (settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY)


def is_configured() -> bool:
    return _auth() is not None and bool(settings.LANGFUSE_BASE_URL)


def trace_web_url(trace_id: str) -> Optional[str]:
    """浏览器可达的 Trace 页面链接。"""
    if not trace_id:
        return None
    return build_trace_web_url(trace_id)


async def fetch_trace_bundle(trace_id: str) -> Optional[Dict[str, Any]]:
    """拉取 Trace 详情 + 全部 Observations（执行链路）。

    返回 {"trace": {...} | None, "observations": [...]}；未配置或失败返回 None。
    注：Trace 摄取进 ClickHouse 有秒级延迟，刚结束的 Run 可能暂不可读。
    """
    auth = _auth()
    if not auth or not trace_id:
        return None
    base = settings.LANGFUSE_BASE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            trace_resp = await client.get(
                f"{base}/api/public/traces/{trace_id}", auth=auth)
            # limit 上限 100（更大返回 400 too_big），分页拉全全部 observations
            observations: List[Dict[str, Any]] = []
            for page in range(1, 21):  # 最多 2000 条，足够覆盖最深 span 树
                obs_resp = await client.get(
                    f"{base}/api/public/observations",
                    params={"traceId": trace_id, "limit": 100, "page": page},
                    auth=auth)
                if obs_resp.status_code != 200:
                    logger.warning(
                        "fetch_trace_bundle(%s) observations 查询失败: %s %s",
                        trace_id, obs_resp.status_code, obs_resp.text[:200])
                    break
                batch = obs_resp.json().get("data") or []
                observations.extend(batch)
                if len(batch) < 100:
                    break
        trace = trace_resp.json() if trace_resp.status_code == 200 else None
        if trace is None and not observations:
            return None
        return {"trace": trace, "observations": observations}
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_trace_bundle(%s) 失败（降级为无 Trace 数据）: %s", trace_id, e)
        return None


async def write_scores(trace_id: str, scores: Sequence[Dict[str, Any]]) -> None:
    """把评估分回写 Langfuse（Trace 详情页可见评分）。

    scores: [{"name": str, "value": float, "comment": str?}, ...]；
    value 为 0~10 数值（NUMERIC）。失败仅告警。
    """
    auth = _auth()
    if not auth or not trace_id or not scores:
        return
    base = settings.LANGFUSE_BASE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for item in scores:
                payload = {
                    "name": str(item.get("name"))[:63],
                    "value": float(item.get("value", 0.0)),
                    "traceId": trace_id,
                    "dataType": "NUMERIC",
                }
                if item.get("comment"):
                    payload["comment"] = str(item["comment"])[:2000]
                resp = await client.post(
                    f"{base}/api/public/scores", json=payload, auth=auth)
                if resp.status_code >= 400:
                    logger.warning(
                        "write_scores(%s) %s 写入失败: %s %s",
                        trace_id, payload["name"], resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("write_scores(%s) 失败（忽略）: %s", trace_id, e)
