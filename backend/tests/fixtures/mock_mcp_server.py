"""最小 MCP stdio 服务（测试夹具）：JSONL JSON-RPC 2.0 over stdin/stdout。

工具面：
- echo(message)     回显文本
- add(a, b)         数字求和（验证 typed 参数）
- fail_tool()       远端业务错误（result.isError=true，非协议错误）
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "回显文本",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "要回显的文本"}},
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "两数求和",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "加数 a"},
                "b": {"type": "number", "description": "加数 b"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail_tool",
        "description": "返回业务错误",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _reply(req_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_call(params):
    name = (params or {}).get("name")
    args = (params or {}).get("arguments") or {}
    if name == "echo":
        return {"content": [{"type": "text", "text": f"echo: {args.get('message', '')}"}]}
    if name == "add":
        total = float(args.get("a", 0)) + float(args.get("b", 0))
        return {"content": [{"type": "text", "text": f"sum: {total}"}]}
    if name == "fail_tool":
        return {"content": [{"type": "text", "text": "业务失败：模拟远端错误"}], "isError": True}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        req_id = msg.get("id")
        if req_id is None:
            continue  # 通知（notifications/initialized 等）无需响应
        if method == "initialize":
            _reply(req_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp-server", "version": "1.0.0"},
            })
        elif method == "tools/list":
            _reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            result = _handle_call(msg.get("params"))
            if result is None:
                _reply(req_id, error={"code": -32602, "message": "未知工具"})
            else:
                _reply(req_id, result)
        else:
            _reply(req_id, error={"code": -32601, "message": f"未知方法: {method}"})


if __name__ == "__main__":
    main()
