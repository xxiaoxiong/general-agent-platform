#!/usr/bin/env python3
"""意图切换端到端探针：会话里有 Word 文档后，新任务不得再被绑成 Word 交付。

真实模型、真实后端、单会话多轮：
  1) 先生成一份 Word 文档（会话内产生 document 产物与 document_run_id）；
  2) 用户要求「放到md文档中给到我下载」→ 必须交付真实 .md 文件，不得再生成 DOCX；
  3) 普通知识问答 → 必须按对话回答，不得产生任何文档产物；
  4) 指回刚才的 Word 第 2 章要求改写 → 必须走文档编辑并产生新版本；
  5) 换话题（健身计划）→ 必须按对话回答，不得被历史文档绑架。

容器内运行：
  docker cp backend/tests/e2e/intent_switch_probe.py ai_plat_backend:/tmp/intent_probe.py
  docker exec -e PYTHONPATH=/app -w /app ai_plat_backend python /tmp/intent_probe.py \
      --user-id 2 --agent-id 12 --report /tmp/intent_switch_probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "cancelled", "limit_exceeded", "needs_reconciliation", "suspended"}
BLOCKED = {"waiting_input", "waiting_approval"}

_DOC_PATH = re.compile(r"(?:/workspace/)?[\w.\-()\u4e00-\u9fff]{1,120}\.md", re.I)


def _now() -> float:
    return time.monotonic()


class IntentProbe:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = httpx.AsyncClient(
            base_url=args.base.rstrip("/"),
            headers={"Authorization": f"Bearer {args.token}"},
            timeout=httpx.Timeout(connect=15, read=None, write=60, pool=60),
        )
        self.report: dict[str, Any] = {"turns": [], "checks": [], "issues": [], "passed": False}
        self.document_id: int | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def create_conversation(self) -> int:
        response = await self.client.post("/api/v2/conversations", json={
            "assistant_id": self.args.agent_id, "title": "意图切换验收",
        })
        response.raise_for_status()
        return int(response.json()["id"])

    async def run_turn(
        self, session_id: int, message: str, *, preview: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message, "conversation_id": session_id,
            "approval_mode": "auto", "web_enabled": False,
        }
        if preview:
            body["preview_document_id"] = preview["document_id"]
            if preview.get("version_no"):
                body["preview_document_version_no"] = preview["version_no"]
        created = _now()
        response = await self.client.post("/api/v2/runs", json=body)
        if response.status_code == 409:
            await asyncio.sleep(2)
            created = _now()
            response = await self.client.post("/api/v2/runs", json=body)
        response.raise_for_status()
        create_latency = round(_now() - created, 2)
        payload = response.json()
        watch = await self.watch(str(payload["run_id"]))
        watch["create_latency_s"] = create_latency
        return watch

    async def watch(self, run_id: str) -> dict[str, Any]:
        started = _now()
        deadline = started + self.args.turn_timeout
        timeline: list[dict[str, Any]] = []
        sequence = 0
        status = "running"
        while _now() < deadline:
            try:
                async with self.client.stream(
                    "GET", f"/api/v2/runs/{run_id}/events", params={"after_seq": sequence},
                    timeout=httpx.Timeout(connect=15, read=None, write=30, pool=30),
                ) as response:
                    response.raise_for_status()
                    name: str | None = None
                    async for line in response.aiter_lines():
                        if _now() > deadline:
                            break
                        if line.startswith("event:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            data = json.loads(line.split(":", 1)[1].strip())
                            timeline.append({
                                "event": name or data.get("event"),
                                "sequence": int(data.get("sequence") or 0),
                                "data": data.get("data") or {},
                            })
                            sequence = max(sequence, int(data.get("sequence") or 0))
                            if str(data.get("event") or name) in TERMINAL:
                                status = str(data.get("event") or name)
            except (httpx.RemoteProtocolError, httpx.ReadError):
                pass
            if status in TERMINAL:
                break
            info = (await self.client.get(f"/api/v2/runs/{run_id}")).json()
            status = str(info.get("status") or status)
            if status in TERMINAL or status in BLOCKED:
                break
            await asyncio.sleep(0.4)
        return {
            "run_id": run_id, "status": status, "timeline": timeline,
            "duration_s": round(_now() - started, 1),
        }

    async def files(self, session_id: int) -> list[dict[str, Any]]:
        response = await self.client.get(f"/api/v2/conversations/{session_id}/file-list")
        response.raise_for_status()
        return list(response.json().get("files") or [])

    def summarize(self, watch: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for event in watch["timeline"]:
            by_name.setdefault(str(event.get("event")), []).append(event)
        routed = (by_name.get("task.routed") or [{}])[0].get("data") or {}
        completed = by_name.get("run.completed") or []
        failed = by_name.get("run.failed") or []
        final_text = ""
        if completed or failed:
            final_text = str((completed or failed)[-1]["data"].get("final_text") or "")
        versions = by_name.get("document.version.created") or []
        artifacts = by_name.get("artifact.created") or []
        if versions:
            self.document_id = int((versions[-1]["data"] or {}).get("document_id") or self.document_id or 0) or None
        return {
            "status": watch["status"],
            "duration_s": watch["duration_s"],
            "create_latency_s": watch.get("create_latency_s"),
            "task_type": routed.get("task_type"),
            "reason": routed.get("reason"),
            "intent_source": routed.get("intent_source"),
            "deliverable": routed.get("deliverable"),
            "document_versions": len(versions),
            "artifacts": len(artifacts),
            "document_id": self.document_id,
            "final_text_chars": len(final_text),
            "final_text_head": final_text[:600],
            "final_text_tail": final_text[-300:],
            "files": [{"name": item.get("name"), "size": item.get("size")} for item in files],
            "tool_calls": len(by_name.get("tool.completed") or []),
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description="意图切换端到端探针")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument("--conversation-id", type=int, default=0)
    parser.add_argument("--turn-timeout", type=float, default=600)
    parser.add_argument("--report", default="/tmp/intent_switch_probe.json")
    args = parser.parse_args()

    if not args.token:
        if not args.user_id:
            raise SystemExit("需要 --token 或 --user-id")
        from app.core.security import create_access_token

        args.token = create_access_token(args.user_id)

    probe = IntentProbe(args)
    try:
        session_id = args.conversation_id or await probe.create_conversation()
        probe.report["conversation_id"] = session_id
        print(f"会话 {session_id} · 助手 {args.agent_id}", flush=True)

        turns = [
            ("word_generate",
             "写一份《AI Agent 评测入门》Word 文档给我：3 章左右，每章至少 150 字；"
             "不需要联网，也不需要再向我确认额外信息，缺少的信息采用通用假设并在文中注明。"
             "完成后直接交付可下载的 DOCX。", None),
            ("markdown_delivery", "放到md文档中给到我下载", None),
            ("plain_question", "用三句话解释一下向量数据库和倒排索引的区别", None),
            ("edit_word", "把刚才那份 Word 文档的第 2 章改写得具体一些，补一个实际例子", "preview"),
            ("switch_topic", "换个话题：帮我列一份本周健身训练计划", None),
        ]
        records: list[dict[str, Any]] = []
        for name, message, preview_mode in turns:
            preview = None
            if preview_mode == "preview" and probe.document_id:
                preview = {"document_id": probe.document_id, "version_no": None}
            before = await probe.files(session_id) if name != "word_generate" else []
            print(f"[{name}] 发送：{message[:40]}", flush=True)
            watch = await probe.run_turn(session_id, message, preview=preview)
            files = await probe.files(session_id)
            detail = probe.summarize(watch, files)
            detail.update({"turn": name, "message": message, "preview": preview})
            if name == "word_generate" and detail["document_id"] is None:
                await asyncio.sleep(0)
            records.append(detail)
            probe.report["turns"] = records
            print(f"  -> {detail['status']} · {detail['task_type']} · {detail['duration_s']}s · "
                  f"versions={detail['document_versions']} files={[f['name'] for f in detail['files']]}", flush=True)
            if detail["status"] in BLOCKED:
                probe.report["issues"].append(f"{name}: Run 进入 {detail['status']}，需要人工输入/审批")
                break
            if detail["status"] != "completed":
                probe.report["issues"].append(f"{name}: Run 未正常完成（{detail['status']}）")
                break

        def turn(name: str) -> dict[str, Any] | None:
            return next((item for item in records if item["turn"] == name), None)

        def check(name: str, ok: bool, detail: Any) -> None:
            probe.report["checks"].append({"check": name, "passed": bool(ok), "detail": detail})
            if not ok:
                probe.report["issues"].append(f"{name}: {detail}")

        word = turn("word_generate")
        check("word_generate_routed", bool(word and word["task_type"] == "document_generate"),
              word and word["task_type"])
        check("word_generate_artifact", bool(word and word["document_versions"] >= 1),
              word and word["document_versions"])

        markdown = turn("markdown_delivery")
        md_files = [item for item in (markdown or {}).get("files", []) if str(item.get("name", "")).lower().endswith(".md")]
        check("markdown_routed_general_chat", bool(markdown and markdown["task_type"] == "general_chat"),
              markdown and markdown["task_type"])
        check("markdown_deliverable_format", bool(markdown and (markdown.get("deliverable") or {}).get("format") == "md"),
              markdown and markdown.get("deliverable"))
        check("markdown_no_new_docx", bool(markdown and markdown["document_versions"] == 0),
              markdown and markdown["document_versions"])
        check("markdown_file_exists", bool(md_files), md_files)
        check("markdown_answer_mentions_file", bool(markdown and _DOC_PATH.search(markdown["final_text_head"] + markdown["final_text_tail"])),
              markdown and markdown["final_text_tail"])

        plain = turn("plain_question")
        check("plain_question_general_chat", bool(plain and plain["task_type"] == "general_chat"),
              plain and plain["task_type"])
        check("plain_question_no_artifact", bool(plain and plain["artifacts"] == 0 and plain["document_versions"] == 0),
              plain and (plain["artifacts"], plain["document_versions"]))
        check("plain_question_answered", bool(plain and plain["final_text_chars"] > 40),
              plain and plain["final_text_chars"])

        edit = turn("edit_word")
        check("edit_word_routed", bool(edit and edit["task_type"] == "document_edit"),
              edit and edit["task_type"])
        check("edit_word_new_version", bool(edit and edit["document_versions"] >= 1),
              edit and edit["document_versions"])

        switch = turn("switch_topic")
        check("switch_topic_general_chat", bool(switch and switch["task_type"] == "general_chat"),
              switch and switch["task_type"])
        check("switch_topic_no_artifact", bool(switch and switch["artifacts"] == 0 and switch["document_versions"] == 0),
              switch and (switch["artifacts"], switch["document_versions"]))

        probe.report["passed"] = not probe.report["issues"] and all(
            item["passed"] for item in probe.report["checks"])
        Path(args.report).write_text(json.dumps(probe.report, ensure_ascii=False, indent=2, default=str),
                                     encoding="utf-8")
        print(json.dumps({
            "passed": probe.report["passed"],
            "checks": probe.report["checks"],
            "issues": probe.report["issues"],
            "report": args.report,
        }, ensure_ascii=False, indent=2), flush=True)
        return 0 if probe.report["passed"] else 1
    finally:
        await probe.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
