"""Team Tools：Leader / Member 的受控团队协作工具。

工具只是接口，权限与事实由后端强制：
- 所有操作按 ``RuntimeContext.team_context`` 中的 team_run_id 作用域校验；
- Leader 专属工具（建任务/分配/查看成员）在 handler 内再次校验角色；
- 成员只能更新本人任务的 blocked 状态；
- 工具风险显式登记（未登记默认拒绝），执行仍走 ToolPolicy + Tool Ledger；
- 工具返回紧凑 JSON，长内容必须走 Artifact，避免把成果塞进消息或 Prompt。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.services.tools.result import ToolResult

from ..errors import TaskConflict, TaskStateError, TeamError, TeamNotFound

TaskIdPattern = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

_CREATE_PARAMS = {
    "task_id": {"type": "string", "description": "任务 ID：小写字母开头，可含数字/下划线/连字符，≤24"},
    "role": {"type": "string", "description": "负责该任务的成员角色（团队内唯一）"},
    "title": {"type": "string", "description": "任务标题（≤100 字）"},
    "brief": {"type": "string", "description": "交付要求、范围与验收要点（≤600 字）"},
    "depends_on": {"type": "array", "description": "依赖的任务 ID 列表（可空）"},
    "priority": {"type": "integer", "description": "优先级（越大越先），默认 0"},
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "team_task_create",
        "display_name": "创建团队任务",
        "category": "team",
        "description": "在 Task Board 创建一项团队任务（仅团队负责人可用）。返回任务 ID 与 revision。",
        "parameters": _CREATE_PARAMS,
        "required": ["task_id", "role", "title", "brief"],
    },
    {
        "name": "team_task_list",
        "display_name": "查看任务板",
        "category": "team",
        "description": "列出当前团队全部任务及状态、负责人、依赖与 revision。",
        "parameters": {},
        "required": [],
    },
    {
        "name": "team_task_get",
        "display_name": "查看任务",
        "category": "team",
        "description": "读取单个任务的完整信息（含 revision 与结果产物引用）。",
        "parameters": {"task_id": {"type": "string", "description": "任务 ID"}},
        "required": ["task_id"],
    },
    {
        "name": "team_task_update",
        "display_name": "更新任务",
        "category": "team",
        "description": (
            "按 revision 更新任务（CAS）。负责人可改标题/描述/优先级/状态（pending/blocked/cancelled）；"
            "成员仅可把本人任务标记为 blocked 并说明原因。"
        ),
        "parameters": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "expected_revision": {"type": "integer", "description": "当前 revision（来自 task_get）"},
            "subject": {"type": "string", "description": "新标题（可选）"},
            "description": {"type": "string", "description": "新描述（可选）"},
            "priority": {"type": "integer", "description": "新优先级（可选）"},
            "status": {"type": "string", "description": "pending / blocked / cancelled（可选）"},
            "blocked_reason": {"type": "string", "description": "阻塞原因（可选）"},
        },
        "required": ["task_id", "expected_revision"],
    },
    {
        "name": "team_assign_task",
        "display_name": "分配任务",
        "category": "team",
        "description": "把待执行任务分配给指定角色或成员（仅团队负责人可用）。",
        "parameters": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "member": {"type": "string", "description": "成员 member_id 或角色名"},
            "expected_revision": {"type": "integer", "description": "当前 revision"},
        },
        "required": ["task_id", "member", "expected_revision"],
    },
    {
        "name": "team_send_message",
        "display_name": "发送团队消息",
        "category": "team",
        "description": "向团队其他成员发送持久化消息（可携带 artifact_id）。重复发送请携带 idempotency_key。",
        "parameters": {
            "recipient": {"type": "string", "description": "接收者 member_id 或角色名；留空=发给负责人"},
            "message_type": {"type": "string", "description": "消息类型，默认 text"},
            "payload": {"type": "object", "description": "结构化消息内容（长篇内容请用 Artifact）"},
            "artifact_id": {"type": "string", "description": "关联产物 ID（可选）"},
            "correlation_id": {"type": "string", "description": "关联 ID（可选）"},
            "idempotency_key": {"type": "string", "description": "幂等键（可选）"},
        },
        "required": [],
    },
    {
        "name": "team_member_status",
        "display_name": "查看成员状态",
        "category": "team",
        "description": "查看团队成员身份、状态、当前任务、预算用量与最近错误（仅负责人可用）。",
        "parameters": {"member_id": {"type": "string", "description": "成员 ID（可选，缺省返回全部）"}},
        "required": [],
    },
    {
        "name": "team_artifact_get",
        "display_name": "读取成员产物",
        "category": "team",
        "description": "按 artifact_id 读取团队内成员成果（可指定片段）。",
        "parameters": {
            "artifact_id": {"type": "string", "description": "产物 ID"},
            "max_chars": {"type": "integer", "description": "本次读取的最大字符数（默认 6000）"},
            "offset": {"type": "integer", "description": "起始字符偏移（可选）"},
        },
        "required": ["artifact_id"],
    },
    {
        "name": "team_artifact_create",
        "display_name": "保存成员产物",
        "category": "team",
        "description": "把交付内容保存为 Artifact，返回 artifact_id 供任务/消息引用。",
        "parameters": {
            "name": {"type": "string", "description": "产物名称"},
            "content": {"type": "string", "description": "完整内容"},
            "content_type": {"type": "string", "description": "MIME 类型，默认 text/plain"},
        },
        "required": ["name", "content"],
    },
    {
        "name": "team_inbox_list",
        "display_name": "读取团队收件箱",
        "category": "team",
        "description": "读取发给自己的团队消息；可短暂等待新消息（可恢复等待，最多 30 秒）。",
        "parameters": {
            "wait_seconds": {"type": "number", "description": "等待新消息的秒数（0=立即返回）"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
        },
        "required": [],
    },
    {
        "name": "team_message_ack",
        "display_name": "确认团队消息",
        "category": "team",
        "description": "确认已处理某条消息（幂等）。",
        "parameters": {"message_id": {"type": "string", "description": "消息 ID"}},
        "required": ["message_id"],
    },
]

LEADER_TOOL_NAMES = frozenset({
    "team_task_create", "team_task_list", "team_task_get", "team_task_update",
    "team_assign_task", "team_send_message", "team_member_status", "team_artifact_get",
})
MEMBER_TOOL_NAMES = frozenset({
    "team_task_get", "team_task_update", "team_send_message", "team_inbox_list",
    "team_message_ack", "team_artifact_get", "team_artifact_create",
})
ALL_TOOL_NAMES = LEADER_TOOL_NAMES | MEMBER_TOOL_NAMES

_SPECS_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


def build_tool_specs(*, leader: bool) -> list[dict[str, Any]]:
    names = LEADER_TOOL_NAMES if leader else MEMBER_TOOL_NAMES
    return [spec for name, spec in _SPECS_BY_NAME.items() if name in names]


def _team(ctx) -> dict[str, Any]:
    team_context = getattr(ctx, "team_context", None)
    if not isinstance(team_context, dict) or not team_context.get("team_run_id"):
        raise TeamError("当前执行环境不属于团队，无法使用团队协作工具")
    return team_context


def _is_leader(ctx) -> bool:
    return bool(_team(ctx).get("is_leader"))


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str)


def _fail(message: str, **data: Any) -> ToolResult:
    return ToolResult.failed(message, **data)


async def team_task_create(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    if not team.get("is_leader"):
        return _fail("仅团队负责人可以创建任务")
    task_id = str(params.get("task_id") or "").strip()
    if not TaskIdPattern.match(task_id):
        return _fail("task_id 非法：须以小写字母开头，仅含小写字母/数字/下划线/连字符，长度≤24")
    role = str(params.get("role") or "").strip()
    if not role:
        return _fail("role 不能为空")
    dependencies = params.get("depends_on") or []
    if not isinstance(dependencies, list):
        return _fail("depends_on 必须是数组")
    board = team["board"]
    existing = await board.list_tasks(team["team_run_id"])
    if len(existing) >= 12:
        return _fail("团队任务数已达上限（12）")
    task = await board.create_task(
        team["team_run_id"],
        task_id=task_id,
        subject=str(params.get("title") or "")[:200],
        description=str(params.get("brief") or "")[:20_000],
        role=role,
        dependencies=[str(item) for item in dependencies],
        priority=int(params.get("priority") or 0),
    )
    return _ok(task=task, message=f"任务 {task_id} 已创建，revision={task['revision']}")


async def team_task_list(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    tasks = await team["board"].list_tasks(team["team_run_id"])
    compact = [
        {
            "task_id": task["id"],
            "role": task["role"],
            "title": task["title"],
            "status": task["board_status"],
            "depends_on": task["depends_on"],
            "owner_member_id": task["owner_member_id"],
            "revision": task["revision"],
            "result_artifact_id": task["result_artifact_id"],
            "error": task["error"],
        }
        for task in tasks
    ]
    return _ok(tasks=compact)


async def team_task_get(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    task_id = str(params.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id 不能为空")
    try:
        task = await team["board"].get_task(team["team_run_id"], task_id)
    except TeamNotFound as exc:
        return _fail(str(exc))
    payload = dict(task)
    payload.pop("lease_token", None)
    return _ok(task=payload)


async def team_task_update(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    task_id = str(params.get("task_id") or "").strip()
    expected_revision = params.get("expected_revision")
    if not task_id or expected_revision is None:
        return _fail("task_id 与 expected_revision 必填")
    changes = {
        key: params[key]
        for key in ("subject", "description", "priority", "status", "blocked_reason")
        if params.get(key) is not None
    }
    if not changes:
        return _fail("没有需要更新的字段")
    actor = {"member_id": team["member_id"], "is_leader": bool(team.get("is_leader"))}
    try:
        task = await team["board"].update_task(
            team["team_run_id"], task_id,
            expected_revision=int(expected_revision),
            actor=actor,
            changes=changes,
        )
    except (TaskConflict, TaskStateError, TeamNotFound) as exc:
        return _fail(str(exc))
    return _ok(task=task, message="任务已更新")


async def team_assign_task(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    if not team.get("is_leader"):
        return _fail("仅团队负责人可以分配任务")
    task_id = str(params.get("task_id") or "").strip()
    member = str(params.get("member") or "").strip()
    expected_revision = params.get("expected_revision")
    if not task_id or not member or expected_revision is None:
        return _fail("task_id / member / expected_revision 必填")
    try:
        task = await team["board"].assign(
            team["team_run_id"], task_id,
            member=member,
            expected_revision=int(expected_revision),
        )
    except (TaskConflict, TaskStateError, TeamNotFound) as exc:
        return _fail(str(exc))
    return _ok(task=task, message="任务已分配")


async def team_send_message(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    recipient = str(params.get("recipient") or "").strip() or None
    payload = params.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _fail("payload 必须是 JSON 对象")
    try:
        message = await team["messages"].send(
            team["team_run_id"],
            sender_member_id=team["member_id"],
            recipient_member_id=await _resolve_recipient(team, recipient),
            message_type=str(params.get("message_type") or "text")[:40],
            payload=payload,
            artifact_id=(str(params["artifact_id"]) if params.get("artifact_id") else None),
            correlation_id=(
                str(params["correlation_id"])[:64] if params.get("correlation_id") else None
            ),
            idempotency_key=(
                str(params["idempotency_key"])[:128]
                if params.get("idempotency_key") else None
            ),
        )
    except TeamError as exc:
        return _fail(str(exc))
    return _ok(message=message, duplicate=not message.get("created", True))


async def team_member_status(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    if not team.get("is_leader"):
        return _fail("仅团队负责人可以查看成员状态")
    member_id = str(params.get("member_id") or "").strip() or None
    members = await team["members"].list_members(team["team_run_id"])
    if member_id:
        members = [item for item in members if item["member_id"] == member_id]
        if not members:
            return _fail(f"成员 {member_id} 不存在")
    return _ok(members=members)


async def team_artifact_get(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    artifact_id = str(params.get("artifact_id") or "").strip()
    if not artifact_id:
        return _fail("artifact_id 不能为空")
    max_chars = int(params.get("max_chars") or 6000)
    offset = int(params.get("offset") or 0)
    if not 1 <= max_chars <= 20_000:
        return _fail("max_chars 必须在 1~20000 之间")
    try:
        artifact = await team["artifacts"].get(
            team["team_run_id"], artifact_id, snippet_chars=max_chars, offset=offset,
        )
    except TeamNotFound as exc:
        return _fail(str(exc))
    return _ok(artifact=artifact)


async def team_artifact_create(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    name = str(params.get("name") or "").strip()
    content = params.get("content")
    if not name or not isinstance(content, str) or not content.strip():
        return _fail("name 与 content 必填")
    artifact = await team["artifacts"].create(
        team["team_run_id"],
        member_id=team["member_id"],
        task_id=team.get("task_id"),
        name=name,
        content=content,
        content_type=str(params.get("content_type") or "text/plain"),
    )
    return _ok(artifact=artifact, message="产物已保存")


async def team_inbox_list(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    wait_seconds = float(params.get("wait_seconds") or 0)
    limit = int(params.get("limit") or 20)
    messages = await team["messages"].list_inbox(
        team["team_run_id"], team["member_id"], limit=max(1, min(50, limit)),
    )
    if not messages and wait_seconds > 0:
        message = await team["messages"].wait_for_message(
            team["team_run_id"], team["member_id"], timeout=min(wait_seconds, 30.0),
        )
        messages = [message] if message else []
    return _ok(messages=messages)


async def team_message_ack(ctx, params: dict[str, Any]) -> str | ToolResult:
    team = _team(ctx)
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return _fail("message_id 不能为空")
    try:
        outcome = await team["messages"].ack(
            team["team_run_id"], team["member_id"], message_id,
        )
    except TeamError as exc:
        return _fail(str(exc))
    return _ok(status=outcome)


async def _resolve_recipient(team: dict[str, Any], recipient: Optional[str]) -> Optional[str]:
    """接收者支持 member_id / 角色名 / leader；空=负责人。"""
    members = await team["members"].list_members(team["team_run_id"])
    if not recipient:
        return team.get("leader_member_id") or "leader"
    if recipient in {item["member_id"] for item in members}:
        return recipient
    matches = [
        item for item in members
        if item["role"].casefold() == recipient.casefold()
        or item["name"].casefold() == recipient.casefold()
    ]
    if len(matches) == 1:
        return matches[0]["member_id"]
    raise TeamNotFound(f"无法解析接收者 '{recipient}'（成员不存在或角色不唯一）")


HANDLERS = {
    "team_task_create": team_task_create,
    "team_task_list": team_task_list,
    "team_task_get": team_task_get,
    "team_task_update": team_task_update,
    "team_assign_task": team_assign_task,
    "team_send_message": team_send_message,
    "team_member_status": team_member_status,
    "team_artifact_get": team_artifact_get,
    "team_artifact_create": team_artifact_create,
    "team_inbox_list": team_inbox_list,
    "team_message_ack": team_message_ack,
}


# ── 风险登记（未登记工具默认拒绝）────────────────────────────────────────

def register_team_tool_risks() -> None:
    from app.services.agent_runtime.tools.policy import (
        CLASS_NON_IDEMPOTENT, CLASS_READ_ONLY, RETRY_NONE, RETRY_SAFE,
        ToolRisk, ToolRiskProfile, register_tool_risk,
    )

    read_only = dict(side_effect_class=CLASS_READ_ONLY, retry_policy=RETRY_SAFE)
    write = dict(side_effect_class=CLASS_NON_IDEMPOTENT, retry_policy=RETRY_NONE)
    entries = [
        ToolRiskProfile("team_task_list", ToolRisk.READ, **read_only),
        ToolRiskProfile("team_task_get", ToolRisk.READ, **read_only),
        ToolRiskProfile("team_member_status", ToolRisk.READ, **read_only),
        ToolRiskProfile("team_artifact_get", ToolRisk.READ, **read_only),
        ToolRiskProfile("team_inbox_list", ToolRisk.READ, **read_only),
        ToolRiskProfile("team_task_create", ToolRisk.WRITE, **write),
        ToolRiskProfile("team_task_update", ToolRisk.WRITE, **write),
        ToolRiskProfile("team_assign_task", ToolRisk.WRITE, **write),
        ToolRiskProfile("team_send_message", ToolRisk.WRITE, **write),
        ToolRiskProfile("team_message_ack", ToolRisk.WRITE, **write),
        ToolRiskProfile("team_artifact_create", ToolRisk.WRITE, **write),
    ]
    for entry in entries:
        register_tool_risk(entry)


register_team_tool_risks()
