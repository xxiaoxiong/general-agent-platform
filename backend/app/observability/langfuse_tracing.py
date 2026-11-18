"""Langfuse 可观测接入（P1-01 / P1-04）—— Langfuse Python SDK v5。

职责：
- 统一初始化 Langfuse Client 与 LangChain ``CallbackHandler``；
- 平台 Span 包装（排队 / Claim / 审批 / 恢复 / Session 写回等非 LangChain 阶段）；
- Run ↔ Trace 关联：trace_id 由 run_id 确定性派生（``Langfuse.create_trace_id``），
  Run 在另一个 Worker / 进程执行（HITL 恢复、崩溃恢复）时可重建同一 Trace；
- 隐私（P1-04）：``mask`` 脱敏敏感字段、``sample_rate`` 采样、SDK 后台批量+有界缓冲；
  Trace 导出失败不影响 Agent 执行。

未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 时全部为 no-op。
"""
from __future__ import annotations

import contextlib
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

import httpx  # 顶层导入：函数内冷导入会同步阻塞事件循环（Windows 上数百毫秒）

from app.core.config import settings

logger = logging.getLogger("ai_plat.observability.langfuse")

# 敏感字段名（小写子串匹配）：值一律脱敏
_SENSITIVE_KEY_PARTS = (
    "api_key", "apikey", "secret", "token", "password", "passwd",
    "authorization", "cookie", "credential", "private_key",
)

_REDACTED = "***REDACTED***"

# P1-04：大文件 / 大工具输出不写入 Trace —— 超长字符串统一截断
_MAX_CAPTURE_CHARS = 2000

_client = None
_client_init_failed = False

# 项目 ID 发现缓存（key=LANGFUSE_BASE_URL，进程内一次即可）
_project_id_cache: Dict[str, str] = {}


def is_enabled() -> bool:
    """Langfuse 是否已配置（公钥 + 私钥）。"""
    return bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)


def mask_sensitive(data: Any) -> Any:
    """P1-04：递归脱敏敏感字段 + 截断超长内容（大输出不写入 Trace）。"""
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and any(p in k.lower() for p in _SENSITIVE_KEY_PARTS):
                out[k] = _REDACTED
            else:
                out[k] = mask_sensitive(v)
        return out
    if isinstance(data, (list, tuple)):
        return [mask_sensitive(v) for v in data]
    if isinstance(data, str) and len(data) > _MAX_CAPTURE_CHARS:
        return data[:_MAX_CAPTURE_CHARS] + f"...[truncated {len(data) - _MAX_CAPTURE_CHARS} chars]"
    return data


def get_client():
    """获取全局 Langfuse Client（懒加载单例）；未配置或初始化失败返回 None。"""
    global _client, _client_init_failed
    if _client is not None:
        return _client
    if _client_init_failed or not is_enabled():
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            base_url=settings.LANGFUSE_BASE_URL,
            environment=settings.APP_ENV,
            release=settings.LANGFUSE_RELEASE or settings.APP_VERSION,
            sample_rate=max(0.0, min(1.0, settings.LANGFUSE_TRACING_SAMPLE_RATE)),
            mask=mask_sensitive,
            # 后台批量 + 有界缓冲（P1-04）：导出失败只丢 Trace 不影响执行
            flush_at=50,
            flush_interval=5.0,
        )
        return _client
    except Exception as e:  # noqa: BLE001
        # 可观测性故障不得影响业务执行：降级为 no-op 并记录
        _client_init_failed = True
        logger.warning("Langfuse 初始化失败，可观测性降级为 no-op: %s", e)
        return None


def run_trace_id(run_id: str) -> str:
    """由 run_id 确定性派生 trace_id：跨 Worker / 进程 / 恢复一致关联。"""
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=f"run:{run_id}")


def make_callback_handler(run_id: str, *, trace_id: Optional[str] = None):
    """构造 LangChain CallbackHandler（绑定 Run 的确定性 trace）。

    返回 None（未配置）时调用方跳过，不影响执行。
    """
    if not is_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        tid = trace_id or run_trace_id(run_id)
        return CallbackHandler(trace_context={"trace_id": tid})
    except Exception as e:  # noqa: BLE001
        logger.warning("Langfuse CallbackHandler 构造失败，降级为 no-op: %s", e)
        return None


@contextmanager
def run_context(run_id: str, *, user_id: Any = None, session_id: Any = None,
                trace_name: str = "", metadata: Optional[Dict[str, Any]] = None):
    """Run 级 Trace 属性传播（SDK v5 ``propagate_attributes``）。

    - user_id / session_id / trace_name / metadata 写入 Trace 及其后创建的所有 Span；
    - ``as_baggage=True`` 跨线程传播（Run 在 Worker 线程执行，Graph 回调可能切线程）；
    - 须在根 Span / CallbackHandler 创建前进入本上下文；
    - 未配置或异常时退化为无操作，绝不影响执行。
    """
    if not is_enabled():
        yield None
        return
    try:
        from langfuse import propagate_attributes

        ctx_mgr = propagate_attributes(
            user_id=str(user_id) if user_id is not None else None,
            session_id=str(session_id) if session_id is not None else None,
            trace_name=trace_name or None,
            metadata=mask_sensitive(metadata or {}) or None,
            as_baggage=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("run_context(%s) 进入失败，已降级: %s", run_id, e)
        yield None
        return
    # 注意：yield 必须在 try/except 之外——with-block 的异常会从 yield 处被 throw
    # 回生成器，若此时再 yield 会被 contextlib 报 "generator didn't stop after
    # throw()" 顶替真实业务异常
    with ctx_mgr as ctx:
        yield ctx


async def enrich_run_trace(run_id: str, *, name: str = "", user_id: Any = None,
                           session_id: Any = None, task: Any = None,
                           final_text: Any = None,
                           metadata: Optional[Dict[str, Any]] = None) -> None:
    """Run 终态富化：把结果/状态写回 Trace（POST /api/public/traces upsert）。

    SDK v5 typed client 仅支持 trace 删除，upsert 走 Public API（HTTP Basic 认证）；
    任何失败只丢观测数据，不影响业务。
    """
    if not is_enabled() or not run_id:
        return
    try:
        tid = run_trace_id(run_id)
        payload: Dict[str, Any] = {"id": tid}
        if name:
            payload["name"] = name[:200]
        if user_id is not None:
            payload["userId"] = str(user_id)
        if session_id is not None:
            payload["sessionId"] = str(session_id)
        if task is not None:
            payload["input"] = mask_sensitive(task)
        # Langfuse upsert 要求 output/usage/latency/totalCost 至少一项；
        # 无最终文本时以 metadata 兜底，保证 upsert 总能落库
        payload["output"] = mask_sensitive(
            final_text if final_text is not None else (metadata or {"run_id": run_id})
        )
        if metadata:
            payload["metadata"] = mask_sensitive(metadata)
        base_url = (settings.LANGFUSE_BASE_URL or "").rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{base_url}/api/public/traces",
                json=payload,
                auth=(settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY),
            )
            resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.warning("enrich_run_trace(%s) 失败（忽略，不影响执行）: %s", run_id, e)


def langfuse_project_id() -> str:
    """Langfuse 项目 ID：优先 ``LANGFUSE_PROJECT_ID`` 配置，缺省经 Public API 发现并缓存。

    Trace 页面路由是 ``/project/{project_id}/traces/{trace_id}``，缺 projectId
    的链接在 UI 上 404，故 URL 构造必须带项目段。
    """
    pid = (settings.LANGFUSE_PROJECT_ID or "").strip()
    if pid:
        return pid
    base = (settings.LANGFUSE_BASE_URL or "").rstrip("/")
    if not base or not is_enabled():
        return ""
    if base in _project_id_cache:
        return _project_id_cache[base]
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{base}/api/public/projects",
                auth=(settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY),
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            pid = str(data[0].get("id") or "") if data else ""
    except Exception as e:  # noqa: BLE001
        logger.warning("langfuse_project_id 发现失败（链接降级为无 projectId）: %s", e)
        return ""
    _project_id_cache[base] = pid
    return pid


def build_trace_web_url(trace_id: str = "") -> Optional[str]:
    """浏览器可达的 Trace 页面链接（trace_id 为空返回 Trace 列表页）。

    路由形如 ``{base}/project/{project_id}/traces/{trace_id}``；projectId 无法
    确定时降级为无项目段路径（best-effort，至少落到 Langfuse）。
    """
    base = (settings.LANGFUSE_PUBLIC_URL or settings.LANGFUSE_BASE_URL or "").rstrip("/")
    if not base:
        return None
    tail = f"/{trace_id}" if trace_id else ""
    pid = langfuse_project_id()
    if pid:
        return f"{base}/project/{pid}/traces{tail}"
    return f"{base}/project/traces{tail}"


def trace_url(run_id: str) -> Optional[str]:
    """浏览器可达的 Trace 跳转链接；未配置返回 None。

    优先 ``LANGFUSE_PUBLIC_URL``（容器内外地址不同时浏览器可达地址）。
    """
    if not is_enabled() or not run_id:
        return None
    try:
        return build_trace_web_url(run_trace_id(run_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("trace_url(%s) 构造失败（忽略）: %s", run_id, e)
        return None


@contextmanager
def platform_span(name: str, *, run_id: str = "", trace_id: Optional[str] = None,
                  attributes: Optional[Dict[str, Any]] = None,
                  input: Any = None, output: Any = None):
    """平台级 Span（P1-03）：排队 / Claim / Lease / 审批 / 恢复 / 写回等阶段。

    - 未配置 Langfuse 或 Span 基础设施异常：退化为无操作上下文，绝不影响执行；
    - with 块内业务异常原样向上传播（Span 标记 ERROR），不被吞掉；
    - ``run_id`` 提供时绑定确定性 trace，与 Graph/Model/Tool Span 同 Trace。
    """
    client = None
    try:
        client = get_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("platform_span(%s) 获取客户端失败，已降级: %s", name, e)

    if client is None:
        yield None
        return

    tid = trace_id or (run_trace_id(run_id) if run_id else None)
    ctx_kwargs: Dict[str, Any] = {"as_type": "span", "name": name}
    if tid:
        ctx_kwargs["trace_context"] = {"trace_id": tid}

    with contextlib.ExitStack() as stack:
        try:
            span = stack.enter_context(client.start_as_current_observation(**ctx_kwargs))
        except Exception as e:  # noqa: BLE001
            logger.warning("platform_span(%s) 创建失败，已降级: %s", name, e)
            yield None
            return
        try:
            if attributes or input is not None:
                span.update(input=input, metadata=mask_sensitive(attributes or {}))
        except Exception as e:  # noqa: BLE001
            logger.warning("platform_span(%s) 属性记录失败（忽略）: %s", name, e)
        try:
            yield span
        except BaseException as e:  # noqa: BLE001 业务异常原样传播，仅标记
            with contextlib.suppress(Exception):
                span.update(output={"error": str(e)[:200]}, level="ERROR")
            raise
        if output is not None:
            try:
                span.update(output=mask_sensitive(output))
            except Exception as e:  # noqa: BLE001
                logger.warning("platform_span(%s) 输出记录失败（忽略）: %s", name, e)


def shutdown_langfuse() -> None:
    """进程退出前冲刷批量缓冲（短生命周期进程必须调用）。"""
    client = get_client()
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as e:  # noqa: BLE001
        logger.warning("Langfuse shutdown 异常（忽略）: %s", e)


def flush_traces() -> None:
    """主动冲刷批量缓冲（Run 终态后调用，保证 Trace 链路即时可见）。"""
    client = get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning("Langfuse flush 异常（忽略）: %s", e)


def reset_for_tests() -> None:
    """测试用：重置单例状态。"""
    global _client, _client_init_failed
    _client = None
    _client_init_failed = False
    _project_id_cache.clear()
