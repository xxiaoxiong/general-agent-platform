#!/usr/bin/env python3
"""联网深度研究 + 文档交付探针（真实模型，端到端）。

用途：在同一个会话里模拟真实用户完成
「联网深度调研 → 针对缺口补研 → 把调研结果写成 Word 报告 → 多轮局部重构 → 样式重排 → 交付检查」，
并做客观核对：
  - 检索事实：web_search / fetch_webpage 的实际调用与成功次数、检索词、来源 URL；
  - 引用事实：正文引用链接是否真的出现在工具返回里（未核对引用单独列出）；
  - 完成事实：模型声称的完成/版本/数量是否有对应事件或产物（虚报单独列出）；
  - 交互体验：首事件延迟、最长静默、心跳、表单拦截与工具轨迹。

与 document_lifecycle_probe.py 的区别：后者针对已有长文档做解析/编辑，
本探针从「联网研究」起步，重点验证检索、来源引用与结果交付的真实性。

容器内运行（默认连本机后端）：
  docker cp backend/tests/e2e/research_delivery_probe.py ai_plat_backend:/tmp/research_probe.py
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/research_probe.py \
      --user-id 2 --report /tmp/research_probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_TOPIC = "企业级 AI Agent 长期记忆方案（Mem0 / Letta / Zep / LangMem 等）"

TERMINAL_STATUSES = {
    "completed", "failed", "cancelled", "limit_exceeded", "needs_reconciliation", "suspended",
}
PAUSE_STATUSES = {"waiting_input", "waiting_approval"}

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def build_scenarios(topic: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "research_full": [
            {
                "key": "research_deep",
                "label": "联网深度研究",
                "message": (
                    f"请联网做一次深度调研：2025-2026 年{topic}的主流方案与选型。\n"
                    "要求：\n"
                    "1. 先列调研提纲，再逐项检索；检索后对照提纲检查缺口，缺什么补搜什么。\n"
                    "2. 覆盖各方案的架构、记忆写入与检索机制、能力边界、延迟与成本、生产落地情况、已知坑。\n"
                    "3. 关键数字、版本号、发布时间尽量用两个独立来源交叉核对，无法核对的标注「单一来源」。\n"
                    "4. 至少 8 个可核验来源：正文结论后用 [N] 编号引用，文末输出「## 来源清单」表"
                    "（编号 / 标题 / 链接 / 发布时间），链接必须来自实际检索结果，不得编造。\n"
                    "5. 最后给出完整的结构化调研结论，覆盖全部提纲项，不要只给摘要。"
                ),
            },
            {
                "key": "research_followup",
                "label": "缺口补研与事实修正",
                "message": (
                    "第 4 部分（延迟与成本）的结论太笼统。请补充近一年的公开 benchmark 数据和真实生产案例；"
                    "如果确实找不到公开数据，就直接说明找不到，不要用记忆里的数字冒充。"
                    "把更新后的完整调研结论按原有章节结构重新发给我：保留原有结论和来源清单，新增内容标注来源编号。"
                ),
            },
            {
                "key": "generate_doc",
                "label": "调研结果写成 Word 报告",
                "message": (
                    f"以上面的完整调研为基础，重写并扩展成一份《{topic}选型报告（2025-2026）》Word 文档给我："
                    "面向企业技术架构师，至少 10000 字；结构完整（执行摘要、技术全景、方案对比、选型决策、"
                    "落地路线、风险与合规、来源清单），方案对比要有表格。"
                    "最新版本、性能、价格等事实必须联网核实后再写，不要凭记忆编造。"
                ),
            },
            {
                "key": "edit_content",
                "label": "局部深度重构",
                "message": (
                    "报告第 3 章写得太浅。把它重写成有深度的方案对比：按架构设计、记忆写入与检索机制、"
                    "延迟与成本、生产成熟度、适用与不适用场景几个维度做对比表，并说明每种方案不适合的场景。"
                    "其他章节保持不变。"
                ),
            },
            {
                "key": "edit_structure",
                "label": "结构调整",
                "message": (
                    "文档里「选型决策」这一章节改成可执行的决策流程：先给出决策树式的判断步骤，"
                    "再补一张快速决策表，最后加一段常见误判提醒。其余内容不要动。"
                ),
            },
            {
                "key": "style",
                "label": "样式与排版优化",
                "message": "整体优化一下文档的样式和排版，适合正式汇报。",
            },
            {
                "key": "verify_final",
                "label": "交付前全文检查",
                "message": (
                    "最后通读全文做一次交付检查：章节编号是否连续、表格数据与正文是否一致、"
                    "来源清单与正文编号是否对应，发现问题直接修正。"
                ),
            },
        ],
    }


# 客观验收标准：路由、检索事实、引用事实、产物与交互时延
EXPECT: dict[str, dict[str, Any]] = {
    "research_deep": {
        "routes": {"general_chat"}, "verification": {"passed"},
        "min_web_successes": 4, "min_verified_citations": 8,
    },
    "research_followup": {
        "routes": {"general_chat"}, "verification": {"passed"},
        "min_web_successes": 2, "min_verified_citations": 1,
    },
    "generate_doc": {
        "routes": {"document_generate"}, "artifact": True,
        "min_chars": 9_000, "min_doc_web_successes": 1, "doc_web_source": True,
        "no_placeholder": True,
    },
    "edit_content": {
        "routes": {"document_edit"}, "artifact": True, "scope_max": 3,
        "preserve_content": True, "no_placeholder": True,
    },
    "edit_structure": {
        # 结构调整允许替换为更紧凑的决策流程，但不允许占位符或丢章节
        "routes": {"document_edit"}, "artifact": True, "scope_max": 3, "no_placeholder": True,
    },
    "style": {"routes": {"document_edit"}, "artifact": True, "style": True},
    "verify_final": {
        "routes": {"document_edit"}, "artifact": True, "scope_max": 8,
        "preserve_content": True, "no_placeholder": True,
    },
}
# 交互时延门槛：首事件 30s 内必须有反馈；单次静默不超过 5 分钟
FIRST_EVENT_LIMIT_S = 30.0
MAX_GAP_LIMIT_S = 300.0

_URL_RE = re.compile(r"https?://[^\s)）\"'<>，。；\]】`]+", re.IGNORECASE)
_WEB_TOOLS = {"web_search", "fetch_webpage", "http_get"}

# 模型自报数量的常见句式（只做客观比对，不评价文案）
_CLAIM_REFS = re.compile(
    r"(?:检索|搜索|查阅|调研|核对|核实|交叉核对|整理)(?:了|过)?\s*(\d+)\s*(?:个|条|篇|份|次)?\s*"
    r"(?:独立|可核验|公开|可靠|权威)?\s*(?:来源|信源|网页|资料|文献|案例|搜索结果)"
)
_CLAIM_REFS_ALT = re.compile(
    r"(\d+)\s*(?:个|条|篇|份)\s*(?:独立|可核验|公开|可靠|权威)?\s*(?:来源|信源|网页|文献)"
)
_CLAIM_SEARCHES = re.compile(
    r"(?:共|累计)?\s*(?:检索|搜索|抓取)\s*(?:了|过)?\s*(\d+)\s*(?:次|轮)"
)
_WEB_CLAIM = re.compile(r"已(?:联网|上网|在线)?(?:检索|搜索|搜索到|查阅)|联网(?:检索|搜索)了|检索了|搜索了")
_DOC_DELIVERY_CLAIM = re.compile(
    r"(?:文档|报告|word|docx).{0,12}(?:已生成|已保存|已交付|已导出|已写入|生成完毕)"
    r"|(?:已生成|已保存|已交付|已导出|已写入).{0,12}(?:文档|报告|word|docx)", re.I,
)
_DOC_VERSION_CLAIM = re.compile(r"第\s*(\d+)\s*版")
_DOC_CHARS_CLAIM = re.compile(r"(?:约|共|全文)?\s*(\d{3,6})\s*(?:字|个汉字|字符)")
_NO_DATA_WORDS = re.compile(r"未找到|找不到|无法核实|没有公开|缺乏公开|未检索到|无公开数据")
_PLACEHOLDER = re.compile(
    r"[（(]\s*(?:保留|保持|沿用)\s*原文(?:内容|不变)?\s*[）)]"
    r"|[（(]\s*(?:此处|这里)?\s*(?:省略|略去|略)\s*[）)]"
)
_DELIVERY_PATH = re.compile(
    r"(?<![\w/:])(/[\w.\-()/\u4e00-\u9fff]{1,180}\.(?:md|txt|docx|pdf|xlsx|xls|csv|pptx|zip))", re.I,
)


def _now() -> float:
    return time.monotonic()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _url_map(text: str) -> dict[tuple, str]:
    """URL -> 规范化键（host/path/query），用于跨来源核对。"""
    out: dict[tuple, str] = {}
    for match in _URL_RE.findall(text or ""):
        raw = match.rstrip(".,;]}`")
        try:
            parts = urlsplit(raw)
        except ValueError:
            continue
        if not parts.hostname:
            continue
        key = (parts.hostname.casefold(), parts.path.rstrip("/"), parts.query)
        out.setdefault(key, raw)
    return out


class SessionProbe:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = httpx.AsyncClient(
            base_url=args.base.rstrip("/"),
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=httpx.Timeout(connect=15, read=None, write=60, pool=60),
        )
        self.hints = dict(item.split("=", 1) for item in args.hint)
        self.observed_urls: dict[tuple, str] = {}
        self.report: dict[str, Any] = {
            "scenario": args.scenario,
            "topic": args.topic,
            "base": args.base,
            "agent_id": args.agent_id or None,
            "conversation_id": None,
            "document": None,
            "turns": [],
            "issues": [],
        }

    async def close(self) -> None:
        await self.client.aclose()

    # ── 基础 API ──────────────────────────────────────────────────────────

    async def create_assistant(self) -> int:
        payload = {
            "name": "联网深度研究验收助手",
            "description": "面向联网深度研究与文档交付验收的临时助手",
            "capability_ids": ["general", "web_research"],
            "system_prompt": (
                "你是一名严谨的技术研究员。调研必须联网检索并以可核验来源支撑结论；"
                "交付文档要结构完整、数据可追溯；无法核实的结论明确标注，不得编造。"
            ),
        }
        response = await self.client.post("/api/v2/assistants", json=payload)
        response.raise_for_status()
        data = response.json()
        self.report["agent_id"] = data.get("id")
        self.report["agent_created"] = {"id": data.get("id"), "name": data.get("name")}
        print(f"已创建验收助手: id={data.get('id')} capability={data.get('capability_ids')}", flush=True)
        return int(data["id"])

    async def create_conversation(self) -> int:
        response = await self.client.post(
            "/api/v2/conversations",
            json={"assistant_id": self.args.agent_id, "title": self.args.title},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def create_run(self, session_id: int, message: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "conversation_id": session_id,
            "approval_mode": "auto",
            "web_enabled": True,
        }
        response = await self.client.post("/api/v2/runs", json=body)
        if response.status_code == 409:
            await asyncio.sleep(2)
            response = await self.client.post("/api/v2/runs", json=body)
        response.raise_for_status()
        return response.json()

    async def run_status(self, run_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/v2/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    async def submit_input(self, run_id: str, request_id: str, values: dict) -> None:
        response = await self.client.post(
            f"/api/v2/runs/{run_id}/input",
            json={"request_id": request_id, "values": values},
        )
        response.raise_for_status()

    async def approve_pending(self, run_id: str, info: dict) -> None:
        actions = info.get("pending_actions") or []
        decisions = [
            {"type": "approve", "message": "", "action_id": action.get("action_id")}
            for action in actions
        ] or [{"type": "approve", "message": ""}]
        response = await self.client.post(f"/api/v2/runs/{run_id}/resume", json={"decisions": decisions})
        response.raise_for_status()

    async def cancel(self, run_id: str) -> None:
        try:
            await self.client.post(f"/api/v2/runs/{run_id}/cancel", json={})
        except Exception:  # noqa: BLE001 - 取消失败不应掩盖主流程结果
            pass

    async def retry_document(self, run_id: str, failed_seq: int) -> bool:
        """文档模型输出失败可由平台从断点重试；探针模拟用户点击“重试”。"""
        try:
            response = await self.client.post(
                f"/api/v2/runs/{run_id}/retry",
                headers={"X-Document-Failure-Seq": str(failed_seq)},
                json={},
            )
        except Exception:  # noqa: BLE001
            return False
        return response.status_code < 400

    @staticmethod
    def _document_retryable(timeline: list[dict]) -> bool:
        for event in reversed(timeline):
            if event["event"] == "run.failed":
                return bool((event.get("data") or {}).get("document_retryable"))
        return False

    @staticmethod
    def _failed_sequence(timeline: list[dict]) -> int:
        return max((int(event.get("sequence") or 0) for event in timeline
                    if event["event"] == "run.failed"), default=0)

    # ── 事件流 ────────────────────────────────────────────────────────────

    async def consume(self, run_id: str, after_seq: int, deadline: float) -> tuple[list[dict], bool]:
        events: list[dict] = []
        timed_out = False
        try:
            async with self.client.stream(
                "GET", f"/api/v2/runs/{run_id}/events", params={"after_seq": after_seq},
                timeout=httpx.Timeout(connect=15, read=None, write=30, pool=30),
            ) as response:
                response.raise_for_status()
                name: str | None = None
                async for line in response.aiter_lines():
                    if _now() > deadline:
                        timed_out = True
                        break
                    if line.startswith("event:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line.split(":", 1)[1].strip())
                        events.append({
                            "event": name or payload.get("event"),
                            "sequence": payload.get("sequence") or 0,
                            "data": payload.get("data") or {},
                            "at": _now(),
                        })
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            events.append({"event": "__stream_error__", "sequence": 0, "data": {"error": str(exc)}, "at": _now()})
        return events, timed_out

    # ── 表单填写（按 schema 通用推导，不绑定具体场景）─────────────────────

    def fill_values(self, schema: dict, events: list[dict]) -> dict[str, Any]:
        return {
            key: self.value_for(key, rule, events)
            for key, rule in (schema.get("properties") or {}).items()
        }

    def value_for(self, key: str, rule: dict, events: list[dict]) -> Any:
        if "const" in rule:
            return rule["const"]
        if "default" in rule:
            return rule["default"]
        kind = rule.get("type")
        if kind == "array":
            enum = (rule.get("items") or {}).get("enum") or []
            return list(enum)
        if kind == "boolean":
            return True
        if kind in ("integer", "number"):
            return rule.get("minimum", 1)
        if kind == "object":
            for event in reversed(events):
                candidate = (event.get("data") or {}).get(key)
                if isinstance(candidate, dict) and set(rule.get("properties") or {}) & set(candidate):
                    return candidate
            return self.fill_values(rule, events)
        if rule.get("enum"):
            return rule["enum"][0]
        if rule.get("format") == "date":
            return time.strftime("%Y-%m-%d")
        return self.hints.get(key) or "该项资料已在上面的调研报告中给出，请直接使用报告中的最新数据与来源。"

    # ── 单轮执行 ──────────────────────────────────────────────────────────

    async def run_turn(self, turn: dict) -> dict[str, Any]:
        started = _now()
        deadline = started + self.args.turn_timeout
        result: dict[str, Any] = {
            "key": turn["key"], "label": turn["label"], "message": turn["message"],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": None, "status": None, "duration_s": None, "error": None,
            "route": {}, "events": {}, "first_event_delay_s": None,
            "first_progress_delay_s": None, "max_gap_s": 0.0, "long_gaps": [],
            "tools": [], "pauses": [], "artifact": None, "final_text": None,
            "timed_out": False,
        }
        run = await self.create_run(self.args.session_id, turn["message"])
        run_id = run["run_id"]
        result["run_id"] = run_id
        run_created = _now()
        sequence = 0
        timeline: list[dict] = []
        status = run.get("status") or "running"
        retries = 0
        try:
            while True:
                if _now() > deadline:
                    result["timed_out"] = True
                    await self.cancel(run_id)
                    break
                events, timed_out = await self.consume(run_id, sequence, deadline)
                for event in events:
                    sequence = max(sequence, int(event.get("sequence") or 0))
                    timeline.append(event)
                info = await self.run_status(run_id)
                status = info.get("status") or status
                if status in TERMINAL_STATUSES:
                    # 文档模型输出失败可由平台从已保存章节重试；模拟用户重试后继续观察
                    if (status == "failed" and retries < self.args.document_retries
                            and self._document_retryable(timeline)
                            and await self.retry_document(run_id, self._failed_sequence(timeline))):
                        retries += 1
                        result["document_retries"] = retries
                        status = "running"
                        await asyncio.sleep(1)
                        continue
                    break
                if status == "waiting_input" and info.get("pending_input"):
                    pending = info["pending_input"]
                    schema = pending.get("input_schema") or {}
                    values = self.fill_values(schema, timeline)
                    result["pauses"].append({
                        "ordinal": pending.get("ordinal"),
                        "prompt": pending.get("prompt"),
                        "fields": list((schema.get("properties") or {}).keys()),
                        "filled": _jsonable(values),
                    })
                    await self.submit_input(run_id, pending["request_id"], values)
                    continue
                if status == "waiting_approval":
                    result["pauses"].append({
                        "ordinal": None, "prompt": "工具审批", "fields": [], "filled": "auto-approve",
                    })
                    await self.approve_pending(run_id, info)
                    continue
                if timed_out:
                    result["timed_out"] = True
                    await self.cancel(run_id)
                    break
                await asyncio.sleep(0.8)
        except Exception as exc:  # noqa: BLE001 - 探针要记录异常而不是中断整轮
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["traceback"] = traceback.format_exc()[-1500:]
            await self.cancel(run_id)
        finally:
            info = {}
            try:
                info = await self.run_status(run_id)
                status = info.get("status") or status
            except Exception:  # noqa: BLE001
                pass
            result["status"] = status
            result["duration_s"] = round(_now() - started, 1)
            result["route"] = {
                key: info.get("execution_context", {}).get(key)
                for key in ("task_type", "reason", "style_edit", "confidence")
            } if info.get("execution_context") else {}
            self.summarize_events(result, timeline, run_created)
            try:
                self.audit_turn(result, timeline)
            except Exception as exc:  # noqa: BLE001 - 审计失败不应掩盖主流程结果
                result["audit_error"] = f"audit_turn: {type(exc).__name__}: {exc}"
            if status == "waiting_input" and info.get("pending_input"):
                pending = info["pending_input"]
                result["awaiting_after_stop"] = {
                    "prompt": pending.get("prompt"),
                    "fields": list(((pending.get("input_schema") or {}).get("properties") or {}).keys()),
                }
            if result["artifact"]:
                try:
                    result["version"] = await self.version_facts(result["artifact"])
                    result["doc_audit"] = await self.audit_document(result, timeline)
                except Exception as exc:  # noqa: BLE001
                    result["audit_error"] = f"document: {type(exc).__name__}: {exc}"
            if result.get("verification") is not None:
                result["verification_passed"] = result["verification"].get("status") == "passed"
            result["verdict"] = self.verdict(turn["key"], result)
        return result

    # ── 事件与事实核对 ────────────────────────────────────────────────────

    @staticmethod
    def summarize_events(result: dict, timeline: list[dict], created_at: float) -> None:
        counts: dict[str, int] = {}
        gaps: list[float] = []
        previous = created_at
        first_progress = None
        for event in timeline:
            name = event["event"]
            counts[name] = counts.get(name, 0) + 1
            gap = event["at"] - previous
            gaps.append(gap)
            previous = event["at"]
            if name == "tool.started":
                tool = (event["data"] or {}).get("tool_name")
                if tool and tool not in result["tools"]:
                    result["tools"].append(tool)
            if first_progress is None and (
                name in {"assistant.delta", "tool.started", "subagent.started"}
                or name.startswith("document.")
            ):
                first_progress = event["at"] - created_at
        result["events"] = {
            "total": len(timeline),
            "by_type": dict(sorted(counts.items(), key=lambda item: -item[1])[:18]),
        }
        if timeline:
            result["first_event_delay_s"] = round(timeline[0]["at"] - created_at, 1)
            result["first_progress_delay_s"] = (
                round(first_progress, 1) if first_progress is not None else None
            )
            result["max_gap_s"] = round(max(gaps[1:] or [0]), 1)
            result["long_gaps"] = [
                {"after_event": timeline[index]["event"], "gap_s": round(gap, 1)}
                for index, gap in enumerate(gaps[1:], start=1) if gap >= 15
            ][:10]

    def audit_turn(self, result: dict, timeline: list[dict]) -> None:
        """从事件还原检索事实、引用事实与完成声明，做客观核对。"""
        calls: dict[str, dict[str, Any]] = {}
        web_events: list[dict[str, Any]] = []
        progress_events: list[dict] = []
        file_writes: list[str] = []
        verification = None
        for event in timeline:
            name, data = event["event"], event["data"] or {}
            if name == "tool.started":
                call_id = str(data.get("tool_call_id") or "")
                calls[call_id] = {
                    "tool": data.get("tool_name"),
                    "input": data.get("input") or {},
                    "status": "started",
                }
            elif name in ("tool.completed", "tool.failed"):
                call_id = str(data.get("tool_call_id") or "")
                record = calls.setdefault(call_id, {"tool": data.get("tool_name"), "input": {}})
                record["status"] = "succeeded" if name == "tool.completed" else "failed"
                record["output"] = str(data.get("output") or "")
                record["error"] = str(data.get("error") or "")
                if data.get("sources"):
                    record["sources"] = data["sources"]
                if record.get("tool") in _WEB_TOOLS:
                    web_events.append(record)
                if (name == "tool.completed" and record.get("tool") in {"write_file", "edit_file"}
                        and isinstance(record.get("input"), dict)):
                    path = str(record["input"].get("file_path") or record["input"].get("path") or "")
                    if path:
                        file_writes.append(path)
            elif name == "verification.completed":
                verification = data
            elif name == "document.model.progress":
                progress_events.append(data)
            elif name == "run.completed":
                result["final_text"] = data.get("final_text")
            elif name == "run.failed":
                result["final_text"] = data.get("final_text")
            elif name == "document.version.created":
                result["artifact"] = {
                    key: data.get(key) for key in
                    ("document_id", "document_version_id", "version_no", "title", "size")
                }
        result["file_writes"] = file_writes
        result["verification"] = verification
        succeeded = [item for item in web_events if item.get("status") == "succeeded"]
        failed = [item for item in web_events if item.get("status") == "failed"]
        queries = [str((item.get("input") or {}).get("query") or (item.get("input") or {}).get("url") or "")
                   for item in web_events]
        # 本会话累计的“工具实际返回过的 URL”
        for item in succeeded:
            for source in item.get("sources") or []:
                if url := str(source.get("url") or ""):
                    for key, raw in _url_map(url).items():
                        self.observed_urls.setdefault(key, raw)
            for key, raw in _url_map(item.get("output") or "").items():
                self.observed_urls.setdefault(key, raw)
        result["web"] = {
            "attempts": len(web_events),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "queries": [query for query in queries if query][:30],
            "failure_detail": [item.get("error") or item.get("output", "")[:160] for item in failed][:6],
            "heartbeats": len(progress_events),
        }
        text = str(result.get("final_text") or "")
        cited = _url_map(text)
        verified = [raw for key, raw in cited.items() if key in self.observed_urls]
        unverified = [raw for key, raw in cited.items() if key not in self.observed_urls]
        result["citations"] = {
            "total": len(cited),
            "verified": len(verified),
            "unverified": len(unverified),
            "unverified_urls": unverified[:12],
            "numbered_marks": len({int(value) for value in re.findall(r"\[(\d{1,3})\]", text)}),
            "source_rows": len(re.findall(r"(?m)^\s*(?:\|\s*)?\d{1,3}\s*[.、|]", text)),
            "has_source_section": bool(re.search(r"来源清单|参考资料|参考文献|##\s*来源", text)),
        }
        claimed_refs = {int(value) for value in _CLAIM_REFS.findall(text)}
        claimed_refs |= {int(value) for value in _CLAIM_REFS_ALT.findall(text)}
        claimed_searches = {int(value) for value in _CLAIM_SEARCHES.findall(text)}
        claims = {
            "refs": sorted(claimed_refs),
            "searches": sorted(claimed_searches),
        }
        flags: list[str] = []
        if _WEB_CLAIM.search(text) and not succeeded:
            flags.append("声称已联网检索，但本轮没有成功的网络工具调用")
        if result.get("artifact") is None and not file_writes and _DOC_DELIVERY_CLAIM.search(text):
            flags.append("声称文档已生成/交付，但本轮没有文档版本事件或已完成的文件写入")
        if claimed_refs and max(claimed_refs) > len(self.observed_urls) + 2:
            flags.append(f"自报来源数量 {max(claimed_refs)} 超过本会话已观测来源 {len(self.observed_urls)}")
        if claimed_searches and max(claimed_searches) > max(1, len(succeeded) + 2):
            flags.append(f"自报检索次数 {max(claimed_searches)} 与本轮成功检索 {len(succeeded)} 次不符")
        claimed_paths = [path for path in _DELIVERY_PATH.findall(text) if not path.startswith("//")]
        scratch_paths = [path for path in claimed_paths if not path.startswith("/workspace/")]
        if scratch_paths:
            flags.append("最终回答把会话临时区路径当作交付物（用户无法下载）：" + "、".join(scratch_paths[:6]))
        result["claimed_paths"] = claimed_paths[:12]
        result["claims"] = claims
        result["honesty_flags"] = flags
        if _NO_DATA_WORDS.search(text):
            result["honesty_note"] = "正文明确说明未找到/无法核实"

    async def version_facts(self, artifact: dict) -> dict[str, Any]:
        """读取产物版本的差异与校验报告：证明改动范围、样式继承与内容保留。"""
        document_id, version_no = artifact.get("document_id"), artifact.get("version_no")
        response = await self.client.get(f"/api/v2/documents/{document_id}/versions")
        response.raise_for_status()
        item = next((row for row in response.json().get("items", [])
                     if row.get("version_no") == version_no), None)
        if item is None:
            return {}
        diff = item.get("version_diff") or {}
        report = item.get("validation_report") or {}
        shrunk: list[str] = []
        if int(version_no or 0) > 1:
            before = await self.preview_sections(document_id, int(version_no) - 1)
            after = await self.preview_sections(document_id, int(version_no))
            shrunk = [section_id for section_id, chars in after.items()
                      if before.get(section_id, 0) >= 300 and chars < before[section_id] * 0.75]
        return {
            "sections_changed": len(diff.get("sections") or []),
            "style": bool(diff.get("style_restyle") or diff.get("inherited_style")),
            "change_summary": item.get("change_summary"),
            "errors": [issue for issue in report.get("issues") or [] if issue.get("severity") == "error"],
            "warnings": [issue for issue in report.get("issues") or [] if issue.get("severity") == "warning"],
            "shrunk_sections": shrunk,
        }

    async def preview(self, document_id: int, version_no: int) -> dict[str, Any]:
        response = await self.client.get(
            f"/api/v2/documents/{document_id}/versions/{version_no}/preview")
        if response.status_code >= 400:
            return {}
        return response.json() or {}

    async def preview_sections(self, document_id: int, version_no: int) -> dict[str, int]:
        data = await self.preview(document_id, version_no)
        sizes: dict[str, int] = {}
        for section in data.get("sections") or []:
            chars = 0
            for block in section.get("blocks") or []:
                kind = block.get("type")
                if kind == "paragraph":
                    chars += len(block.get("text") or "")
                elif kind == "list":
                    chars += sum(len(item) for item in block.get("items") or [])
                elif kind == "table":
                    chars += sum(len(cell) for row in block.get("rows") or [] for cell in row)
            sizes[section.get("id")] = chars
        return sizes

    async def audit_document(self, result: dict, timeline: list[dict]) -> dict[str, Any]:
        """产物侧事实：实际字数、来源类型、正文引用 URL 是否来自真实检索。"""
        artifact = result.get("artifact") or {}
        preview = await self.preview(int(artifact.get("document_id")), int(artifact.get("version_no")))
        body_chars = 0
        body_text: list[str] = []
        headings: list[str] = []
        for section in preview.get("sections") or []:
            headings.append(str(section.get("title") or ""))
            for block in section.get("blocks") or []:
                kind = block.get("type")
                if kind == "paragraph":
                    text = str(block.get("text") or "")
                    body_chars += len(text)
                    body_text.append(text)
                elif kind == "list":
                    items = [str(item) for item in block.get("items") or []]
                    body_chars += sum(len(item) for item in items)
                    body_text.extend(items)
                elif kind == "table":
                    rows = block.get("rows") or []
                    body_chars += sum(len(str(cell)) for row in rows for cell in row)
                    body_text.extend(str(cell) for row in rows for cell in row)
        sources = preview.get("sources") or []

        def _kind(source: dict) -> str:
            reference = source.get("reference") if isinstance(source, dict) else None
            if isinstance(reference, dict):
                return str(reference.get("kind") or "")
            return str((source or {}).get("kind") or "")

        kinds = sorted({_kind(source) for source in sources})
        web_sources = [source for source in sources if _kind(source) == "web"]
        text = "\n".join(body_text)
        cited = _url_map(text)
        unverified = [raw for key, raw in cited.items() if key not in self.observed_urls]
        placeholders = len(_PLACEHOLDER.findall(text))
        claimed_chars = [int(value) for value in _DOC_CHARS_CLAIM.findall(str(result.get("final_text") or ""))]
        claimed_versions = [int(value) for value in _DOC_VERSION_CLAIM.findall(str(result.get("final_text") or ""))]
        flags: list[str] = []
        actual_version = int(artifact.get("version_no") or 0)
        if claimed_versions and max(claimed_versions) != actual_version:
            flags.append(f"声称第 {max(claimed_versions)} 版，实际第 {actual_version} 版")
        if claimed_chars and not any(abs(value - body_chars) <= max(300, value * 0.3) for value in claimed_chars):
            flags.append(f"声称字数 {claimed_chars} 与实际正文 {body_chars} 不符")
        result["honesty_flags"] = [*(result.get("honesty_flags") or []), *flags]
        return {
            "chars": body_chars,
            "sections": len(headings),
            "headings": headings[:24],
            "sources": len(sources),
            "source_kinds": kinds,
            "web_sources": len(web_sources),
            "body_urls": len(cited),
            "unverified_body_urls": len(unverified),
            "unverified_urls": unverified[:10],
            "placeholders": placeholders,
            "claimed_chars": claimed_chars,
        }

    # ── 验收判定 ──────────────────────────────────────────────────────────

    @staticmethod
    def verdict(key: str, result: dict) -> dict[str, Any]:
        expect = EXPECT.get(key) or {}
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: Any = "") -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        check("status_completed", result.get("status") == "completed", result.get("status"))
        route = (result.get("route") or {}).get("task_type")
        if expect.get("routes"):
            check("route", route in expect["routes"], route)
        if expect.get("verification"):
            verification = result.get("verification") or {}
            check("verification", verification.get("status") in expect["verification"],
                  {"status": verification.get("status"),
                   "failed": [item.get("id") for item in verification.get("checks") or []
                              if not item.get("passed")]})
        web = result.get("web") or {}
        if expect.get("min_web_successes"):
            check("web_retrieval", int(web.get("succeeded") or 0) >= expect["min_web_successes"],
                  web.get("succeeded"))
        citations = result.get("citations") or {}
        if expect.get("min_verified_citations"):
            check("citations_verified", int(citations.get("verified") or 0) >= expect["min_verified_citations"],
                  {"verified": citations.get("verified"), "unverified": citations.get("unverified")})
        artifact = result.get("artifact") or {}
        if expect.get("artifact"):
            check("artifact", int(artifact.get("version_no") or 0) >= 1, artifact.get("version_no"))
        doc_audit = result.get("doc_audit") or {}
        if expect.get("min_chars"):
            check("doc_chars", int(doc_audit.get("chars") or 0) >= expect["min_chars"], doc_audit.get("chars"))
        if expect.get("min_doc_web_successes"):
            check("doc_web_retrieval", int(web.get("succeeded") or 0) >= expect["min_doc_web_successes"],
                  web.get("succeeded"))
        if expect.get("doc_web_source"):
            check("doc_web_source", int(doc_audit.get("web_sources") or 0) >= 1, doc_audit.get("source_kinds"))
        version = result.get("version") or {}
        if expect.get("scope_max") and version:
            check("scope", int(version.get("sections_changed") or 0) <= expect["scope_max"],
                  version.get("sections_changed"))
            check("no_validation_errors", not version.get("errors"), version.get("errors"))
        if expect.get("style") and version:
            check("style_applied", bool(version.get("style")), version.get("change_summary"))
        if expect.get("preserve_content") and version:
            check("content_preserved", not version.get("shrunk_sections"), version.get("shrunk_sections"))
        if expect.get("no_placeholder"):
            check("no_placeholder", int(doc_audit.get("placeholders") or 0) == 0,
                  doc_audit.get("placeholders"))
        check("no_honesty_flags", not result.get("honesty_flags"), result.get("honesty_flags"))
        first = result.get("first_event_delay_s")
        check("first_event_latency", first is not None and first <= FIRST_EVENT_LIMIT_S, first)
        gap = result.get("max_gap_s")
        check("max_silence", gap is not None and gap <= MAX_GAP_LIMIT_S, gap)
        check("not_timeout", not result.get("timed_out"), result.get("timed_out"))
        return {"passed": all(item["passed"] for item in checks), "checks": checks}


async def main() -> int:
    parser = argparse.ArgumentParser(description="联网深度研究 + 文档交付探针")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--agent-id", type=int, default=0, help="复用已有助手；缺省创建临时验收助手")
    parser.add_argument("--scenario", default="research_full", choices=["research_full"])
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--title", default="联网深度研究与文档交付验收")
    parser.add_argument("--turn-timeout", type=float, default=2400)
    parser.add_argument("--document-retries", type=int, default=2,
                        help="文档模型输出失败时自动重试次数（模拟用户点击重试）")
    parser.add_argument("--hint", action="append", default=[], help="表单字段固定值 key=value")
    parser.add_argument("--report", default="/tmp/research_delivery_probe.json")
    parser.add_argument("--turns", default="", help="只跑指定 key，逗号分隔")
    parser.add_argument("--conversation-id", type=int, default=0, help="复用已有会话（续跑后续轮次）")
    args = parser.parse_args()

    if not args.token:
        if not args.user_id:
            raise SystemExit("需要 --token 或 --user-id")
        from app.core.security import create_access_token

        args.token = create_access_token(args.user_id)

    probe = SessionProbe(args)
    scenarios = build_scenarios(args.topic)
    try:
        if not args.agent_id:
            args.agent_id = await probe.create_assistant()
        if args.conversation_id:
            args.session_id = args.conversation_id
        else:
            args.session_id = await probe.create_conversation()
        probe.report["conversation_id"] = args.session_id
        allowed = {item.strip() for item in args.turns.split(",") if item.strip()}
        for index, turn in enumerate(scenarios[args.scenario]):
            if allowed and turn["key"] not in allowed:
                continue
            print(f"\n=== [{index + 1}] {turn['label']} ===", flush=True)
            result = await probe.run_turn(turn)
            probe.report["turns"].append(result)
            print(json.dumps({
                "key": result["key"], "status": result["status"], "duration_s": result["duration_s"],
                "route": result["route"], "events": result["events"]["total"],
                "web": result.get("web"), "citations": result.get("citations"),
                "pauses": len(result["pauses"]), "artifact": result["artifact"],
                "verification": (result.get("verification") or {}).get("status"),
                "honesty_flags": result.get("honesty_flags"),
                "error": result["error"], "timed_out": result["timed_out"],
            }, ensure_ascii=False), flush=True)
            if result["artifact"]:
                probe.report["document"] = result["artifact"]
            if not result["verdict"]["passed"]:
                probe.report["issues"].append(
                    f"{result['key']} 未通过验收：" + "；".join(
                        f"{item['check']}={item['detail']}" for item in result["verdict"]["checks"]
                        if not item["passed"]))
                print(f"验收未通过: {result['verdict']}", flush=True)
            if result["status"] not in {"completed", "waiting_input"} and not args.turns:
                probe.report["issues"].append(
                    f"{result['key']} 未正常完成：{result['status']} {result['error'] or ''}".strip())
                print("提前终止：该轮未正常完成", flush=True)
                break
    finally:
        await probe.close()

    probe.report["passed"] = bool(probe.report["turns"]) and all(
        item["verdict"]["passed"] for item in probe.report["turns"])
    probe.report["observed_source_urls"] = sorted(probe.observed_urls.values())
    Path(args.report).write_text(json.dumps(probe.report, ensure_ascii=False, indent=2, default=str),
                                 encoding="utf-8")
    print(f"\n报告已写入 {args.report}", flush=True)
    summary = [
        {"turn": item["key"], "status": item["status"], "duration_s": item["duration_s"],
         "route": item["route"].get("task_type"), "web_ok": (item.get("web") or {}).get("succeeded"),
         "citations_ok": (item.get("citations") or {}).get("verified"),
         "pauses": len(item["pauses"]), "artifact": (item["artifact"] or {}).get("version_no"),
         "flags": item.get("honesty_flags"), "passed": item["verdict"]["passed"]}
        for item in probe.report["turns"]
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if probe.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
