#!/usr/bin/env python3
"""长文档多阶段单会话探针（真实模型，端到端）。

用途：在同一个会话里模拟真实用户对一篇长文档完成
「深度解析 → 提炼可复用规范 → 按规范生成新文档 → 多轮局部重构 → 样式重排 → 交付检查」，
记录每一轮的路由决策、耗时、事件密度、工具调用、人工输入拦截与产物版本，
用于评估 Agent 的深度执行能力与交互体验。

与 tests/e2e/document_intelligence.cjs 的区别：后者用固定模型响应做 CI 验收，
本探针连接真实模型，只做观测与报告，断言放到报告解读阶段。

容器内运行（默认连本机后端）：
  docker cp backend/tests/e2e/document_lifecycle_probe.py ai_plat_backend:/tmp/probe.py
  docker cp backend/tests/e2e/fixtures/agent_swarm_report.docx ai_plat_backend:/tmp/source.docx
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/probe.py \
      --user-id 2 --agent-id 6 --source /tmp/source.docx --report /tmp/probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

TERMINAL_STATUSES = {
    "completed", "failed", "cancelled", "limit_exceeded", "needs_reconciliation", "suspended",
}
PAUSE_STATUSES = {"waiting_input", "waiting_approval"}

# 与真实用户写法一致的轮次脚本：不指定 task_mode，由系统自行理解意图
SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "full": [
        {
            "key": "analyze",
            "label": "深度解析长文档",
            "message": "先通读这份《Agent Swarm 深度调研报告》，从整体架构、章节组织、论述深度、"
                       "表格与数据使用、引用规范这几个方面做一份深度解析，指出它作为技术调研报告还有哪些明显不足。",
        },
        {
            "key": "profile",
            "label": "提炼可复用写作规范",
            "message": "基于这份报告，帮我提炼一套可复用的「技术调研报告」写作规范：章节结构、"
                       "每个章节的写作要求、质量检查规则都列清楚，以后我写同类报告直接套用。",
            "publish_profile": True,
        },
        {
            "key": "generate",
            "label": "按规范生成新文档",
            "message": "用这套规范写一份《多智能体协作框架选型指南（2025）》，面向企业的技术架构师，"
                       "讲清楚 LangGraph、CrewAI、AutoGen、MetaGPT 这几个框架怎么选、各自的能力边界在哪、"
                       "落地有哪些坑。资料不足的地方按公开常识合理阐述，不用向我索要资料。",
        },
        {
            "key": "edit_deep",
            "label": "局部深度重构",
            "message": "第 2 章写得太浅了，重写成有深度的框架对比：按编排模型、状态管理、工具生态、"
                       "可观测性、生产成熟度几个维度做对比表，并说明每个框架不适合的场景。其他章节保持不变。",
        },
        {
            "key": "edit_structure",
            "label": "结构调整",
            "message": "文档里「选型建议」那部分改成可执行的决策流程：先给出决策树式的判断步骤，"
                       "再补一张快速决策表，最后加一段常见误判提醒。",
        },
        {
            "key": "edit_consistency",
            "label": "术语统一与一致性修正",
            "message": "统一全文术语：「多智能体系统」和「Agent Swarm」现在混着用，统一成前者；"
                       "再检查第 4 章的风险描述和第 2 章的框架能力描述有没有前后矛盾，有就改掉。",
        },
        {
            "key": "style",
            "label": "样式与排版优化",
            "message": "整体优化一下文档的样式和排版，适合正式汇报。",
        },
        {
            "key": "verify",
            "label": "交付前全文检查",
            "message": "最后通读全文做一次交付检查：章节编号是否连续、表格和正文数据是否一致、"
                       "有没有明显的错别字或前后矛盾，发现问题直接修正。",
        },
    ],
}

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# 客观验收标准：路由、产物、改动范围、交互时延（阈值来自真实用户感知口径）
EXPECT: dict[str, dict[str, Any]] = {
    "analyze": {"routes": {"general_chat", "document_analyze"}},
    "profile": {"routes": {"profile_extract"}, "profile": True},
    "generate": {"routes": {"document_generate"}, "artifact": True, "min_size": 20_000},
    "edit_deep": {"routes": {"document_edit"}, "artifact": True, "scope_max": 3},
    "edit_structure": {"routes": {"document_edit"}, "artifact": True, "scope_max": 3},
    "edit_consistency": {"routes": {"document_edit"}, "artifact": True, "scope_max": 6,
                         "preserve_content": True},
    "style": {"routes": {"document_edit"}, "artifact": True, "style": True},
    "verify": {"routes": {"document_edit"}, "artifact": True, "scope_max": 8,
               "preserve_content": True},
}
# 交互时延门槛：首事件 30s 内必须有反馈；单次静默不超过 5 分钟（期间有章节/工具事件）
FIRST_EVENT_LIMIT_S = 30.0
MAX_GAP_LIMIT_S = 300.0


def _now() -> float:
    return time.monotonic()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class SessionProbe:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = httpx.AsyncClient(
            base_url=args.base.rstrip("/"),
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=httpx.Timeout(connect=15, read=None, write=60, pool=60),
        )
        self.hints = dict(item.split("=", 1) for item in args.hint)
        self.report: dict[str, Any] = {
            "scenario": args.scenario,
            "base": args.base,
            "agent_id": args.agent_id,
            "source": str(args.source),
            "conversation_id": None,
            "document": None,
            "turns": [],
            "issues": [],
        }

    async def close(self) -> None:
        await self.client.aclose()

    # ── 基础 API ──────────────────────────────────────────────────────────

    async def create_conversation(self) -> int:
        response = await self.client.post(
            "/api/v2/conversations",
            json={"assistant_id": self.args.agent_id, "title": self.args.title},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def upload(self, session_id: int) -> dict[str, Any]:
        path = Path(self.args.source)
        response = await self.client.post(
            f"/api/v2/conversations/{session_id}/files",
            files={"file": (path.name, path.read_bytes(), MIME_DOCX)},
        )
        response.raise_for_status()
        return response.json()

    async def create_run(self, session_id: int, message: str,
                         attachments: list[dict] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "conversation_id": session_id,
            "approval_mode": "auto",
            "attachments": attachments or [],
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

    async def publish_profile(self, profile_id: int) -> dict[str, Any] | None:
        detail = await self.client.get(f"/api/v2/document-profiles/{profile_id}")
        detail.raise_for_status()
        expected = int(detail.json().get("current_version") or 1)
        response = await self.client.post(
            f"/api/v2/document-profiles/{profile_id}/publish",
            json={"expected_version": expected},
        )
        if response.status_code >= 400:
            return {"profile_id": profile_id, "error": response.text[:300]}
        return {"profile_id": profile_id, "status": response.json().get("status")}

    # ── 事件流 ────────────────────────────────────────────────────────────

    async def consume(self, run_id: str, after_seq: int, deadline: float) -> tuple[list[dict], bool]:
        """读取 SSE 直到流边界或超时；返回（事件, 是否超时）。"""
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
            # 优先复用工作流已经产出的同名对象（如规范草稿 definition）
            for event in reversed(events):
                candidate = (event.get("data") or {}).get(key)
                if isinstance(candidate, dict) and set(rule.get("properties") or {}) & set(candidate):
                    return candidate
            return self.fill_values(rule, events)
        if rule.get("enum"):
            return rule["enum"][0]
        if rule.get("format") == "date":
            return time.strftime("%Y-%m-%d")
        return self.hints.get(key) or "本报告基于公开资料整理，统计口径与数据来源见正文说明。"

    # ── 单轮执行 ──────────────────────────────────────────────────────────

    async def run_turn(self, turn: dict, attachments: list[dict] | None) -> dict[str, Any]:
        started = _now()
        deadline = started + self.args.turn_timeout
        result: dict[str, Any] = {
            "key": turn["key"], "label": turn["label"], "message": turn["message"],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": None, "status": None, "duration_s": None, "error": None,
            "route": {}, "events": {}, "first_event_delay_s": None,
            "max_gap_s": 0.0, "long_gaps": [], "tools": [], "pauses": [],
            "artifact": None, "final_text": None, "profile_saved": None,
            "timed_out": False,
        }
        run = await self.create_run(self.args.session_id, turn["message"], attachments)
        run_id = run["run_id"]
        result["run_id"] = run_id
        run_created = _now()
        sequence = 0
        timeline: list[dict] = []
        status = run.get("status") or "running"
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
            for event in reversed(timeline):
                name, data = event["event"], event["data"] or {}
                if name == "document.version.created":
                    result["artifact"] = {
                        key: data.get(key) for key in ("document_id", "document_version_id", "version_no", "title", "size")
                    }
                    break
            for event in timeline:
                if event["event"] == "document.profile_saved":
                    result["profile_saved"] = event["data"]
            for event in reversed(timeline):
                if event["event"] == "run.completed":
                    result["final_text"] = (event["data"] or {}).get("final_text")
                    break
            if status == "waiting_input" and info.get("pending_input"):
                pending = info["pending_input"]
                result["awaiting_after_stop"] = {
                    "prompt": pending.get("prompt"),
                    "fields": list(((pending.get("input_schema") or {}).get("properties") or {}).keys()),
                }
            if result["artifact"]:
                result["version"] = await self.version_facts(result["artifact"])
            result["verdict"] = self.verdict(turn["key"], result)
        return result

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
            "shrunk_sections": shrunk,
        }

    async def preview_sections(self, document_id: int, version_no: int) -> dict[str, int]:
        response = await self.client.get(
            f"/api/v2/documents/{document_id}/versions/{version_no}/preview")
        if response.status_code >= 400:
            return {}
        data = response.json() or {}
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

    @staticmethod
    def verdict(key: str, result: dict) -> dict[str, Any]:
        """按轮次客观标准判定通过与否（不评价文案质量）。"""
        expect = EXPECT.get(key) or {}
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: Any = "") -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        check("status_completed", result.get("status") == "completed", result.get("status"))
        route = (result.get("route") or {}).get("task_type")
        if expect.get("routes"):
            check("route", route in expect["routes"], route)
        if expect.get("profile"):
            check("profile_saved", bool(result.get("profile_saved")), result.get("profile_saved"))
            published = (result.get("profile_published") or {}).get("status")
            check("profile_published", published == "published", published)
        artifact = result.get("artifact") or {}
        if expect.get("artifact"):
            check("artifact", int(artifact.get("version_no") or 0) >= 1, artifact.get("version_no"))
            if expect.get("min_size"):
                check("artifact_size", int(artifact.get("size") or 0) >= expect["min_size"],
                      artifact.get("size"))
        version = result.get("version") or {}
        if expect.get("scope_max") and version:
            check("scope", int(version.get("sections_changed") or 0) <= expect["scope_max"],
                  version.get("sections_changed"))
            check("no_validation_errors", not version.get("errors"), version.get("errors"))
        if expect.get("style") and version:
            check("style_applied", bool(version.get("style")), version.get("change_summary"))
        if expect.get("preserve_content") and version:
            check("content_preserved", not version.get("shrunk_sections"), version.get("shrunk_sections"))
        first = result.get("first_event_delay_s")
        check("first_event_latency", first is not None and first <= FIRST_EVENT_LIMIT_S, first)
        gap = result.get("max_gap_s")
        check("max_silence", gap is not None and gap <= MAX_GAP_LIMIT_S, gap)
        check("not_timeout", not result.get("timed_out"), result.get("timed_out"))
        return {"passed": all(item["passed"] for item in checks), "checks": checks}

    @staticmethod
    def summarize_events(result: dict, timeline: list[dict], created_at: float) -> None:
        counts: dict[str, int] = {}
        gaps: list[float] = []
        previous = created_at
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
        result["events"] = {
            "total": len(timeline),
            "by_type": dict(sorted(counts.items(), key=lambda item: -item[1])[:15]),
        }
        if timeline:
            result["first_event_delay_s"] = round(timeline[0]["at"] - created_at, 1)
            result["max_gap_s"] = round(max(gaps[1:] or [0]), 1)
            result["long_gaps"] = [
                {"after_event": timeline[index]["event"], "gap_s": round(gap, 1)}
                for index, gap in enumerate(gaps[1:], start=1) if gap >= 15
            ][:10]


async def main() -> int:
    parser = argparse.ArgumentParser(description="长文档多阶段单会话探针")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--agent-id", type=int, default=6)
    parser.add_argument("--source", required=True)
    parser.add_argument("--scenario", default="full", choices=sorted(SCENARIOS))
    parser.add_argument("--title", default="长文档全生命周期验收")
    parser.add_argument("--turn-timeout", type=float, default=2400)
    parser.add_argument("--hint", action="append", default=[], help="表单字段固定值 key=value")
    parser.add_argument("--report", default="/tmp/document_lifecycle_report.json")
    parser.add_argument("--turns", default="", help="只跑指定 key，逗号分隔")
    parser.add_argument("--conversation-id", type=int, default=0, help="复用已有会话（续跑后续轮次）")
    args = parser.parse_args()

    if not args.token:
        if not args.user_id:
            raise SystemExit("需要 --token 或 --user-id")
        from app.core.security import create_access_token

        args.token = create_access_token(args.user_id)

    probe = SessionProbe(args)
    try:
        if args.conversation_id:
            args.session_id = args.conversation_id
            upload = None
        else:
            args.session_id = await probe.create_conversation()
            upload = await probe.upload(args.session_id)
            probe.report["upload"] = upload
        probe.report["conversation_id"] = args.session_id
        allowed = {item.strip() for item in args.turns.split(",") if item.strip()}
        attachments = [{
            "path": upload["path"], "name": upload["name"], "text_path": upload.get("text_path"),
        }] if upload else None
        for index, turn in enumerate(SCENARIOS[args.scenario]):
            if allowed and turn["key"] not in allowed:
                continue
            print(f"\n=== [{index + 1}] {turn['label']} ===", flush=True)
            result = await probe.run_turn(turn, attachments if index == 0 else None)
            probe.report["turns"].append(result)
            print(json.dumps({
                "key": result["key"], "status": result["status"], "duration_s": result["duration_s"],
                "route": result["route"], "events": result["events"]["total"],
                "pauses": len(result["pauses"]), "artifact": result["artifact"],
                "error": result["error"], "timed_out": result["timed_out"],
            }, ensure_ascii=False), flush=True)
            if result["profile_saved"] and turn.get("publish_profile"):
                published = await probe.publish_profile(int(result["profile_saved"]["profile_id"]))
                result["profile_published"] = published
                result["verdict"] = probe.verdict(turn["key"], result)
                print(f"规范发布: {published}", flush=True)
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
    Path(args.report).write_text(json.dumps(probe.report, ensure_ascii=False, indent=2, default=str),
                                 encoding="utf-8")
    print(f"\n报告已写入 {args.report}", flush=True)
    summary = [
        {"turn": item["key"], "status": item["status"], "duration_s": item["duration_s"],
         "route": item["route"].get("task_type"), "pauses": len(item["pauses"]),
         "artifact": (item["artifact"] or {}).get("version_no"),
         "passed": item["verdict"]["passed"]}
        for item in probe.report["turns"]
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if probe.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
