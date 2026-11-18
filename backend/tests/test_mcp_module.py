"""MCP 扩展模块测试：协议客户端 / 注册装配 / 执行路由 / 管理 API。

运行：
    pytest backend/tests/test_mcp_module.py -q
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, select

FIXTURE = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def _cfg(**over):
    from app.services.mcp.client import McpConnectionConfig

    base = dict(transport="stdio", command=sys.executable, args=[str(FIXTURE)], timeout=20.0)
    base.update(over)
    return McpConnectionConfig(**base)


class TestMcpClient:
    """最小 MCP 客户端对真实 stdio 子进程的协议行为（initialize/tools/list/tools/call）。"""

    @pytest.mark.asyncio
    async def test_list_tools_discovers_schema(self):
        from app.services.mcp.client import list_tools

        tools = await list_tools(_cfg())
        names = {t["name"] for t in tools}
        assert names == {"echo", "add", "fail_tool"}
        echo = next(t for t in tools if t["name"] == "echo")
        assert echo["inputSchema"]["properties"]["message"]["type"] == "string"
        assert echo["inputSchema"]["required"] == ["message"]

    @pytest.mark.asyncio
    async def test_call_tool_roundtrip(self):
        from app.services.mcp.client import call_tool

        result = await call_tool(_cfg(), "echo", {"message": "你好"})
        assert result.is_error is False
        assert result.text == "echo: 你好"

        result2 = await call_tool(_cfg(), "add", {"a": 6, "b": 7})
        assert result2.text == "sum: 13.0"

    @pytest.mark.asyncio
    async def test_call_tool_remote_business_error(self):
        from app.services.mcp.client import call_tool

        result = await call_tool(_cfg(), "fail_tool", {})
        assert result.is_error is True
        assert "业务失败" in result.text

    @pytest.mark.asyncio
    async def test_call_unknown_tool_raises_protocol_error(self):
        from app.services.mcp.client import McpClientError, call_tool

        with pytest.raises(McpClientError):
            await call_tool(_cfg(), "no_such_tool", {})

    @pytest.mark.asyncio
    async def test_spawn_failure_reports_objective_error(self):
        from app.services.mcp.client import test_connection

        result = await test_connection(_cfg(command="definitely-not-a-command-xyz"))
        assert result["ok"] is False
        assert result["error"]

    @pytest.mark.asyncio
    async def test_missing_command_rejected(self):
        from app.services.mcp.client import McpClientError, list_tools

        with pytest.raises(McpClientError):
            await list_tools(_cfg(command=""))

    @pytest.mark.asyncio
    async def test_stdio_works_on_selector_event_loop(self):
        """Windows Selector 循环（persistence 全局切换的场景）下 stdio 仍可用。

        子进程 I/O 必须走 subprocess + 线程池而非 asyncio 子进程 API。
        """
        import asyncio as _asyncio

        from app.services.mcp.client import call_tool

        if sys.platform != "win32":
            pytest.skip("仅 Windows 区分 Selector/Proactor 事件循环")

        def _run_on_selector_loop():
            loop = _asyncio.SelectorEventLoop()
            _asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(call_tool(_cfg(), "echo", {"message": "sel"}))
            finally:
                loop.close()

        result = await _asyncio.to_thread(_run_on_selector_loop)
        assert result.text == "echo: sel"


class TestMcpRegistry:
    """工具命名空间 / 规格装配 / 绑定过滤 / 风险登记（fail-closed）。"""

    def test_parse_tool_name(self):
        from app.services.mcp.registry import mcp_tool_name, parse_mcp_tool_name

        assert mcp_tool_name("srv", "echo") == "mcp__srv__echo"
        assert parse_mcp_tool_name("mcp__srv__echo") == ("srv", "echo")
        assert parse_mcp_tool_name("calculator") is None

    def test_build_tool_specs_converts_schema_and_honors_disabled(self):
        from app.services.mcp.registry import build_tool_specs

        tools = [
            {"name": "echo", "description": "回显", "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "文本"}},
                "required": ["message"],
            }},
            {"name": "hidden", "description": "被禁用", "inputSchema": {"type": "object"}},
        ]
        specs = build_tool_specs("srv", tools, tool_settings={"hidden": {"enabled": False}})
        assert [s["name"] for s in specs] == ["mcp__srv__echo"]
        s = specs[0]
        assert s["required"] == ["message"]
        assert s["parameters"]["message"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_load_mcp_runtime_binding_and_disabled(self):
        from app.core.database import AsyncSessionLocal, init_db
        from app.models.mcp_server import MCPServer
        from app.services.mcp.registry import load_mcp_runtime

        await init_db()
        async with AsyncSessionLocal() as db:
            bound = MCPServer(
                name="t-bind", transport="stdio", command="x",
                discovered_tools=[{"name": "echo", "description": "d", "inputSchema": {}}],
                tool_settings={"echo": {"enabled": False}},
                agent_ids=[1],
                require_approval=True,
            )
            unbound = MCPServer(
                name="t-unbound", transport="stdio", command="x",
                discovered_tools=[{"name": "foo", "description": "d", "inputSchema": {}}],
                agent_ids=[999],
            )
            db.add_all([bound, unbound])
            await db.commit()
        try:
            async with AsyncSessionLocal() as db:
                specs, runtime = await load_mcp_runtime(db, 1, {})
                # t-bind 绑定 agent 1 但唯一工具被禁用 -> 不注入；t-unbound 未绑定 -> 不注入
                assert specs == [] and runtime == {}

                bound2 = (await db.execute(
                    select(MCPServer).where(MCPServer.name == "t-bind")
                )).scalar_one()
                bound2.discovered_tools = [
                    {"name": "echo", "description": "d", "inputSchema": {"type": "object"}},
                ]
                bound2.tool_settings = {}
                await db.commit()

                specs, runtime = await load_mcp_runtime(db, 1, {})
                assert [s["name"] for s in specs] == ["mcp__t-bind__echo"]
                assert "t-bind" in runtime and "t-unbound" not in runtime
                meta = runtime["t-bind"]["tools"]["mcp__t-bind__echo"]
                assert meta["requires_approval"] is True
                assert meta["side_effect_class"] == "non_idempotent"

                # fail-closed：agent 2 未绑定任何服务
                specs2, runtime2 = await load_mcp_runtime(db, 2, {})
                assert specs2 == [] and runtime2 == {}

                # Agent 配置逐工具禁用
                specs3, _ = await load_mcp_runtime(
                    db, 1, {"tools": [{"name": "mcp__t-bind__echo", "enabled": False}]}
                )
                assert specs3 == []
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(MCPServer).where(MCPServer.name.in_(["t-bind", "t-unbound"])))
                await db.commit()

    @pytest.mark.asyncio
    async def test_register_mcp_tool_risks_read_only_downgrade(self):
        from app.services.agent_runtime.tools.policy import get_tool_risk
        from app.services.mcp.registry import register_mcp_tool_risks

        register_mcp_tool_risks({
            "srv": {"tools": {
                "mcp__srv__search": {"requires_approval": False, "side_effect_class": "read_only"},
                "mcp__srv__send": {"requires_approval": True, "side_effect_class": "non_idempotent"},
            }}
        })
        ro = get_tool_risk("mcp__srv__search")
        assert ro is not None and ro.side_effect_class == "read_only"
        assert ro.requires_approval is False and ro.retry_policy == "safe_retry"
        nw = get_tool_risk("mcp__srv__send")
        assert nw is not None and nw.requires_approval is True and nw.retry_policy == "none"
        assert get_tool_risk("mcp__srv__ghost") is None  # 未登记默认拒绝


class TestMcpToolExecution:
    """ToolExecutor MCP 路由：绑定执行 / fail-closed 拒绝。"""

    @pytest.mark.asyncio
    async def test_executor_calls_mcp_tool(self):
        from app.services.tools import ToolExecutor

        config = {"mcp_runtime": {"mock": {
            "connection": {"transport": "stdio", "command": sys.executable,
                           "args": [str(FIXTURE)], "env": {}, "url": "", "headers": {}},
            "tools": {},
        }}}
        executor = ToolExecutor(None, config)
        result = await executor.execute("mcp__mock__echo", {"message": "hi"})
        assert result.status == "succeeded"
        assert "echo: hi" in result.text

    @pytest.mark.asyncio
    async def test_executor_refuses_unbound_mcp_tool(self):
        from app.services.tools import ToolExecutor

        executor = ToolExecutor(None, {})
        result = await executor.execute("mcp__ghost__echo", {"message": "x"})
        assert result.status == "failed"
        assert "未绑定" in result.text


class TestMcpManagementApi:
    """管理 API：CRUD + 真实连通性测试（mock stdio 服务）。"""

    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as c:
            yield c

    @pytest.fixture(scope="class")
    def users(self):
        import os

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.models.user import User

        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            admin = User(username="mcp_admin", email="mcp_admin@test.dev",
                         hashed_password="x", is_active=True, is_superuser=True)
            normal = User(username="mcp_user", email="mcp_user@test.dev",
                          hashed_password="x", is_active=True)
            s.add_all([admin, normal])
            s.commit()
            return {"admin": admin.id, "user": normal.id}

    @staticmethod
    def _auth(user_id: int) -> dict:
        from app.core.security import create_access_token

        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def test_crud_and_discovery_roundtrip(self, client, users):
        admin_h = self._auth(users["admin"])
        user_h = self._auth(users["user"])

        # 普通用户无写权限
        resp = client.post("/api/v2/mcp/servers", headers=user_h, json={"name": "x"})
        assert resp.status_code == 403

        # 创建（stdio 指向 mock 服务）
        resp = client.post("/api/v2/mcp/servers", headers=admin_h, json={
            "name": "mock-mcp",
            "description": "测试 MCP 服务",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE)],
            "require_approval": False,
            "side_effect_class": "read_only",
            "agent_ids": [1],
        })
        assert resp.status_code == 200, resp.text
        server = resp.json()
        sid = server["id"]
        assert server["require_approval"] is False

        # 校验：stdio 缺 command 拒绝
        bad = client.post("/api/v2/mcp/servers", headers=admin_h,
                          json={"name": "bad-srv", "transport": "stdio"})
        assert bad.status_code == 400

        # 连通性测试：真实 initialize + tools/list，发现结果落库
        resp = client.post(f"/api/v2/mcp/servers/{sid}/test", headers=admin_h)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True, f"/test 返回: {data}"
        assert {t["name"] for t in data["tools"]} == {"echo", "add", "fail_tool"}
        assert data["saved_tools"] == 3

        # 发现结果可见
        resp = client.get(f"/api/v2/mcp/servers/{sid}", headers=user_h)
        assert [t["name"] for t in resp.json()["discovered_tools"]].count("echo") == 1

        # 逐工具禁用 + 修改
        resp = client.put(f"/api/v2/mcp/servers/{sid}", headers=admin_h, json={
            "tool_settings": {"fail_tool": {"enabled": False}},
        })
        assert resp.status_code == 200
        assert resp.json()["tool_settings"]["fail_tool"]["enabled"] is False

        # 删除
        resp = client.delete(f"/api/v2/mcp/servers/{sid}", headers=admin_h)
        assert resp.status_code == 200
        assert client.get(f"/api/v2/mcp/servers/{sid}", headers=user_h).status_code == 404

    def test_test_endpoint_records_failure_objectively(self, client, users):
        admin_h = self._auth(users["admin"])
        resp = client.post("/api/v2/mcp/servers", headers=admin_h, json={
            "name": "broken-mcp", "transport": "stdio",
            "command": "definitely-not-a-command-xyz",
        })
        sid = resp.json()["id"]
        try:
            data = client.post(f"/api/v2/mcp/servers/{sid}/test", headers=admin_h).json()
            assert data["ok"] is False and data["error"], f"/test 返回: {data}"
            row = client.get(f"/api/v2/mcp/servers/{sid}", headers=admin_h).json()
            assert row["last_test_error"] and row["last_test_at"]
        finally:
            client.delete(f"/api/v2/mcp/servers/{sid}", headers=admin_h)
