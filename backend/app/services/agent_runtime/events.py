"""
运行事件模型与旧 SSE 契约适配层。

内部统一使用结构化 RuntimeEvent；对外（AgentChat.vue / 老客户端）通过
LegacyEventAdapter 映射为旧事件流：
  thought / action / observation / final_start / final_chunk / final /
  error / verify / clarify / trace / done

映射规则：
  run_started        -> trace(run_started)
  plan / todo 变更    -> thought
  tool_started       -> action
  tool_completed     -> observation
  tool_failed        -> observation（含错误标记）
  model_delta        -> 首个 delta 前发 final_start，随后 final_chunk
  subagent_*         -> trace
  approval_required  -> 新事件 approval（旧客户端忽略），并附 thought 说明
  run_completed      -> final(全部可见模型文本拼接) + done
  run_failed         -> error + done
  run_cancelled      -> error(cancelled) + done
  run_suspended      -> done(status=suspended)（挂起边界：流必须结束，禁止悬挂等待）
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional


# ── 结构化运行事件 ────────────────────────────────────────────────────────────

RUN_STARTED = "run_started"
MODEL_DELTA = "model_delta"
TOOL_STARTED = "tool_started"
TOOL_COMPLETED = "tool_completed"
TOOL_FAILED = "tool_failed"
SUBAGENT_STARTED = "subagent_started"
SUBAGENT_COMPLETED = "subagent_completed"
APPROVAL_REQUIRED = "approval_required"
RUN_WAITING_APPROVAL = "run_waiting_approval"
RUN_WAITING_INPUT = "run_waiting_input"
STATE_CHANGED = "state_changed"
RUN_COMPLETED = "run_completed"
RUN_FAILED = "run_failed"
RUN_CANCELLED = "run_cancelled"
RUN_SUSPENDED = "run_suspended"
PLAN_UPDATED = "plan_updated"
# P4-02：Verification（§7.1 verification.started / verification.completed）
VERIFICATION_STARTED = "verification_started"
VERIFICATION_COMPLETED = "verification_completed"
# P4-03：Retry / Recovery（§7.1 run.retrying / run.recovering）
RUN_RETRYING = "run_retrying"
RUN_RECOVERING = "run_recovering"

# 流边界事件：消费者收到即结束本次事件流（Run 可能稍后由其他 Worker 恢复，
# 恢复后的执行产生新的事件流）。任务书 §12.2：SSE 消费者必须能终止。
TERMINAL_EVENTS = (
    RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_WAITING_APPROVAL,
    RUN_WAITING_INPUT, RUN_SUSPENDED,
)


@dataclass
class RuntimeEvent:
    """平台标准运行事件（最小字段对齐任务书 §7.2）。

    sequence / thread_id / agent_id / status 由 EventBus 发布时补齐。
    """

    type: str
    run_id: str
    data: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    sequence: int = 0
    thread_id: str = ""
    agent_id: Optional[int] = None
    status: str = ""
    trace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        from datetime import datetime, timezone

        return {
            "event_id": self.event_id,
            "event_type": self.type,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "timestamp": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "sequence": self.sequence,
            "status": self.status,
            "payload": self.data,
            "trace_id": self.trace_id or self.run_id,
        }


# ── 脱敏（§15.2 E：错误信息 / 工具输出 / 审批记录 / Ledger 统一脱敏）────────
# 识别规则：
# 1) 敏感 key 关键词匹配（password/pass/secret/token/api_key/authorization/
#    cookie/credential 等，词边界防 bypass/passenger 误伤）；
# 2) URL 内 user:pass@ 凭据与 token/secret/key/signature 等查询参数打码；
# 3) 自由文本中 Authorization/Bearer/API-Key 凭据值打码。
import re as _re

_SENSITIVE_KEY_RE = _re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|cookie"
    r"|credential|access[_-]?key|private[_-]?key|smtp[_-]?pass"
    r"|(^|[^a-z0-9])pass([^a-z0-9]|$))",
    _re.IGNORECASE,
)
_URL_USERINFO_RE = _re.compile(r"(://[^/\s:@]+:)([^@/\s]+)(@)")
_URL_SECRET_PARAM_RE = _re.compile(
    r"([?&][^=\s&]*?(?:token|secret|signature|password|passwd|credential"
    r"|apikey|api_key|access_key|private_key|auth)[^=\s&]*=)([^&\s]*)",
    _re.IGNORECASE,
)
_TEXT_AUTH_RE = _re.compile(
    r"((?:authorization|api[-_]?key)\s*[:=]\s*)(?:bearer\s+)?(\S+)", _re.IGNORECASE
)
_TEXT_BEARER_RE = _re.compile(r"(\bbearer\s+)(\S+)", _re.IGNORECASE)


def _mask_text(text: str) -> str:
    """自由文本打码：URL 凭据 / 查询参数 token / Bearer 值（不截断）。"""
    text = _URL_USERINFO_RE.sub(r"\1***\3", text)
    text = _URL_SECRET_PARAM_RE.sub(r"\1***", text)
    text = _TEXT_AUTH_RE.sub(r"\1***", text)
    text = _TEXT_BEARER_RE.sub(r"\1***", text)
    return text


def _redact(value: Any, max_len: Optional[int], max_items: Optional[int] = 20) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                out[k] = "***"
            else:
                out[k] = _redact(v, max_len, max_items)
        return out
    if isinstance(value, list):
        return [_redact(v, max_len, max_items) for v in value[:max_items]]
    if isinstance(value, str):
        text = _mask_text(value)
        if max_len is not None and len(text) > max_len:
            return text[:max_len] + f"...[truncated {len(text) - max_len} chars]"
        return text
    return value


def redact_secrets(value: Any) -> Any:
    """统一脱敏（仅打码、不截断）：工具输入证据 / 工具输出 / 错误信息 / Ledger 写入使用。"""
    return _redact(value, max_len=None)


def redact_sensitive(value: Any, max_len: int = 400) -> Any:
    """对外事件脱敏：截断长参数并遮蔽疑似密钥字段。"""
    return _redact(value, max_len=max_len)


# 持久化截断策略：文本型事件（模型增量/最终结果）与工具调用证据（工具 args /
# 输出：write_file 写入内容、python_exec 代码等）按大上限入库——400 字符截断
# 会破坏最终文本重建，并让事件日志里的执行证据失真（评测交付物漏采、约束假
# 失败）；其余事件（计划/审批/状态类）仍 400 上限防事件表膨胀。密钥遮蔽一律保留。
_FULL_PAYLOAD_EVENTS = frozenset(
    {MODEL_DELTA, RUN_COMPLETED, TOOL_STARTED, TOOL_COMPLETED, TOOL_FAILED})
_FULL_PAYLOAD_MAX_LEN = 100_000


def _persist_max_len(ev_type: str) -> int:
    """按事件类型取持久化截断上限（仅长文本上限不同，脱敏遮蔽一律保留）。"""
    return _FULL_PAYLOAD_MAX_LEN if ev_type in _FULL_PAYLOAD_EVENTS else 400


# ── 旧 SSE 契约适配 ──────────────────────────────────────────────────────────

class LegacyEventAdapter:
    """把 RuntimeEvent 流映射为旧 SSE 扁平事件 dict（前端契约：type/content/tool/input）。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.final_text_parts: List[str] = []
        self.final_started = False
        self.finished = False
        self.seq = 0  # 事件序号：SSE 客户端可据此检测丢事件/乱序

    def adapt(self, ev: RuntimeEvent) -> List[Dict[str, Any]]:
        """返回 0..n 个旧事件 dict，每个事件带自增 seq 序号。"""
        out = self._adapt(ev)
        for item in out:
            item["seq"] = self.seq
            self.seq += 1
        return out

    def _adapt(self, ev: RuntimeEvent) -> List[Dict[str, Any]]:
        """返回 0..n 个旧事件 dict。"""
        if self.finished:
            return []
        t = ev.type
        if t == RUN_STARTED:
            return []

        if t == PLAN_UPDATED:
            todos = ev.data.get("todos") or []
            lines = []
            for item in todos:
                status = item.get("status", "pending")
                mark = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(status, "[ ]")
                lines.append(f"{mark} {item.get('content', '')}")
            if lines:
                return [{"type": "thought", "content": "任务计划：\n" + "\n".join(lines)}]
            return []

        if t == TOOL_STARTED:
            out = self._ensure_final_start()
            out.append({
                "type": "action",
                "tool": ev.data.get("tool"),
                "input": redact_sensitive(ev.data.get("input")),
            })
            return out

        if t == TOOL_COMPLETED:
            return [{
                "type": "observation",
                "tool": ev.data.get("tool"),
                "content": redact_sensitive(ev.data.get("output"), max_len=1500),
            }]

        if t == TOOL_FAILED:
            return [{
                "type": "observation",
                "tool": ev.data.get("tool"),
                "content": f"工具执行失败: {ev.data.get('error')}",
                "error": True,
            }]

        if t == MODEL_DELTA:
            text = ev.data.get("text") or ""
            if not text:
                return []
            self.final_text_parts.append(text)
            out = self._ensure_final_start()
            out.append({"type": "final_chunk", "content": text})
            return out

        if t in (SUBAGENT_STARTED, SUBAGENT_COMPLETED):
            return [{
                "type": "thought",
                "content": f"子代理 {ev.data.get('name')} {'开始执行' if t == SUBAGENT_STARTED else '执行完成'}",
            }]

        if t == APPROVAL_REQUIRED:
            tools = [a.get("tool") for a in (ev.data.get("actions") or [])]
            return [{
                "type": "thought",
                "content": f"⏸ 等待人工审批：{', '.join(map(str, tools))}",
                "approval": {"run_id": self.run_id, "actions": redact_sensitive(ev.data.get("actions"))},
            }]

        if t == STATE_CHANGED:
            return []

        if t in (RUN_WAITING_APPROVAL, RUN_WAITING_INPUT):
            out = self._ensure_final_start()
            status = "waiting_input" if t == RUN_WAITING_INPUT else "waiting_approval"
            out.append({"type": "done", "status": status, "run_id": self.run_id})
            self.finished = True
            return out

        if t == RUN_COMPLETED:
            final_text = ev.data.get("final_text")
            if final_text is None:
                final_text = "".join(self.final_text_parts)
            out = self._ensure_final_start()
            out.append({"type": "final", "content": final_text})
            out.append({"type": "done", "status": "completed"})
            self.finished = True
            return out

        if t == RUN_FAILED:
            out = self._ensure_final_start()
            out.append({"type": "error", "content": ev.data.get("error") or "运行失败"})
            out.append({"type": "done", "status": ev.data.get("status", "failed")})
            self.finished = True
            return out

        if t == RUN_CANCELLED:
            out = self._ensure_final_start()
            out.append({"type": "error", "content": "运行已取消"})
            out.append({"type": "done", "status": "cancelled"})
            self.finished = True
            return out

        if t == RUN_SUSPENDED:
            # 挂起边界（租约丢失/优雅停机/驱动异常结束）：流必须结束，
            # 消费者可按 run_id 重新订阅或走恢复流程，禁止无限等待
            out = self._ensure_final_start()
            out.append({
                "type": "done",
                "status": "suspended",
                "run_id": self.run_id,
                "reason": ev.data.get("reason"),
            })
            self.finished = True
            return out

        return []

    def _ensure_final_start(self) -> List[Dict[str, Any]]:
        if self.final_started:
            return []
        self.final_started = True
        return [{"type": "final_start"}]


def sse_format(data: Dict[str, Any]) -> str:
    """渲染为旧前端契约的 SSE wire 格式（data: {json} 行）。"""
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ── SSE 消费者唤醒（事件持久化后立即下发，消除轮询间隔延迟）─────────────────
# 内存通知只对同进程（API + RunWorker 同事件循环）的 SSE 消费者生效；
# 跨进程场景退化为原有轮询兜底，正确性不受影响。
_WAKEUP_WAITERS: Dict[str, List[Any]] = {}


def notify_wakeup(run_id: str) -> None:
    """事件持久化后唤醒该 run 的 SSE 轮询消费者（无等待者时为空操作）。"""
    for waiter in tuple(_WAKEUP_WAITERS.get(run_id, ())):
        waiter.set()


async def wait_wakeup(run_id: str, timeout: float) -> None:
    """等待 run 新事件通知；超时返回（轮询兜底，跨进程仍正确）。"""
    import asyncio

    ev = asyncio.Event()
    waiters = _WAKEUP_WAITERS.setdefault(run_id, [])
    waiters.append(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            waiters.remove(ev)
        except ValueError:
            pass
        if not waiters:
            _WAKEUP_WAITERS.pop(run_id, None)


class EventBus:
    """run 级事件总线：异步队列 + 订阅广播（当前单订阅者场景直接出队）。

    发布时为事件补齐 §7.2 最小字段：sequence（单调解号）、thread_id、
    agent_id、status（当前 run 状态）、trace_id。

    Phase 5（FR-07）持久化接线：persist=True 的 run 级总线在发布时先把
    事件写入 agent_run_events（数据库是事实来源，sequence 由
    agent_runs.last_event_seq 原子自增分配，payload 入库前脱敏），
    成功后才通过内存队列唤醒本地 SSE 消费者 —— 内存队列只是通知优化，
    溢出时仅丢通知、绝不丢持久化事件；事件写入失败显式抛出
    PersistenceError，不得静默当作成功。

    Phase 2.1 §14.5（P1-06）：MODEL_DELTA 批量持久化 —— 高频 Token 增量
    不得每条一个数据库事务。增量先入内存缓冲并**立即通知**本地消费者
    （live SSE 仍逐 Token），达到 ``delta_batch_size`` 或遇到非增量事件边界
    时**一次事务**批量写入 DB（DB 仍是事实来源，序号在写入时分配）。批量
    持久化不合并事件行（每 Token 仍是一条 agent_run_events），因此事件条数、
    回放顺序与最终文本重建（_load_prior_final_text）保持完整一致。
    """

    def __init__(self, maxsize: int = 1000, *, persist: bool = False,
                 delta_batch_size: int = 32, delta_flush_interval: Optional[float] = None):
        import asyncio

        self._queue: "asyncio.Queue[RuntimeEvent]" = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self._seq = 0
        self.persist = persist
        self.thread_id = ""
        self.agent_id: Optional[int] = None
        self.status = ""
        self.trace_id = ""
        # MODEL_DELTA 批量缓冲（P1-06 §14.5）：攒够 delta_batch_size 或事件边界一次事务写入
        self._delta_buffer: List[RuntimeEvent] = []
        self.delta_batch_size = max(1, int(delta_batch_size))
        # 时间兜底（流式体验）：SSE 事实来源是 DB，增量落库不能只按条数攒批
        # （32 个 token ≈ 1s 一坨），超过 delta_flush_interval 秒即刷，保证前端逐段流出
        if delta_flush_interval is None:
            from app.core.config import settings

            delta_flush_interval = getattr(settings, "AGENT_DELTA_FLUSH_INTERVAL", 0.2)
        self.delta_flush_interval = max(0.0, float(delta_flush_interval))
        self._delta_first_ts: float = 0.0

    def bind_run(self, *, thread_id: str, agent_id: Optional[int], trace_id: str = "") -> None:
        """绑定 run 级上下文（由 RunHandle 构造时调用）。"""
        self.thread_id = thread_id
        self.agent_id = agent_id
        self.trace_id = trace_id

    @staticmethod
    async def persist_in_transaction(ev: RuntimeEvent, db) -> int:
        """持久化与暂停或恢复状态必须原子提交的事件。"""
        from .repositories import EventRepository

        # 输入 schema 属于前端协议字段，递归脱敏会改变属性名和枚举。
        # 提交值仅持久化至请求记录，不进入任何事件。
        if ev.type == "input.required":
            payload = {**ev.data, "prompt": redact_sensitive(ev.data.get("prompt", ""), 4000)}
        elif ev.type == "document.profile_draft.created":
            # 规范草稿须可完整编辑与确认，保留 Schema 允许的章节与说明。
            payload = _redact(ev.data, max_len=None, max_items=None)
        elif ev.type in {"document.edit.review_required", "document.edit.reviewed"}:
            # 全部操作锚点需与等待输入的枚举一致，正文由鉴权后的审阅接口读取。
            payload = _redact(ev.data, max_len=400, max_items=None)
        else:
            payload = redact_sensitive(ev.data or {}, max_len=_persist_max_len(ev.type))
        return await EventRepository(db).append(
            ev.run_id, ev.type, payload, thread_id=ev.thread_id or ""
        )

    @staticmethod
    async def _persist(ev: RuntimeEvent) -> int:
        """持久化事件并返回分配的 run 内序号（失败向上抛出 PersistenceError）。"""
        from app.core.database import AsyncSessionLocal
        from .repositories import PersistenceError

        try:
            async with AsyncSessionLocal() as db:
                seq = await EventBus.persist_in_transaction(ev, db)
                await db.commit()
            return seq
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(f"run {ev.run_id} 事件持久化失败: {e}") from e

    async def publish(self, ev: RuntimeEvent) -> None:
        if self._closed:
            return
        ev.thread_id = ev.thread_id or self.thread_id
        ev.agent_id = ev.agent_id if ev.agent_id is not None else self.agent_id
        ev.status = ev.status or self.status
        ev.trace_id = ev.trace_id or self.trace_id
        # P1-05：工具事件统一计数（覆盖 adapter/内部/子代理所有工具来源）；
        # 只按终态计数（completed/failed），指标故障绝不影响事件发布
        if ev.type in (TOOL_COMPLETED, TOOL_FAILED):
            try:
                from app.observability import metrics

                metrics.observe_tool(
                    str((ev.data or {}).get("tool") or "unknown"),
                    "completed" if ev.type == TOOL_COMPLETED else "failed",
                )
            except Exception:  # noqa: BLE001
                pass
        if self.persist and ev.run_id:
            if ev.type == MODEL_DELTA:
                # P1-06 §14.5：MODEL_DELTA 批量持久化（不每 Token 一个事务）。
                # 先入缓冲并立即通知本地消费者（live SSE 逐 Token），攒够批量
                # （条数上限或 AGENT_DELTA_FLUSH_INTERVAL 时间上限）或遇到非增量
                # 事件边界时一次事务写入 DB（序号写入时分配）。
                self._delta_buffer.append(ev)
                if self._delta_first_ts == 0.0:
                    self._delta_first_ts = time.monotonic()
                if (len(self._delta_buffer) >= self.delta_batch_size
                        or time.monotonic() - self._delta_first_ts >= self.delta_flush_interval):
                    await self.flush_deltas()
                self._notify(ev)
                return
            # 非增量事件：先刷缓冲的 MODEL_DELTA（保持持久化顺序），再写本事件
            await self.flush_deltas()
            ev.sequence = await self._persist(ev)
        else:
            self._seq += 1
            ev.sequence = self._seq
        self._notify(ev)

    def notify_persisted(self, ev: RuntimeEvent) -> None:
        """提交跨表事务后唤醒消费者。"""
        self._notify(ev)

    def _notify(self, ev: RuntimeEvent) -> None:
        """通知本地 SSE 消费者（内存队列仅是通知优化，溢出只丢通知不丢持久化事件）。"""
        try:
            self._queue.put_nowait(ev)
        except Exception:
            # 队列满时丢弃最旧事件，保证执行不被观测阻塞（持久化事件仍在 DB）
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(ev)
            except Exception:
                pass
        # 同进程 SSE 消费者立即醒来取事件（消除轮询间隔延迟）
        if ev.run_id:
            notify_wakeup(ev.run_id)

    async def flush_deltas(self) -> None:
        """把缓冲的 MODEL_DELTA 一次事务持久化（P1-06 §14.5：单 commit = 单事务）。

        批量写入不合并事件行——每条 MODEL_DELTA 仍是一条 agent_run_events，
        仅事务数按批聚合，从而高频 Token 输出不再产生每 Token 一个事务。
        """
        if not self._delta_buffer:
            return
        buf = self._delta_buffer
        self._delta_buffer = []
        self._delta_first_ts = 0.0
        seqs = await self._persist_many(buf)
        for ev, seq in zip(buf, seqs):
            ev.sequence = seq
        if buf and buf[0].run_id:
            notify_wakeup(buf[0].run_id)

    @staticmethod
    async def _persist_many(events: List[RuntimeEvent]) -> List[int]:
        """一次事务批量持久化多条事件，返回各自分配的 sequence。

        与单条 ``_persist`` 语义一致：失败向上抛出 PersistenceError，不静默成功。
        """
        from app.core.database import AsyncSessionLocal
        from .repositories import EventRepository, PersistenceError

        try:
            async with AsyncSessionLocal() as db:
                repo = EventRepository(db)
                seqs: List[int] = []
                for ev in events:
                    # 与 _persist 相同的脱敏规则：文本/工具证据不截断，保证最终文本重建与执行证据完整
                    max_len = _persist_max_len(ev.type)
                    payload = redact_sensitive(ev.data or {}, max_len=max_len)
                    seq = await repo.append(
                        ev.run_id, ev.type, payload, thread_id=ev.thread_id or "",
                    )
                    seqs.append(seq)
                await db.commit()
            return seqs
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(f"MODEL_DELTA 批量事件持久化失败: {e}") from e

    async def events(self) -> AsyncGenerator[RuntimeEvent, None]:
        while True:
            ev = await self._queue.get()
            yield ev
            if ev.type in TERMINAL_EVENTS:
                break

    async def get(self, timeout: Optional[float] = None) -> Optional[RuntimeEvent]:
        """取下一个事件；指定 timeout 时超时返回 None。

        供消费端在无事件间隙做"生产侧是否已结束"的判定，避免无限悬挂等待。
        被取消/超时的 getter 不会丢失队列中的事件（asyncio.Queue 语义保证）。
        """
        import asyncio

        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def empty(self) -> bool:
        return self._queue.empty()

    def close(self) -> None:
        self._closed = True
