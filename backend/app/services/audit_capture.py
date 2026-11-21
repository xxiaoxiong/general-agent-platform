"""在业务事务中记录平台数据库实际发生的变化。"""

from __future__ import annotations

import re
from contextvars import Context, copy_context
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, event, inspect, or_, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import ORMExecuteState, Session

from app.models.agent import session_message_count_expr
from app.models.audit import AuditLog
from app.services.audit_context import AuditContext, current_audit


# 按资源选取业务字段。会话正文、文件内容、模型输入输出、执行租约和心跳属于其他记录。
_FIELDS = {
    "users": ("user", "username email hashed_password full_name avatar is_active is_superuser department phone"),
    "roles": ("user", "name display_name description is_system"),
    "permissions": ("user", "code name description module"),
    "user_roles": ("user", "user_id role_id"),
    "role_permissions": ("user", "role_id permission_id"),
    "agents": ("agent", "name description icon model_config_id system_prompt max_iterations max_tokens "
                "temperature tools capability_ids is_default knowledge_base_id database_connection_id is_active owner_id"),
    "agent_versions": ("agent", "agent_id version_no snapshot created_by published_at"),
    "agent_sessions": ("agent", "agent_id user_id title title_edited pinned_at"),
    "agent_runs": ("agent", "agent_id agent_version_id session_id user_id status cancel_requested cancel_requested_by cancel_requested_at"),
    "agent_approvals": ("agent", "run_id action_id tool_name decision_status decision decided_by decided_at"),
    "agent_tool_executions": ("agent", "run_id tool_name reconcile_decision reconciled_by reconciled_at"),
    "run_input_requests": ("agent", "run_id ordinal status required_fields submitted_at"),
    "document_assets": ("document", "owner_id title document_type session_id profile_id current_version status created_by_run_id"),
    "document_versions": ("document", "document_id version_no filename mime_type size profile_id profile_version_no "
                          "created_by_run_id change_summary content_hash"),
    "document_profiles": ("document", "owner_id name description document_type visibility status current_version published_version deleted_at"),
    "document_profile_versions": ("document", "profile_id version_no definition change_summary created_by created_by_run_id"),
    "database_connections": ("database", "name description db_type host port database username password extra_params "
                              "is_active owner_id last_test_ok last_tested_at"),
    "model_providers": ("model", "name display_name provider_type base_url api_key is_active is_default description logo_url extra_config"),
    "model_configs": ("model", "provider_id name display_name model_type context_length max_tokens input_price output_price "
                      "is_active supports_streaming supports_function_call supports_structured_output supports_vision "
                      "supports_parallel_tool_calls capability_status capability_verified_at configuration_version description extra_params"),
    "knowledge_bases": ("knowledge", "name description embedding_model chunk_size chunk_overlap is_active owner_id doc_count vector_count"),
    "knowledge_documents": ("knowledge", "knowledge_base_id filename file_type file_size status chunk_count"),
    "workflows": ("workflow", "name description definition is_active is_published version owner_id trigger_type trigger_config category icon"),
    "workflow_executions": ("workflow", "workflow_id status trigger_by duration_ms"),
    "prompt_templates": ("prompt", "name description category content variables is_public owner_id version tags"),
    "prompt_template_versions": ("prompt", "template_id version content variables change_note created_by"),
    "app_templates": ("app", "name display_name description category icon cover_image app_type config_schema "
                      "default_config workflow_definition system_prompt is_builtin is_active sort_order tags"),
    "app_instances": ("app", "name description template_id workflow_id owner_id model_config_id knowledge_base_id "
                      "config is_active is_public access_token"),
    "skills": ("skill", "name description version body files enabled agent_ids source created_by"),
    "mcp_servers": ("mcp", "name description transport command args env url headers enabled require_approval "
                    "side_effect_class tool_settings agent_ids discovered_tools created_by last_test_at"),
    "eval_datasets": ("evaluation", "name description source category scoring_method reference created_by"),
    "eval_dataset_items": ("evaluation", "dataset_id position input expected criteria scoring_method scoring_config "
                           "complexity task_type required_tools expected_min_steps turns"),
    "eval_runs": ("evaluation", "name dataset_id agent_id agent_version_id status config created_by"),
    "evolution_reports": ("evolution", "agent_id baseline_version_no window_start window_end status created_by validated_at"),
    "evolution_change_log": ("evolution", "report_id agent_id change_type target before after applied_version_no "
                             "status applied_by reverted_at revert_of_change_id"),
}

_MASK = "（敏感内容已隐藏）"
_OMITTED = "（部分内容已截断）"
_OPAQUE = {
    "env", "headers", "authorization", "cookie", "setcookie", "credentials", "secret", "secrets",
    "privatekey", "token", "extraconfig", "extraparams", "args", "phone", "email", "dsn", "connectionstring",
}
_SECRET_KEY = re.compile(
    r"password|passwd|passphrase|credential|secret|authorization|cookie|privatekey|apikey|accesskey|token$|^pwd$", re.I,
)
_SECRET_TEXT = re.compile(
    r"(\b(?:authorization|api[-_ ]?key|password|passwd|secret|access[-_ ]?token|refresh[-_ ]?token|token)"
    r"[\"']?\s*[:=]\s*(?:(?:bearer|basic)\s+)?)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;\"']+)", re.I,
)
_URL_AUTH = re.compile(r"(://)[^/@\s]+@")
_REQUEST_FIELDS = ("request_id", "method", "path", "ip_address", "user_agent", "user_id", "username")
_PENDING = "audit_capture_pending"


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _OPAQUE or bool(_SECRET_KEY.search(normalized))


def _public(key: str, value: Any, budget: list[int], depth: int = 0) -> Any:
    # 限制整份快照的序列化体积，保守按 Unicode 转义估算。
    budget[0] -= 32 + len(key) * 6
    if value is None:
        return None
    if _sensitive_key(key):
        return _MASK
    if depth >= 8 or budget[0] <= 0:
        return _OMITTED
    if isinstance(value, dict):
        named_secret = any(isinstance(value.get(k), str) and _sensitive_key(value[k]) for k in ("key", "name"))
        result = {}
        for position, (name, item) in enumerate(value.items()):
            if position >= 50 or budget[0] <= 0:
                result["…"] = _OMITTED
                break
            name = str(name)
            if named_secret and name in {"value", "default", "default_value"}:
                result[name] = _MASK if item is not None else None
            else:
                result[name[:160]] = _public(name, item, budget, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = []
        for position, item in enumerate(value):
            if position >= 50 or budget[0] <= 0:
                items.append(_OMITTED)
                break
            items.append(_public("", item, budget, depth + 1))
        return items
    if isinstance(value, str):
        # 复用运行时的 URL 签名、Bearer 和请求头脱敏，附加审计需处理的配置文本。
        from app.services.agent_runtime.events import redact_secrets

        clean = redact_secrets(_SECRET_TEXT.sub(r"\1***", _URL_AUTH.sub(r"\1***@", value)))
        limit = min(2048, budget[0] // 6)
        budget[0] -= min(len(clean), limit) * 6
        return clean if len(clean) <= limit else clean[:limit] + _OMITTED
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return _MASK


def run_audit_origin() -> dict | None:
    """将请求关联号传给可跨进程恢复的任务。"""
    context = current_audit.get()
    return {field: getattr(context, field) for field in _REQUEST_FIELDS} if context else None


def worker_audit_context(origin: dict | None) -> Context:
    """执行恢复任务时绑定发起者，不沿用碰巧创建后台协程的其他请求上下文。"""
    context = copy_context()
    audit = AuditContext(**{field: origin.get(field) for field in _REQUEST_FIELDS}) if origin else None
    context.run(current_audit.set, audit)
    return context


def _fields(table):
    return _FIELDS.get(table.name) if table.metadata is AuditLog.metadata else None


def _columns(table, dialect):
    names = _FIELDS[table.name][1].split()
    columns = [*table.primary_key.columns, *(table.c[name] for name in names if name in table.c)]
    if table.name == "agent_sessions":
        columns.append(session_message_count_expr(dialect).label("message_count"))
    return columns


def _read(conn, table, where, *, lock: bool = False, parameters=None) -> list[dict]:
    query = select(*_columns(table, conn.dialect.name))
    if where is not None:
        query = query.where(where)
    if lock and conn.dialect.name != "sqlite":
        query = query.with_for_update()
    return [dict(row) for row in conn.execute(query, parameters or {}).mappings()]


def _write_connection(session):
    conn = session.connection()
    if conn.dialect.name == "sqlite" and not conn.connection.driver_connection.in_transaction:
        # SQLite 的延迟事务默认不会在 SELECT 时拿写锁。
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    return conn


def _condition(table, identities):
    columns = list(table.primary_key.columns)
    if len(columns) == 1:
        return columns[0].in_([identity[0] for identity in identities])
    return or_(*(and_(*(column == value for column, value in zip(table.primary_key.columns, identity)))
                 for identity in identities))


def _identity(table, row):
    return tuple(row[column.key] for column in table.primary_key.columns)


def _snapshot(row: dict | None) -> dict | None:
    if row is None:
        return None
    budget = [64 * 1024]
    return {name: _public(name, value, budget) for name, value in row.items()}


def _entry(context, table, before: dict | None, after: dict | None) -> dict | None:
    old, new = before or {}, after or {}
    changed = sorted(name for name in old.keys() | new.keys() if name not in old or name not in new or old[name] != new[name])
    if not changed:
        return None
    return {
        "event_type": "change", "request_id": context.request_id, "http_method": context.method,
        "request_path": context.path, "user_id": context.user_id, "username": context.username,
        "ip_address": context.ip_address, "user_agent": context.user_agent,
        "status": "success", "action": "create" if before is None else "delete" if after is None else "update",
        "module": _FIELDS[table.name][0], "resource_type": table.name,
        "resource_id": ":".join(str(part) for part in _identity(table, after if after is not None else before)),
        "before_data": _snapshot(before), "after_data": _snapshot(after), "changed_fields": changed,
    }


def _write(conn, context, table, before, after):
    earlier = {_identity(table, row): row for row in before}
    latest = {_identity(table, row): row for row in after}
    rows = [entry for key in dict.fromkeys((*earlier, *latest))
            if (entry := _entry(context, table, earlier.get(key), latest.get(key))) is not None]
    if rows:
        conn.execute(AuditLog.__table__.insert(), rows)


@dataclass
class _Pending:
    obj: Any
    table: Any
    before: list[dict]


def _before_flush(session, _flush_context, _instances):
    session.info.pop(_PENDING, None)
    context = current_audit.get()
    if context is None:
        return
    pending = []
    for obj in session.new | session.dirty | session.deleted:
        state = inspect(obj)
        table = state.mapper.local_table
        if _fields(table) is None or (obj in session.dirty and obj not in session.deleted
                                     and not session.is_modified(obj, include_collections=False)):
            continue
        before = _read(_write_connection(session), table, _condition(table, [state.identity]), lock=True) if state.identity else []
        pending.append(_Pending(obj, table, before))
    if pending:
        session.info[_PENDING] = (context, pending)


def _after_flush(session, _flush_context):
    work = session.info.pop(_PENDING, None)
    if not work:
        return
    context, pending = work
    conn = session.connection()
    for item in pending:
        state = inspect(item.obj)
        after = _read(conn, item.table, _condition(item.table, [state.identity]), lock=True) if state.persistent else []
        _write(conn, context, item.table, item.before, after)


def _looks_unchanged(statement, table, parameters) -> bool:
    if table.name == "agent_sessions":
        relevant = {*_FIELDS[table.name][1].split(), "messages"}
    else:
        relevant = set(_FIELDS[table.name][1].split())
    assigned = {getattr(key, "key", key) for key in (getattr(statement, "_values", None) or {})}
    return not parameters and bool(assigned) and assigned.isdisjoint(relevant)


def _bulk_before(conn, table, statement, params):
    if isinstance(params, list):
        # ORM executemany 按主键更新。用参数里的实际主键缩小快照范围。
        keys = [column.key for column in table.primary_key.columns]
        if all(all(key in record for key in keys) for record in params):
            return _read(conn, table, _condition(table, [tuple(record[key] for key in keys) for record in params]), lock=True) if params else []
        return []
    return _read(conn, table, statement.whereclause, lock=True, parameters=params)


def _insert_identities(result, table, params):
    try:
        ids = result.inserted_primary_key_rows
    except (AttributeError, InvalidRequestError):
        names = [column.key for column in table.primary_key.columns]
        rows = params if isinstance(params, list) else [params or {}]
        ids = [tuple(row.get(name) for name in names) for row in rows]
    return [tuple(identity) for identity in ids if identity and all(value is not None for value in identity)]


def _execute(state: ORMExecuteState):
    context = current_audit.get()
    if context is None or not (state.is_update or state.is_delete or state.is_insert):
        return None
    statement = state.statement
    table = getattr(statement, "table", None)
    if table is None or _fields(table) is None:
        return None
    if state.is_update and _looks_unchanged(statement, table, state.parameters):
        return None
    # 快照应在待写 ORM 对象完成 flush 后产生，避免与同事务中物件写入乱序。
    if state.session.autoflush and state.execution_options.get("autoflush", True):
        state.session.flush()
    conn = _write_connection(state.session)
    before = [] if state.is_insert else _bulk_before(conn, table, statement, state.parameters)
    result = state.invoke_statement()
    keys = _insert_identities(result, table, state.parameters) if state.is_insert else [_identity(table, row) for row in before]
    # 分批核对真正写入后的数据库值（包括 SQL 表达式和服务器默认值）。
    after = []
    for start in range(0, len(keys), 200):
        after.extend(_read(conn, table, _condition(table, keys[start:start + 200]), lock=True))
    _write(conn, context, table, before, after)
    return result


def install_audit_capture() -> None:
    """同步 SQLAlchemy 回调在 AsyncSession 的同一数据库事务内工作。"""
    for name, hook in (("before_flush", _before_flush), ("after_flush_postexec", _after_flush),
                       ("do_orm_execute", _execute)):
        if not event.contains(Session, name, hook):
            event.listen(Session, name, hook)
