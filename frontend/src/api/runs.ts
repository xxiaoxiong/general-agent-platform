/**
 * Run API（/api/v2/runs）+ Run 事件流消费（§7.1 + §8.4：断线重连 / Last-Event-ID 续传）。
 *
 * 事件流为 v2 协议（tool.started / assistant.delta / run.completed ...），
 * SSE 标准 id:/event:/data: 帧；停止订阅与取消分离（取消必须显式调用 cancel）。
 */
import { createRequest } from '@/utils/request'
import { isPausedRunStatus, shouldReplayPastPause } from '@/features/run/runReplay'
import type { DocumentMode, InputRequest } from './documents'

const request = createRequest('/api/v2')
const inlineRequest = createRequest('/api/v2', { inlineErrors: true })

export interface RunInfo {
  run_id: string
  status: string
  agent_id: number
  session_id: number | null
  error: string | null
  cancel_requested: boolean
  /** 目标模式（不限调用次数/执行时长，执行中可补充要求） */
  goal_mode?: boolean
  pending_actions?: any[]
  pending_input?: InputRequest | null
  last_event_seq?: number
  document_retryable?: boolean
  created_at: string | null
  finished_at: string | null
  /** Langfuse Trace 关联（未配置 Langfuse 时缺省，前端隐藏入口） */
  langfuse_trace_id?: string | null
  langfuse_trace_url?: string | null
}

/** v2 事件信封（§7.1） */
export interface RunEventEnvelope {
  event: string
  run_id: string
  sequence: number
  timestamp: string
  trace_id: string
  data: Record<string, any>
}

export const runApi = {
  create: (data: {
    message: string
    conversation_id?: number
    assistant_id?: number
    use_memory?: boolean
    /** 权限模块：manual=高危工具人工审批；auto=自动放行 */
    approval_mode?: 'manual' | 'auto'
    /** 联网检索开关：false=本次 Run 禁用网络检索工具（缺省跟随助手能力包） */
    web_enabled?: boolean
    /** 历史会话检索开关：false=本次 Run 禁用自动预取与历史检索工具（缺省跟随用户设置） */
    history_enabled?: boolean
    /** 手动选模型（仅本次 Run 生效；缺省 = 助手默认模型） */
    model_config_id?: number
    /** 当前会话手动启用团队协作时随本次 Run 提交 */
    team_mode?: boolean
    /** 目标模式：不限模型/工具调用与执行时长，执行中可补充要求 */
    goal_mode?: boolean
    task_mode?: DocumentMode
    document_profile_id?: number
    document_id?: number
    /** 右侧预览文档：仅作为意图分诊提示，不强制绑定为本轮编辑目标 */
    preview_document_id?: number
    preview_document_version_no?: number
    task_options?: Record<string, unknown>
    /** 会话附件（上传接口返回的项） */
    attachments?: Array<{ name: string; path: string; text_path?: string | null }>
  }) =>
    request.post<any, {
      run_id: string; conversation_id: number; status: string; events_url: string; goal_mode?: boolean
    }>('/runs', data),
  get: (runId: string) => request.get<any, RunInfo>(`/runs/${runId}`),
  cancel: (runId: string, reason = '') =>
    request.post<any, { run_id: string; status: string; message: string }>(`/runs/${runId}/cancel`, { reason }),
  /** 目标模式执行中补充要求（不新起 Run，下一次模型调用前生效） */
  steer: (runId: string, message: string) =>
    request.post<any, { run_id: string; status: string; message_id: string; events_url: string }>(
      `/runs/${runId}/steer`, { message },
    ),
  resume: (runId: string, decisions: Array<{ type: string; message?: string; action_id?: string | null }>) =>
    request.post<any, { run_id: string; status: string; message: string }>(`/runs/${runId}/resume`, { decisions }),
  submitInput: (runId: string, requestId: string, values: Record<string, unknown>) =>
    inlineRequest.post<unknown, { run_id: string; status: string }>(`/runs/${runId}/input`, { request_id: requestId, values }),
  retryDocument: (runId: string, lastSequence?: number) =>
    inlineRequest.post<unknown, { run_id: string; status: string; after_seq: number }>(
      `/runs/${runId}/retry`, undefined,
      { headers: lastSequence ? { 'X-Document-Failure-Seq': String(lastSequence) } : undefined },
    ),
  repairProject: (runId: string, modelId?: number, key?: string) =>
    inlineRequest.post<unknown, { run_id: string; conversation_id: number; status: string; user_message: string }>(
      `/runs/${runId}/project-repair`, { model_config_id: modelId },
      { headers: key ? { 'Idempotency-Key': key } : undefined },
    ),
}

// ── Run 事件流消费 ───────────────────────────────────────────────────────────

export interface RunStreamOptions {
  /** 事件回调（按到达顺序） */
  onEvent: (envelope: RunEventEnvelope) => void
  /** 流终止（终态事件到达或不可恢复错误） */
  onDone?: (reason: string) => void
  /** 可恢复断线（重连中） */
  onReconnecting?: (attempt: number) => void
  /** 起始序号（续传）；缺省从头 */
  afterSeq?: number
  /** 历史重建跨过已恢复的暂停，从相同 Run 继续读取。 */
  replayPastPauses?: boolean
  signal?: AbortSignal
  maxRetries?: number
}

const TERMINAL_EVENTS = new Set([
  'run.completed', 'run.failed', 'run.cancelled', 'run.suspended',
])

function isBoundary(envelope: RunEventEnvelope): boolean {
  if (envelope.data?.historical) return false
  if (TERMINAL_EVENTS.has(envelope.event)) return true
  if (envelope.event === 'run.status_changed') {
    const s = envelope.data?.status
    return isPausedRunStatus(s)
  }
  return false
}

/**
 * 消费 Run 事件流：SSE 解析 + 断线自动重连（携带 Last-Event-ID 续传）。
 *
 * §8.4：JSON 解析失败记录诊断信息并跳过（不中断流）；重连按指数退避，
 * 达到 maxRetries 或收到终止事件后停止。
 */
export async function consumeRunEvents(runId: string, options: RunStreamOptions): Promise<void> {
  const maxRetries = options.maxRetries ?? 5
  let lastSeq = options.afterSeq ?? 0
  let attempt = 0

  while (true) {
    if (options.signal?.aborted) return
    try {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${localStorage.getItem('access_token')}`,
      }
      if (lastSeq > 0) headers['Last-Event-ID'] = String(lastSeq)

      const response = await fetch(`/api/v2/runs/${runId}/events`, {
        headers,
        signal: options.signal,
      })
      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let boundary: RunEventEnvelope | undefined
      try {
        while (!boundary) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n')
          buffer = frames.pop() || ''

          for (const frame of frames) {
            const dataLine = frame.split('\n').find((l) => l.startsWith('data: '))
            if (!dataLine) continue
            let envelope: RunEventEnvelope
            try {
              envelope = JSON.parse(dataLine.slice(6)) as RunEventEnvelope
            } catch (e) {
              // §8.4 7：解析失败记录诊断信息，不中断流。
              console.warn('[run-events] JSON 解析失败，已跳过:', (e as Error).message)
              continue
            }
            if (envelope.sequence > 0 && envelope.sequence <= lastSeq) continue
            if (envelope.sequence > lastSeq) lastSeq = envelope.sequence
            attempt = 0
            options.onEvent(envelope)
            if (!isBoundary(envelope)) continue
            boundary = envelope
            break
          }
        }
      } finally {
        await reader.cancel().catch(() => {})
        reader.releaseLock()
      }

      if (boundary) {
        const pause = boundary.event === 'run.status_changed' ? boundary.data.status : undefined
        if (options.replayPastPauses && isPausedRunStatus(pause)) {
          const current = await runApi.get(runId)
          if (shouldReplayPastPause(pause, lastSeq, current.status, current.last_event_seq)) continue
        }
        options.onDone?.(boundary.event)
        return
      }

      // 流被服务端正常关闭但未见终止事件（如 Run 仍在执行中轮询边界）：
      // 轻量确认状态后决定是否继续
      const info = await runApi.get(runId)
      if (isTerminalStatus(info.status)) {
        options.onDone?.(info.status)
        return
      }
      throw new Error('事件流提前关闭')
    } catch (e) {
      if (options.signal?.aborted) return
      attempt += 1
      if (attempt > maxRetries) {
        options.onDone?.(`reconnect_failed: ${(e as Error).message}`)
        return
      }
      options.onReconnecting?.(attempt)
      await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** (attempt - 1), 8000)))
    }
  }
}

export function isTerminalStatus(status: string): boolean {
  return ['completed', 'failed', 'cancelled', 'suspended', 'limit_exceeded'].includes(status) || isPausedRunStatus(status)
}
