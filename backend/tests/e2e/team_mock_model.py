"""Agent Team Runtime 确定性 E2E 模型端点（OpenAI 兼容 /v1/chat/completions）。

用途：在真实 Docker 部署（MySQL + Postgres Checkpointer + RunWorker + SSE）中
验证 Leader/Member 规划、Task Board 依赖调度、成员执行、综合交付与事件链路。
不依赖外部模型服务，结果可重复；不是 LLM 质量评测。

启动：
    python backend/tests/e2e/team_mock_model.py --port 9999
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _planning_tool_calls() -> list[dict]:
    return [
        _tool_call("call-research", "team_task_create", {
            "task_id": "research",
            "role": "资料研究员",
            "title": "核对 Python 版本差异",
            "brief": "收集 Python 3.11 与 3.12 的性能改进证据，给出简明事实清单。",
            "depends_on": [],
            "priority": 2,
        }),
        _tool_call("call-summary", "team_task_create", {
            "task_id": "summary",
            "role": "综合整理",
            "title": "整理升级判断",
            "brief": "基于资料研究员的成果，整理企业升级风险与判断依据。",
            "depends_on": ["research"],
            "priority": 1,
        }),
    ]


def _message(content: str | None, tool_calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
    return message


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[mock-model] {self.address_string()} {fmt % args}", flush=True)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            request = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            request = {}
        messages = request.get("messages") or []
        system = "\n".join(
            str(item.get("content") or "")
            for item in messages if item.get("role") == "system"
        )
        user_messages = [
            str(item.get("content") or "")
            for item in messages if item.get("role") == "user"
        ]
        # 只按最后一条用户消息判断阶段：Leader Thread 会保留规划历史，
        # 若扫描全部历史，综合阶段会误命中【planning】。
        last_user = user_messages[-1] if user_messages else ""
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        leader = "团队负责人协议" in system
        print(
            f"[mock-model] leader={leader} planning={'【planning】' in last_user} "
            f"synthesis={'【synthesis】' in last_user} tool_result={has_tool_result} "
            f"users={len(user_messages)}",
            flush=True,
        )
        if leader and "【planning】" in last_user:
            if has_tool_result:
                message = _message(
                    '规划完成。\n{"acceptance":["性能差异有事实依据","升级风险与判断依据完整"]}'
                )
            else:
                message = _message(None, _planning_tool_calls())
        elif leader and "【review】" in last_user:
            message = _message(json.dumps({"checks": [
                {"criterion": 1, "status": "supported", "evidence": "资料清单已交付", "task_id": "research"},
                {"criterion": 2, "status": "supported", "evidence": "风险与判断依据已整理", "task_id": "summary"},
            ]}, ensure_ascii=False))
        elif leader and "【synthesis】" in last_user:
            message = _message(
                "团队结论：Python 3.12 在解释器启动、函数调用与部分标准库实现上有性能改进，"
                "但企业升级仍需先核对依赖兼容性、C 扩展与生产观测数据。建议在预发环境完成"
                "依赖扫描与基准测试后再灰度升级；若关键依赖尚未支持 3.12，应暂缓升级。"
            )
        else:
            role = "团队成员"
            if "资料研究员" in system:
                role = "资料研究员"
            elif "综合整理" in system:
                role = "综合整理"
            message = _message(
                f"{role}交付：已依据任务要求完成核对与归纳，结论与证据缺口已如实说明。"
            )
        payload = {
            "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.get("model") or "mock-team",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[mock-model] listening on 0.0.0.0:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
