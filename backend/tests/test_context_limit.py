"""P4-04：Context Engineering——大输出受限进上下文（AG-06）。

运行：
    pytest backend/tests/test_context_limit.py -q
"""
import os

from app.services.agent_runtime.context import RuntimeContext, RunScope
from app.services.agent_runtime.tools.adapter import (
    _TOOL_CONTEXT_LIMIT,
    limit_for_context,
)


def _ctx(user_id=501, agent_id=601, session_id=701):
    return RuntimeContext(
        scope=RunScope(user_id=user_id),
        run_id="run-ctx", thread_id="agent-601-run-ctx",
        agent_id=agent_id, session_id=session_id,
        trace_id="run-ctx", model_id="fake", agent_config={},
    )


class TestLimitForContext:
    def test_small_output_passes_through(self):
        out = limit_for_context("hello", ctx=_ctx(), tool_name="t")
        assert out == "hello"

    def test_large_output_offloaded_to_workspace(self):
        from app.services.agent_runtime.backends import workspace_root

        big = "x" * (_TOOL_CONTEXT_LIMIT + 5000)
        ctx = _ctx()
        out = limit_for_context(big, ctx=ctx, tool_name="web_search")

        # 上下文只留预览 + 路径，不含整份输出
        assert len(out) < 3_000
        assert "已卸载" in out
        assert "tool-outputs/" in out
        assert big not in out

        # 完整输出落盘可读（用户/Agent/会话隔离的工作区）
        root = workspace_root(ctx.scope.user_id, ctx.agent_id, ctx.session_id)
        dumped = os.path.join(root, "tool-outputs")
        files = os.listdir(dumped)
        assert len(files) == 1
        with open(os.path.join(dumped, files[0]), encoding="utf-8") as f:
            assert f.read() == big

    def test_offload_isolated_per_session(self):
        from app.services.agent_runtime.backends import workspace_root

        big = "y" * (_TOOL_CONTEXT_LIMIT + 1)
        ctx1 = _ctx(session_id=701)
        ctx2 = _ctx(session_id=702)
        limit_for_context(big, ctx=ctx1, tool_name="t")
        limit_for_context(big, ctx=ctx2, tool_name="t")
        r1 = workspace_root(501, 601, 701)
        r2 = workspace_root(501, 601, 702)
        assert os.path.isdir(os.path.join(r1, "tool-outputs"))
        assert os.path.isdir(os.path.join(r2, "tool-outputs"))

    def test_offload_failure_falls_back_to_truncation(self, monkeypatch):
        """卸载失败不得阻断交付：降级为截断。"""
        import app.services.agent_runtime.tools.adapter as adapter_mod

        big = "z" * (_TOOL_CONTEXT_LIMIT + 100)
        monkeypatch.setattr(adapter_mod.os.path, "join",
                            lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        out = limit_for_context(big, ctx=_ctx(), tool_name="t")
        assert len(out) <= _TOOL_CONTEXT_LIMIT + 60
        assert "已截断" in out
