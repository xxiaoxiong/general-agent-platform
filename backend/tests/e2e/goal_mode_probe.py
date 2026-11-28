#!/usr/bin/env python3
"""目标模式（/goal）长程任务 + 执行中补充要求（steering）端到端探针。

真实模型、真实后端、单会话单 Run：
  1. 以目标模式发起一次联网深度研究（不限制模型/工具调用与执行时长）；
  2. 执行中按时间点提交两次补充要求（一次新增交付项，一次纠正范围）；
  3. 客观核对：补充是否被受理/注入、最终交付是否落实补充、完成声明是否真实、
     交互时延（首事件/最长静默/补充受理与注入延迟）。

容器内运行：
  docker cp backend/tests/e2e/goal_mode_probe.py ai_plat_backend:/tmp/goal_probe.py
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/goal_probe.py \
      --user-id 2 --report /tmp/goal_probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_TOPIC = "2025-2026 年企业级 AI Agent 可观测与评测方案（Langfuse / LangSmith / Arize Phoenix / Braintrust）"
TERMINAL = {"completed", "failed", "cancelled", "limit_exceeded", "needs_reconciliation", "suspended"}
WEB_TOOLS = {"web_search", "fetch_webpage", "http_get"}
FIRST_EVENT_LIMIT_S = 30.0
MAX_GAP_LIMIT_S = 300.0
STEER_POST_LIMIT_S = 5.0
STEER_APPLY_LIMIT_S = 600.0

_URL_RE = re.compile(r"https?://[^\s)）\"'<>，。；\]】`]+", re.IGNORECASE)
_CLAIM_REFS = re.compile(
    r"(?:检索|搜索|查阅|调研|核对|核实|交叉核对|整理)(?:了|过)?\s*(\d+)\s*(?:个|条|篇|份|次)?\s*"
    r"(?:独立|可核验|公开|可靠|权威)?\s*(?:来源|信源|网页|资料|文献|案例|搜索结果)")
_CLAIM_REFS_ALT = re.compile(r"(\d+)\s*(?:个|条|篇|份)\s*(?:独立|可核验|公开|可靠|权威)?\s*(?:来源|信源|网页|文献)")
_CLAIM_SEARCHES = re.compile(r"(?:共|累计)?\s*(?:检索|搜索|抓取)\s*(?:了|过)?\s*(\d+)\s*(?:次|轮)")
_WEB_CLAIM = re.compile(r"已(?:联网|上网|在线)?(?:检索|搜索|搜索到|查阅)|联网(?:检索|搜索)了|检索了|搜索了")
_DELIVERY_PATH = re.compile(
    r"(?<![\w/:])(/[\w.\-()/\u4e00-\u9fff]{1,180}\.(?:md|txt|docx|pdf|xlsx|xls|csv|pptx|zip))", re.I)


def _now() -> float:
    return time.monotonic()


def _url_keys(text: str) -> dict[tuple, str]:
    out: dict[tuple, str] = {}
    for match in _URL_RE.findall(text or ""):
        raw = match.rstrip(".,;]}`")
        try:
            parts = urlsplit(raw)
        except ValueError:
            continue
        if not parts.hostname:
            continue
        out.setdefault((parts.hostname.casefold(), parts.path.rstrip("/"), parts.query), raw)
    return out


def _num(pattern: re.Pattern, text: str) -> int:
    values = [int(item) for item in pattern.findall(text or "")]
    return max(values, default=0)


class GoalProbe:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = httpx.AsyncClient(
            base_url=args.base.rstrip("/"),
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=httpx.Timeout(connect=15, read=None, write=60, pool=60),
        )
        self.nonce = uuid.uuid4().hex[:8].upper()
        self.marker_a = f"GOAL-A-{self.nonce}"
        self.marker_b = f"GOAL-B-{self.nonce}"
        self.report: dict[str, Any] = {
            "scenario": f"{args.mode}_steering",
            "mode": args.mode,
            "topic": args.topic,
            "markers": {"a": self.marker_a, "b": self.marker_b},
            "conversation_id": None,
            "run_id": None,
            "goal_mode": None,
            "steering": [],
            "events": {},
            "interaction": {},
            "web": {},
            "citations": {},
            "honesty_flags": [],
            "checks": [],
            "issues": [],
        }

    async def close(self) -> None:
        await self.client.aclose()

    # ── 基础 API ─────────────────────────────────────────────────────────

    async def create_assistant(self) -> int:
        response = await self.client.post("/api/v2/assistants", json={
            "name": "目标模式长程任务验收助手",
            "description": "目标模式与执行中补充要求的端到端验收",
            "capability_ids": ["general", "web_research"],
            "system_prompt": (
                "你是一名严谨的技术研究员。调研必须联网检索并以可核验来源支撑结论；"
                "执行过程中用户可能补充或纠正要求，必须优先落实；无法核实的结论明确标注，不得编造。"
            ),
        })
        response.raise_for_status()
        return int(response.json()["id"])

    async def create_conversation(self) -> int:
        response = await self.client.post("/api/v2/conversations", json={
            "assistant_id": self.args.agent_id, "title": self.args.title,
        })
        response.raise_for_status()
        return int(response.json()["id"])

    async def create_run(self, session_id: int, message: str) -> dict[str, Any]:
        body = {
            "message": message, "conversation_id": session_id,
            "approval_mode": "auto", "web_enabled": True,
        }
        if self.args.mode == "goal":
            body["goal_mode"] = True
        response = await self.client.post("/api/v2/runs", json=body)
        if response.status_code == 409:
            await asyncio.sleep(2)
            response = await self.client.post("/api/v2/runs", json=body)
        response.raise_for_status()
        return response.json()

    async def steer(self, run_id: str, text: str) -> dict[str, Any]:
        started = _now()
        response = await self.client.post(f"/api/v2/runs/{run_id}/steer", json={"message": text})
        return {
            "text": text,
            "status_code": response.status_code,
            "response": response.json() if response.content else {},
            "latency_s": round(_now() - started, 3),
            "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def run_status(self, run_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/v2/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    async def conversation_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        response = await self.client.get(f"/api/v2/conversations/{conversation_id}/messages")
        response.raise_for_status()
        return list(response.json().get("items") or [])

    # ── 事件流（持续消费 + 断线续传）──────────────────────────────────────

    async def consume_once(self, run_id: str, after_seq: int, deadline: float) -> tuple[list[dict], bool]:
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
                            "sequence": int(payload.get("sequence") or 0),
                            "timestamp": payload.get("timestamp"),
                            "data": payload.get("data") or {},
                            "at": _now(),
                        })
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            events.append({"event": "__stream_error__", "sequence": 0, "data": {"error": str(exc)}, "at": _now()})
        return events, timed_out

    async def watch_run(self, run_id: str, deadline: float, done: asyncio.Event) -> dict[str, Any]:
        timeline: list[dict] = []
        sequence = 0
        status = "running"
        info: dict[str, Any] = {}
        created_at = _now()
        try:
            while _now() < deadline:
                events, timed_out = await self.consume_once(run_id, sequence, deadline)
                for event in events:
                    sequence = max(sequence, int(event.get("sequence") or 0))
                    timeline.append(event)
                if timed_out:
                    break
                info = await self.run_status(run_id)
                status = str(info.get("status") or status)
                if status in TERMINAL or status in {"waiting_input", "waiting_approval"}:
                    break
                if not events:
                    await asyncio.sleep(0.3)
            else:
                status = "probe_timeout"
        finally:
            done.set()
        return {
            "status": status, "info": info, "timeline": timeline,
            "created_at": created_at, "finished_at": _now(),
        }

    # ── 验收 ─────────────────────────────────────────────────────────────

    def build_message(self) -> str:
        return (
            f"请联网做一次大型深度调研：{self.args.topic}。\n"
            "要求：\n"
            "1. 先列调研提纲，再逐项检索；检索后对照提纲检查缺口，缺什么补搜什么，直到证据充分。\n"
            "2. 覆盖各方案的定位、核心能力、部署与数据合规、成本结构、生产落地情况、已知限制。\n"
            "3. 关键数字、版本号、发布时间尽量用两个独立来源交叉核对，无法核对的标注「单一来源」。\n"
            "4. 至少 8 个可核验来源：正文结论后用 [N] 编号引用，文末输出「## 来源清单」表"
            "（编号 / 标题 / 链接 / 发布时间），链接必须来自实际检索结果，不得编造。\n"
            "5. 最后给出完整、结构化的调研结论，覆盖全部提纲项，不要只给摘要或文件路径。\n"
            "这是一次长程任务：允许自主决定检索轮次与顺序，不要因为耗时而过早收敛。"
        )

    def steer_texts(self) -> list[str]:
        return [
            (
                "【执行中补充要求】最终报告需要调整结构：在开头新增「## 关键结论」小节，"
                "给出 3-5 条要点，并在结尾给出至少 3 条可执行建议；"
                f"同时在最终回答的最后单独一行输出标记 {self.marker_a}。"
            ),
            (
                "【执行中纠正】范围调整：不要只罗列各产品功能。必须新增「## 成本与合规」小节，"
                "对成本结构（计费方式/规模成本）与合规（数据驻留/自托管/审计）做对比，"
                "并明确标注哪些结论只有单一来源。保留前面的要求，"
                f"并在最终回答的最后一行追加标记 {self.marker_b}。"
            ),
        ]

    def audit(self, watch: dict[str, Any], steers: list[dict], messages: list[dict]) -> None:
        report = self.report
        timeline = watch["timeline"]
        info = watch["info"]
        status = watch["status"]
        report["status"] = status
        report["run_goal_mode"] = info.get("goal_mode")
        report["duration_s"] = round(watch["finished_at"] - watch["created_at"], 1)
        report["timeline_size"] = len(timeline)
        report["steering"] = steers

        by_name: dict[str, list[dict]] = {}
        for event in timeline:
            by_name.setdefault(str(event.get("event")), []).append(event)
        report["events"] = {name: len(items) for name, items in sorted(by_name.items())}
        report["event_names"] = sorted(by_name)

        # 交互时延
        first = timeline[0]["at"] - watch["created_at"] if timeline else None
        gaps = [timeline[i]["at"] - timeline[i - 1]["at"] for i in range(1, len(timeline))]
        report["interaction"] = {
            "first_event_s": round(first, 2) if first is not None else None,
            "max_gap_s": round(max(gaps), 2) if gaps else 0.0,
            "steer_posts": [
                {"latency_s": item["latency_s"], "status_code": item["status_code"]}
                for item in steers
            ],
            "steer_apply_delays": [],
        }
        applied = by_name.get("run.steering.applied") or by_name.get("run.steering_applied") or []
        for item in steers:
            if item["status_code"] >= 400:
                continue
            marker = self.marker_a if item is steers[0] else self.marker_b
            match = next((event for event in applied
                          if marker in json.dumps(event.get("data") or {}, ensure_ascii=False)), None)
            if match is not None:
                # 事件 at 是消费时刻；用事件真实 timestamp 与提交时刻的差更准确
                submit_ts = item.get("submit_ts")
                if submit_ts is not None:
                    report["interaction"]["steer_apply_delays"].append(round(match["at"] - submit_ts, 2))

        # 工具事实
        completed = by_name.get("tool.completed") or []
        failed = by_name.get("tool.failed") or []
        web_ok = [event for event in completed if str((event["data"] or {}).get("tool_name")) in WEB_TOOLS]
        web_bad = [event for event in failed if str((event["data"] or {}).get("tool_name")) in WEB_TOOLS]
        tool_urls: set[tuple] = set()
        for event in completed:
            data = event["data"] or {}
            tool_urls |= set(_url_keys(str(data.get("output") or "")))
            for source in data.get("sources") or []:
                if isinstance(source, dict) and source.get("url"):
                    tool_urls |= set(_url_keys(str(source["url"])))
        report["web"] = {
            "successes": len(web_ok), "failures": len(web_bad),
            "tools": sorted({str((event["data"] or {}).get("tool_name")) for event in completed}),
        }

        # 终态与验收
        completed_events = by_name.get("run.completed") or []
        failed_events = by_name.get("run.failed") or []
        final_text = ""
        verification: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        if completed_events:
            payload = completed_events[-1]["data"] or {}
            final_text = str(payload.get("final_text") or "")
            summary = payload.get("summary") or {}
            verification = summary.get("verification") or {}
        elif failed_events:
            payload = failed_events[-1]["data"] or {}
            final_text = str(payload.get("final_text") or "")
            summary = payload.get("summary") or {}
            verification = summary.get("verification") or {}
        report["final_text_chars"] = len(final_text)
        report["verification"] = {
            "status": verification.get("status"),
            "stop_reason": verification.get("stop_reason"),
            "fix_rounds": verification.get("fix_rounds"),
            "checks": verification.get("checks") or [],
        }
        report["summary"] = {
            key: summary.get(key) for key in (
                "tool_calls", "model_calls", "model_node_runs", "steps", "tokens_generated", "elapsed_seconds",
            )
        }

        # 引用与来源核对
        cited = {int(item) for item in re.findall(r"\[(\d+)\]", final_text)}
        answer_urls = _url_keys(final_text)
        unverified_urls = sorted(
            value for key, value in answer_urls.items() if key not in tool_urls
        )
        report["citations"] = {
            "numbers": sorted(cited), "count": len(cited),
            "source_section": "来源清单" in final_text,
            "answer_urls": len(answer_urls), "unverified_urls": unverified_urls[:20],
        }

        # 补充要求落实与虚报核对
        claims_refs = max(_num(_CLAIM_REFS, final_text), _num(_CLAIM_REFS_ALT, final_text))
        claims_searches = _num(_CLAIM_SEARCHES, final_text)
        flags: list[str] = []
        if claims_refs and claims_refs > len(web_ok):
            flags.append(f"自报来源 {claims_refs} 个 > 实际成功检索 {len(web_ok)} 次")
        if claims_searches and claims_searches > len(web_ok):
            flags.append(f"自报检索 {claims_searches} 次 > 实际成功检索 {len(web_ok)} 次")
        if _WEB_CLAIM.search(final_text) and not web_ok:
            flags.append("声称联网检索但无成功检索记录")
        for path in _DELIVERY_PATH.findall(final_text):
            name = path.rsplit("/", 1)[-1]
            if not any(name in json.dumps(event.get("data") or {}, ensure_ascii=False)
                       for event in completed if str((event["data"] or {}).get("tool_name")) in {
                           "write_file", "edit_file"}):
                flags.append(f"声称交付文件 {path} 但无对应写入记录")
        report["honesty_flags"] = flags

        # 会话历史（补充要求必须落库，刷新后仍可见）
        user_texts = [str(item.get("content") or "") for item in messages if item.get("role") == "user"]
        report["session"] = {
            "messages": len(messages),
            "steering_persisted": [bool(any(text[:40] in content for content in user_texts))
                                   for text in (item["text"] for item in steers)],
        }

        # 目标模式质量门槛：可验证计划 + 交付前完成核对（不是只解除预算）
        # 事件是完整计划快照：取最新一条统计最终计划，避免累计更新重复计数
        plan_events = by_name.get("plan.updated") or []
        latest_todos = ((plan_events[-1].get("data") or {}).get("todos") or []) if plan_events else []
        plan_items = [
            todo for todo in latest_todos
            if str((todo or {}).get("content") or "").strip()
        ]
        goal_mode = self.args.mode == "goal"
        if goal_mode:
            report["goal_quality"] = {
                "plan_items": len(plan_items),
                "completed_items": sum(
                    1 for todo in plan_items if str((todo or {}).get("status")) == "completed"),
                "audit_present": "目标核对" in final_text,
            }

        # 补充要求插入位置：run.steered 事件必须出现在执行中段（前面已有工具/文本，
        # 后面还有后续执行事件），前端据此把补充块原位插入运行块流
        steered = by_name.get("run.steered") or []
        steer_seqs = [int(event.get("sequence") or 0) for event in steered]
        other_seqs = [
            int(event.get("sequence") or 0) for event in timeline
            if event.get("event") not in {"run.steered", "run.steering_applied", "run.steering.applied"}
        ]
        mid_run = bool(steer_seqs) and any(
            seq < min(steer_seqs) for seq in other_seqs
        ) and any(seq > max(steer_seqs) for seq in other_seqs)
        report["steering_position"] = {
            "sequences": steer_seqs, "inserted_mid_run": mid_run,
        }

        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: Any = None) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        if goal_mode:
            check("goal_plan_present", bool(plan_items), report["goal_quality"])
            check("goal_audit_present", "目标核对" in final_text, report["goal_quality"]["audit_present"])
            check("goal_mode_enabled", report["run_goal_mode"] is True, report["run_goal_mode"])
        check("steering_inserted_mid_run", mid_run, report["steering_position"])
        check("run_completed", status == "completed", status)
        check("verification_passed", (verification.get("status") == "passed"), verification.get("status"))
        check("no_limit_exceeded", status != "limit_exceeded", status)
        check("steer_accepted", len(steers) == 2 and all(item["status_code"] < 400 for item in steers),
              [item["status_code"] for item in steers])
        applied_names = {"run.steering.applied", "run.steering_applied"}
        check("steering_events", len(by_name.get("run.steered") or []) >= 2
              and sum(len(by_name.get(name) or []) for name in applied_names) >= 1,
              {"submitted": len(by_name.get("run.steered") or []),
               "applied": sum(len(by_name.get(name) or []) for name in applied_names)})
        check("steering_markers_in_answer", self.marker_a in final_text and self.marker_b in final_text,
              {"a": self.marker_a in final_text, "b": self.marker_b in final_text})
        check("steering_sections_in_answer", "关键结论" in final_text and "成本与合规" in final_text,
              {"key_findings": "关键结论" in final_text, "cost_compliance": "成本与合规" in final_text})
        check("web_evidence", len(web_ok) >= 6, len(web_ok))
        check("citations_present", len(cited) >= 8 and "来源清单" in final_text,
              {"count": len(cited), "source_section": "来源清单" in final_text})
        check("no_unverified_urls", not unverified_urls, unverified_urls[:5])
        check("steering_persisted", all(report["session"]["steering_persisted"]),
              report["session"]["steering_persisted"])
        check("no_honesty_flags", not flags, flags)
        check("first_event_latency", first is not None and first <= FIRST_EVENT_LIMIT_S,
              report["interaction"]["first_event_s"])
        check("max_silence", (max(gaps) if gaps else 0.0) <= MAX_GAP_LIMIT_S,
              report["interaction"]["max_gap_s"])
        check("steer_post_latency", all(item["latency_s"] <= STEER_POST_LIMIT_S for item in steers),
              report["interaction"]["steer_posts"])
        delays = report["interaction"]["steer_apply_delays"]
        check("steer_apply_latency", bool(delays) and max(delays) <= STEER_APPLY_LIMIT_S, delays)
        report["checks"] = checks
        report["passed"] = all(item["passed"] for item in checks)
        report["issues"] = [f"{item['check']}={item['detail']}" for item in checks if not item["passed"]]

    async def audit_goal_contract(self, run_id: str, checks: list[dict[str, Any]]) -> None:
        """真实验收目标契约：持久化、验收项、revision，以及完成时必需项是否真的通过。"""
        if self.args.mode != "goal":
            return
        try:
            response = await self.client.get(f"/api/v2/goals/by-run/{run_id}")
            if response.status_code != 200:
                checks.append({
                    "check": "goal_contract_persisted", "passed": False,
                    "detail": f"HTTP {response.status_code}",
                })
                return
            payload = response.json()
            criteria = [
                item for item in (payload.get("acceptance_criteria") or [])
                if isinstance(item, dict)
            ]
            required = [item for item in criteria if item.get("required", True)]
            self.report["goal_contract"] = {
                "goal_id": payload.get("goal_id"),
                "revision": payload.get("revision"),
                "status": payload.get("status"),
                "criteria": len(criteria),
                "required_passed": sum(
                    1 for item in required if item.get("status") == "passed"),
                "required_total": len(required),
                "progress": payload.get("progress"),
                "iterations": payload.get("iterations"),
            }
            checks.append({
                "check": "goal_contract_persisted",
                "passed": bool(payload.get("goal_id")) and bool(criteria),
                "detail": {"goal_id": payload.get("goal_id"), "criteria": len(criteria)},
            })
            checks.append({
                "check": "goal_contract_revision",
                "passed": int(payload.get("revision") or 0) >= 1,
                "detail": payload.get("revision"),
            })
            # 完成 ≠ 丢失验收证据：completed 时必须所有必需项 passed
            completed = payload.get("status") == "completed"
            required_ok = all(item.get("status") == "passed" for item in required)
            checks.append({
                "check": "goal_no_false_completion",
                "passed": (required_ok if completed else True),
                "detail": {
                    "status": payload.get("status"),
                    "required_passed": sum(
                        1 for item in required if item.get("status") == "passed"),
                    "required_total": len(required),
                },
            })
        except Exception as exc:  # noqa: BLE001 探针审计失败如实记录
            checks.append({
                "check": "goal_contract_persisted", "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            })

    def db_audit(self, run_id: str) -> None:
        try:
            import pymysql
            from pymysql.cursors import DictCursor

            conn = pymysql.connect(
                host=self.args.db_host, port=self.args.db_port,
                user=self.args.db_user, password=self.args.db_password,
                database=self.args.db_name, charset="utf8mb4",
                cursorclass=DictCursor, connect_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            self.report["db_audit_error"] = f"{type(exc).__name__}: {exc}"
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, agent_config_snapshot, budget_deadline_at, budget_model_calls, "
                    "budget_tool_calls, summary FROM agent_runs WHERE id=%s", (run_id,),
                )
                row = cur.fetchone() or {}
                snapshot = row.get("agent_config_snapshot") or {}
                if isinstance(snapshot, str):
                    try:
                        snapshot = json.loads(snapshot)
                    except ValueError:
                        snapshot = {}
                self.report["db"] = {
                    "status": row.get("status"),
                    "goal_mode": bool(snapshot.get("goal_mode")),
                    "budget_deadline_at": str(row.get("budget_deadline_at")),
                    "budget_model_calls": row.get("budget_model_calls"),
                    "budget_tool_calls": row.get("budget_tool_calls"),
                }
                cur.execute(
                    "SELECT message_id, content, delivered_at, delivered_via "
                    "FROM agent_run_steering WHERE run_id=%s ORDER BY id", (run_id,),
                )
                self.report["db"]["steering"] = [
                    {**item, "delivered_at": str(item.get("delivered_at"))}
                    for item in (cur.fetchall() or [])
                ]
                if self.args.mode == "goal":
                    self.report["checks"].append({
                        "check": "db_goal_snapshot",
                        "passed": bool(snapshot.get("goal_mode")),
                        "detail": snapshot.get("goal_mode"),
                    })
                    self.report["checks"].append({
                        # 目标模式保留全局安全预算：应有绝对截止时间，而不是无限制
                        "check": "db_global_deadline",
                        "passed": row.get("budget_deadline_at") is not None,
                        "detail": str(row.get("budget_deadline_at")),
                    })
                else:
                    self.report["checks"].append({
                        "check": "db_normal_mode",
                        "passed": not bool(snapshot.get("goal_mode")),
                        "detail": bool(snapshot.get("goal_mode")),
                    })
        except Exception as exc:  # noqa: BLE001
            self.report["db_audit_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="目标模式长程任务 + 执行中补充要求探针")
    parser.add_argument("--mode", choices=["goal", "normal"], default="goal",
                        help="goal=目标模式；normal=普通对话（验证补充要求为通用能力）")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--agent-id", type=int, default=0, help="复用已有助手；缺省创建临时验收助手")
    parser.add_argument("--conversation-id", type=int, default=0, help="复用已有会话")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--title", default="目标模式长程任务验收")
    parser.add_argument("--steer-at", default="75,180", help="相对 Run 创建的补充时间点（秒，逗号分隔）")
    parser.add_argument("--turn-timeout", type=float, default=2400)
    parser.add_argument("--report", default="/tmp/goal_mode_probe.json")
    parser.add_argument("--db-host", default="mysql")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="123456")
    parser.add_argument("--db-name", default="ai_plat")
    args = parser.parse_args()

    if not args.token:
        if not args.user_id:
            raise SystemExit("需要 --token 或 --user-id")
        from app.core.security import create_access_token

        args.token = create_access_token(args.user_id)

    probe = GoalProbe(args)
    try:
        if not args.agent_id:
            args.agent_id = await probe.create_assistant()
        session_id = args.conversation_id or await probe.create_conversation()
        probe.report["conversation_id"] = session_id
        print(f"会话 {session_id} · 助手 {args.agent_id} · 模式 {args.mode} · 补充标记 {probe.marker_a} / {probe.marker_b}", flush=True)

        started = _now()
        run = await probe.create_run(session_id, probe.build_message())
        run_id = run["run_id"]
        probe.report["run_id"] = run_id
        probe.report["goal_mode"] = run.get("goal_mode")
        print(f"Run {run_id} 已创建（goal_mode={run.get('goal_mode')}）", flush=True)

        deadline = started + args.turn_timeout
        done = asyncio.Event()
        watch_task = asyncio.create_task(probe.watch_run(run_id, deadline, done))
        texts = probe.steer_texts()
        delays = [float(item) for item in args.steer_at.split(",") if item.strip()]

        steers: list[dict[str, Any]] = []
        for index, text in enumerate(texts):
            delay = delays[index] if index < len(delays) else delays[-1]
            wait = started + delay - _now()
            if wait > 0 and not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            if done.is_set():
                steers.append({"text": text, "status_code": 0, "response": {}, "latency_s": 0.0,
                               "submitted_at": "", "skipped": "run_finished"})
                continue
            result = await probe.steer(run_id, text)
            result["submit_ts"] = _now()
            steers.append(result)
            print(f"补充要求 #{index + 1}: HTTP {result['status_code']} ({result['latency_s']}s)", flush=True)

        watch = await watch_task
        messages: list[dict[str, Any]] = []
        try:
            messages = await probe.conversation_messages(session_id)
        except Exception as exc:  # noqa: BLE001
            probe.report["messages_error"] = str(exc)
        probe.audit(watch, steers, messages)
        await probe.audit_goal_contract(run_id, probe.report["checks"])
        probe.db_audit(run_id)
        # 目标契约/DB 审计可能追加检查项，最终结论以追加后的完整清单为准
        probe.report["passed"] = all(
            item.get("passed") for item in probe.report["checks"]
        )
        probe.report["issues"] = [
            f"{item.get('check')}={item.get('detail')}"
            for item in probe.report["checks"] if not item.get("passed")
        ]

        Path(args.report).write_text(json.dumps(probe.report, ensure_ascii=False, indent=2, default=str),
                                     encoding="utf-8")
        print(json.dumps({
            "status": probe.report["status"],
            "duration_s": probe.report["duration_s"],
            "web": probe.report["web"],
            "citations": probe.report["citations"],
            "interaction": probe.report["interaction"],
            "verification": probe.report["verification"]["status"],
            "goal_quality": probe.report.get("goal_quality"),
            "steering_position": probe.report.get("steering_position"),
            "honesty_flags": probe.report["honesty_flags"],
            "checks": probe.report["checks"],
            "passed": probe.report["passed"],
            "report": args.report,
        }, ensure_ascii=False, indent=2), flush=True)
        return 0 if probe.report["passed"] else 1
    finally:
        await probe.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
