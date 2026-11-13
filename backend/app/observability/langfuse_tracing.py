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


def reset_for_tests() -> None:
    """测试用：重置单例状态。"""
    global _client, _client_init_failed
    _client = None
    _client_init_failed = False
