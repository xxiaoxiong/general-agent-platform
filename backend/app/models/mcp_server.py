"""MCP 服务管理模型（扩展性治理：MCP 管理模块）。

MCP（Model Context Protocol）服务 = 外部工具进程/端点，接入后其工具以
``mcp__<server>__<tool>`` 命名进入 Agent 工具面。

安全边界（与 Skill 同口径，fail-closed）：
- MCP 工具是**外部未知副作用**，默认 ``require_approval=True``（HITL 审批）
  且副作用等级 ``non_idempotent``（禁止自动重放）；只读工具可由管理员
  逐服务/逐工具显式降级为 ``read_only``；
- 工具启用状态显式声明（tool_settings），未发现/未启用的工具不进工具面；
- 绑定显式声明（agent_ids），未绑定的 Agent 看不到该服务的任何工具。

新表由 init_db() 自动创建（不动已有表结构，无需 Alembic 迁移）。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base

# 传输方式
TRANSPORT_STDIO = "stdio"                  # 本地子进程（JSONL over stdin/stdout）
TRANSPORT_STREAMABLE_HTTP = "streamable_http"  # 远端 HTTP（POST JSON-RPC，SSE/JSON 响应）

# 副作用等级（与 tools/classification 口径一致）
SIDE_EFFECT_READ_ONLY = "read_only"
SIDE_EFFECT_NON_IDEMPOTENT = "non_idempotent"


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, default="")

    # 传输配置（stdio 用 command/args/env；streamable_http 用 url/headers）
    transport = Column(String(24), default=TRANSPORT_STDIO, nullable=False)
    command = Column(String(255), nullable=True)       # stdio：可执行命令（npx / uvx / python ...）
    args = Column(JSON, default=list)                  # stdio：命令参数
    env = Column(JSON, default=dict)                   # stdio：附加环境变量
    url = Column(String(500), nullable=True)           # http：端点 URL
    headers = Column(JSON, default=dict)               # http：附加请求头（如鉴权）

    enabled = Column(Boolean, default=True, nullable=False)

    # 安全策略（fail-safe 默认值）
    require_approval = Column(Boolean, default=True, nullable=False)
    side_effect_class = Column(String(32), default=SIDE_EFFECT_NON_IDEMPOTENT, nullable=False)
    # 逐工具覆盖：{"<tool>": {"enabled": bool, "requires_approval": bool, "side_effect_class": str}}
    tool_settings = Column(JSON, default=dict)

    # tools/list 发现结果缓存：[{"name", "description", "inputSchema"}]
    discovered_tools = Column(JSON, default=list)

    # Agent 级绑定：[] = 未绑定任何 Agent（不注入）；[1,2] = 仅注入这些 Agent
    agent_ids = Column(JSON, default=list)

    # 最近一次连通性测试结果
    last_test_at = Column(DateTime, nullable=True)
    last_test_error = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
