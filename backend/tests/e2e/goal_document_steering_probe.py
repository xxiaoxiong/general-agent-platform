#!/usr/bin/env python3
"""目标模式文档任务 + 执行中补充要求探针（真实模型，端到端）。

在已有会话中发起一次目标模式 Word 文档生成，执行中提交一条补充要求，
客观核对：补充受理/注入（文档图路径）、最终 DOCX 版本是否落实、是否虚报完成、
表单暂停与长模型调用心跳是否正常。

容器内运行：
  docker cp backend/tests/e2e/goal_document_steering_probe.py ai_plat_backend:/tmp/goal_doc_probe.py
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/goal_doc_probe.py \
      --user-id 2 --conversation-id 174 --report /tmp/goal_doc_probe.json
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

import httpx

TERMINAL = {"completed", "failed", "cancelled", "limit_exceeded", "needs_reconciliation", "suspended"}
PLACEHOLDER = re.compile(
    r"[（(]\s*(?:保留|保持|沿用)\s*原文(?:内容|不变)?\s*[）)]"
    r"|[（(]\s*(?:此处|这里)?\s*(?:省略|略去|略)\s*[）)]")
FIRST_EVENT_LIMIT_S = 30.0
MAX_GAP_LIMIT_S = 600.0
STEER_POST_LIMIT_S = 5.0


def _now() -> float:
    return time.monotonic()


class DocProbe:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = httpx.AsyncClient(
            base_url=args.base.rstrip("/"),
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=httpx.Timeout(connect=15, read=None, write=60, pool=60),
        )
        self.nonce = uuid.uuid4().hex[:8].upper()
        self.marker = f"GOAL-DOC-{self.nonce}"
        self.report: dict[str, Any] = {
            "scenario": "goal_document_steering",
            "marker": self.marker,
            "conversation_id": args.conversation_id or None,
            "steering": [],
            "events": {},
            "checks": [],
            "honesty_flags": [],
        }

    async def close(self) -> None:
        await self.client.aclose()

    async def create_conversation(self) -> int:
        response = await self.client.post("/api/v2/conversations", json={
            "assistant_id": self.args.agent_id, "title": self.args.title,
        })
        response.raise_for_status()
        return int(response.json()["id"])

    async def create_run(self, session_id: int, message: str) -> dict[str, Any]:
        body = {
            "message": message, "conversation_id": session_id,
            "approval_mode": "auto", "web_enabled": False, "goal_mode": True,
        }
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
            "text": text, "status_code": response.status_code,
            "response": response.json() if response.content else {},
            "latency_s": round(_now() - started, 3),
            "submit_ts": _now(),
        }

    async def status(self, run_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/api/v2/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    async def submit_input(self, run_id: str, request: dict, timeline: list[dict]) -> dict[str, Any]:
        schema = request.get("input_schema") or {}
        values = {}
        for key, rule in (schema.get("properties") or {}).items():
            kind = rule.get("type")
            if kind == "array":
                values[key] = (rule.get("items") or {}).get("enum") or []
            elif kind == "number":
                values[key] = rule.get("minimum", 1)
            elif kind == "boolean":
                values[key] = True
            elif rule.get("format") == "date":
                values[key] = time.strftime("%Y-%m-%d")
            elif rule.get("enum"):
                values[key] = rule["enum"][0]
            else:
                values[key] = "无额外要求，请采用通用假设并在文中注明假设。"
        response = await self.client.post(f"/api/v2/runs/{run_id}/input", json={
            "request_id": request.get("request_id"), "values": values,
        })
        return {"status_code": response.status_code, "fields": list(values), "values": values}

    async def approve(self, run_id: str, actions: list[dict]) -> int:
        decisions = [
            {"type": "approve", "message": "", "action_id": action.get("action_id")}
            for action in actions
        ] or [{"type": "approve", "message": ""}]
        response = await self.client.post(f"/api/v2/runs/{run_id}/resume", json={"decisions": decisions})
        return response.status_code

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
                            "data": payload.get("data") or {},
                            "at": _now(),
                        })
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            events.append({"event": "__stream_error__", "sequence": 0, "data": {"error": str(exc)}, "at": _now()})
        return events, timed_out

    async def watch(self, run_id: str, deadline: float, steer_task: asyncio.Task) -> dict[str, Any]:
        timeline: list[dict] = []
        sequence = 0
        status = "running"
        info: dict[str, Any] = {}
        created = _now()
        pauses: list[dict] = []
        while _now() < deadline:
            events, timed_out = await self.consume_once(run_id, sequence, deadline)
            for event in events:
                sequence = max(sequence, int(event.get("sequence") or 0))
                timeline.append(event)
            if timed_out:
                status = "probe_timeout"
                break
            info = await self.status(run_id)
            status = str(info.get("status") or status)
            if status in TERMINAL:
                break
            if status == "waiting_input" and info.get("pending_input"):
                pauses.append(await self.submit_input(run_id, info["pending_input"], timeline))
                await asyncio.sleep(0.5)
                continue
            if status == "waiting_approval":
                pauses.append({"approval_status": await self.approve(run_id, info.get("pending_actions") or [])})
                await asyncio.sleep(0.5)
                continue
            if not events:
                await asyncio.sleep(0.3)
        if not steer_task.done():
            steer_task.cancel()
        return {
            "status": status, "info": info, "timeline": timeline, "pauses": pauses,
            "created": created, "finished": _now(),
        }

    def build_message(self) -> str:
        return (
            "写一份《企业级 AI Agent 可观测与评测建设指南》Word 文档给我："
            "面向研发团队，4 章左右，每章至少 500 字，包含一张方案对比表和使用建议。"
            "不需要联网，也不需要再向我确认额外信息；缺少的业务信息采用通用假设并在文中注明。"
            "完成后直接交付可下载的 DOCX。"
        )

    def steer_text(self) -> str:
        return (
            "【执行中补充要求】从当前进度开始调整文档：每个后续章节正文开头加一句「本章要点：…」总结；"
            "所有「本章要点」中都必须包含标记 " + self.marker + "；"
            "对比表保留，其余结构不变。"
        )

    def audit(self, watch: dict[str, Any], steers: list[dict], result: dict[str, Any]) -> None:
        timeline = watch["timeline"]
        info = watch["info"]
        status = watch["status"]
        by_name: dict[str, list[dict]] = {}
        for event in timeline:
            by_name.setdefault(str(event.get("event")), []).append(event)
        self.report.update({
            "run_id": info.get("run_id"),
            "status": status,
            "duration_s": round(watch["finished"] - watch["created"], 1),
            "goal_mode": info.get("goal_mode"),
            "events": {name: len(items) for name, items in sorted(by_name.items())},
            "steering": steers,
            "pauses": watch["pauses"],
        })
        first = timeline[0]["at"] - watch["created"] if timeline else None
        gaps = [timeline[i]["at"] - timeline[i - 1]["at"] for i in range(1, len(timeline))]
        self.report["interaction"] = {
            "first_event_s": round(first, 2) if first is not None else None,
            "max_gap_s": round(max(gaps), 2) if gaps else 0.0,
        }

        artifact = None
        for event in reversed(by_name.get("artifact.created") or []):
            data = event["data"] or {}
            if data.get("kind") == "document" or data.get("document_id"):
                artifact = data
                break
        self.report["artifact"] = artifact
        final_text = ""
        completed = by_name.get("run.completed") or []
        if completed:
            final_text = str((completed[-1]["data"] or {}).get("final_text") or "")
        self.report["final_text_chars"] = len(final_text)

        checks: list[dict[str, Any]] = list(self.report.get("checks") or [])

        def check(name: str, passed: bool, detail: Any = None) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        applied = by_name.get("run.steering_applied") or by_name.get("run.steering.applied") or []
        check("goal_mode_enabled", self.report.get("goal_mode") is True, self.report.get("goal_mode"))
        check("run_completed", status == "completed", status)
        check("steer_accepted", len(steers) == 1 and steers[0]["status_code"] < 400,
              [item["status_code"] for item in steers])
        check("steering_applied_event", bool(applied), len(applied))
        check("artifact_created", bool(artifact), artifact.get("version_no") if artifact else None)
        check("document_marker", bool(result.get("marker_in_preview")), result.get("marker_in_preview"))
        check("no_placeholder", not result.get("placeholders"), result.get("placeholders"))
        check("first_event_latency", first is not None and first <= FIRST_EVENT_LIMIT_S,
              self.report["interaction"]["first_event_s"])
        check("max_silence", (max(gaps) if gaps else 0.0) <= MAX_GAP_LIMIT_S,
              self.report["interaction"]["max_gap_s"])
        check("steer_post_latency", all(item["latency_s"] <= STEER_POST_LIMIT_S for item in steers),
              [item["latency_s"] for item in steers])
        self.report["checks"] = checks
        self.report["passed"] = all(item["passed"] for item in checks)
        self.report["issues"] = [f"{item['check']}={item['detail']}" for item in checks if not item["passed"]]

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
                    "SELECT status, agent_config_snapshot, budget_deadline_at FROM agent_runs WHERE id=%s",
                    (run_id,),
                )
                row = cur.fetchone() or {}
                snapshot = row.get("agent_config_snapshot") or {}
                if isinstance(snapshot, str):
                    try:
                        snapshot = json.loads(snapshot)
                    except ValueError:
                        snapshot = {}
                cur.execute(
                    "SELECT id, content, delivered_at, delivered_via FROM agent_run_steering "
                    "WHERE run_id=%s ORDER BY id", (run_id,),
                )
                steering = [
                    {**item, "delivered_at": str(item.get("delivered_at"))}
                    for item in (cur.fetchall() or [])
                ]
                cur.execute(
                    "SELECT preview, size, change_summary FROM document_versions "
                    "WHERE created_by_run_id=%s ORDER BY version_no DESC LIMIT 1", (run_id,),
                )
                version = cur.fetchone() or {}
                preview = version.get("preview") or {}
                if isinstance(preview, str):
                    try:
                        preview = json.loads(preview)
                    except ValueError:
                        preview = {}
                text_parts = []
                for section in preview.get("sections") or []:
                    text_parts.append(str(section.get("title") or ""))
                    for block in section.get("blocks") or []:
                        if isinstance(block, dict):
                            text_parts.append(str(block.get("text") or ""))
                            for cell in block.get("rows") or []:
                                if isinstance(cell, list):
                                    text_parts.extend(str(item) for item in cell)
                            text_parts.append(str(block.get("caption") or ""))
                preview_text = "\n".join(text_parts)
                self.report["db"] = {
                    "status": row.get("status"),
                    "goal_mode": bool(snapshot.get("goal_mode")),
                    "budget_deadline_at": str(row.get("budget_deadline_at")),
                    "steering": steering,
                    "document_chars": len(preview_text),
                    "document_version_size": version.get("size"),
                }
                self.report["marker_in_preview"] = self.marker in preview_text
                self.report["document_chars"] = len(preview_text)
                self.report["placeholders"] = PLACEHOLDER.findall(preview_text)[:5]
                self.report["checks"].append({
                    "check": "db_goal_snapshot", "passed": bool(snapshot.get("goal_mode")),
                    "detail": snapshot.get("goal_mode"),
                })
                self.report["checks"].append({
                    "check": "db_no_deadline", "passed": row.get("budget_deadline_at") is None,
                    "detail": str(row.get("budget_deadline_at")),
                })
                self.report["checks"].append({
                    "check": "db_steering_document_path",
                    "passed": bool(steering) and all(item.get("delivered_via") == "document" for item in steering),
                    "detail": [item.get("delivered_via") for item in steering],
                })
                self.report["passed"] = all(item["passed"] for item in self.report["checks"])
                self.report["issues"] = [
                    f"{item['check']}={item['detail']}" for item in self.report["checks"] if not item["passed"]
                ]
        except Exception as exc:  # noqa: BLE001
            self.report["db_audit_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="目标模式文档任务 + 执行中补充要求探针")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--agent-id", type=int, default=0)
    parser.add_argument("--conversation-id", type=int, default=0)
    parser.add_argument("--title", default="目标模式文档验收")
    parser.add_argument("--steer-at", type=float, default=60.0)
    parser.add_argument("--turn-timeout", type=float, default=2400)
    parser.add_argument("--report", default="/tmp/goal_doc_probe.json")
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

    probe = DocProbe(args)
    try:
        if not args.conversation_id:
            if not args.agent_id:
                response = await probe.client.post("/api/v2/assistants", json={
                    "name": "目标模式文档验收助手",
                    "capability_ids": ["general"],
                    "system_prompt": "你是一名严谨的技术文档作者，交付结构完整、内容真实可核验的 Word 文档。",
                })
                response.raise_for_status()
                args.agent_id = int(response.json()["id"])
            args.conversation_id = await probe.create_conversation()
        probe.report["conversation_id"] = args.conversation_id
        started = _now()
        run = await probe.create_run(args.conversation_id, probe.build_message())
        run_id = run["run_id"]
        probe.report["run_id"] = run_id
        probe.report["goal_mode"] = run.get("goal_mode")
        print(f"会话 {args.conversation_id} · Run {run_id} · goal_mode={run.get('goal_mode')} "
              f"· 标记 {probe.marker}", flush=True)

        async def steer_after():
            wait = started + args.steer_at - _now()
            if wait > 0:
                await asyncio.sleep(wait)
            item = await probe.steer(run_id, probe.steer_text())
            print(f"补充要求: HTTP {item['status_code']} ({item['latency_s']}s)", flush=True)
            return item

        steer_task = asyncio.create_task(steer_after())
        watch = await probe.watch(run_id, started + args.turn_timeout, steer_task)
        try:
            steers = [await steer_task]
        except asyncio.CancelledError:
            steers = [{"text": probe.steer_text(), "status_code": 0, "latency_s": 0.0,
                       "response": {}, "skipped": "run_finished"}]
        db_result = {"marker_in_preview": False, "placeholders": []}
        probe.db_audit(run_id)
        probe.audit(watch, steers, {
            "marker_in_preview": probe.report.get("marker_in_preview"),
            "placeholders": probe.report.get("placeholders") or [],
        })
        Path(args.report).write_text(json.dumps(probe.report, ensure_ascii=False, indent=2, default=str),
                                     encoding="utf-8")
        print(json.dumps({
            "status": probe.report["status"], "duration_s": probe.report["duration_s"],
            "artifact": probe.report.get("artifact"), "document_chars": probe.report.get("document_chars"),
            "steering": probe.report["steering"], "interaction": probe.report["interaction"],
            "checks": probe.report["checks"], "passed": probe.report.get("passed"),
            "report": args.report,
        }, ensure_ascii=False, indent=2), flush=True)
        return 0 if probe.report.get("passed") else 1
    finally:
        await probe.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
