/**
 * API v2：产品工作台契约（任务书 §7）。
 *
 * 与 v1 并存：前端工作台（默认助手/Builder/对话/Run Timeline）走 v2；
 * Run 事件流为 §7.1 新协议（tool.started / assistant.delta / run.completed ...），
 * SSE 标准 id:/event:/data: 帧，Last-Event-ID 断线续传（§8.4）。
 */
import { createRequest } from '@/utils/request'

const request2 = createRequest('/api/v2')

// ── Workspace ─────────────────────────────────────────────────────────────────

export interface Assistant {
  id: number
  name: string
  description: string
  icon: string
  system_prompt: string
  capability_ids: string[]
  tools: any[]
  model_config_id: number | null
  knowledge_base_id: number | null
  database_connection_id: number | null
  is_default: boolean
  is_active: boolean
  version?: { id: number; version_no: number; published_at: string | null }
}

export interface Conversation {
  id: number
  assistant_id: number
  title: string
  message_count: number
  active_run_id: string | null
  created_at: string | null
  updated_at: string | null
}

export interface RunInfo {
  run_id: string
  status: string
  agent_id: number
  session_id: number | null
  error: string | null
  cancel_requested: boolean
  pending_actions?: any[]
  created_at: string | null
  finished_at: string | null
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

export const workspaceApi = {
  bootstrap: () =>
    request2.get<any, {
      user: { id: number; username: string; is_superuser: boolean }
      default_assistant: Assistant
      capabilities: any[]
      workspace: Record<string, any>
    }>('/workspace/bootstrap'),
  capabilities: () => request2.get<any, { items: any[] }>('/capabilities'),
}

// ── Assistants（Builder 草稿 → 确认 → 创建）───────────────────────────────────

export const assistantApi = {
  list: (params?: any) => request2.get<any, { items: Assistant[]; total: number }>('/assistants', { params }),
  draft: (description: string, name?: string) =>
    request2.post<any, any>('/assistants/draft', { description, name }),
  create: (data: any) => request2.post<any, Assistant>('/assistants', data),
  get: (id: number) => request2.get<any, Assistant>(`/assistants/${id}`),
  patch: (id: number, data: any) => request2.patch<any, Assistant>(`/assistants/${id}`, data),
  publish: (id: number) => request2.post<any, any>(`/assistants/${id}/publish`),
}

// ── Conversations ─────────────────────────────────────────────────────────────

export const conversationApi = {
  list: (params?: any) =>
    request2.get<any, { items: Conversation[]; total: number }>('/conversations', { params }),
  create: (data: { assistant_id?: number; title?: string } = {}) =>
    request2.post<any, Conversation>('/conversations', data),
  messages: (id: number) => request2.get<any, { items: any[] }>(`/conversations/${id}/messages`),
  delete: (id: number) => request2.delete(`/conversations/${id}`),
}

// ── Runs ──────────────────────────────────────────────────────────────────────

export const runApi = {
  create: (data: { message: string; conversation_id?: number; assistant_id?: number; use_memory?: boolean }) =>
    request2.post<any, { run_id: string; conversation_id: number; status: string; events_url: string }>(
      '/runs', data,
    ),
  get: (runId: string) => request2.get<any, RunInfo>(`/runs/${runId}`),
  cancel: (runId: string, reason = '') =>
    request2.post<any, { run_id: string; status: string; message: string }>(`/runs/${runId}/cancel`, { reason }),
  resume: (runId: string, decisions: Array<{ type: string; message?: string; action_id?: string | null }>) =>
    request2.post<any, { run_id: string; status: string; message: string }>(`/runs/${runId}/resume`, { decisions }),
}

// ── Artifacts ─────────────────────────────────────────────────────────────────

export function artifactDownloadUrl(artifactId: string): string {
  return `/api/v2/artifacts/${artifactId}`
}

// ── Run 事件流消费（§7.1 + §8.4：断线重连 / Last-Event-ID 续传）───────────────

export interface RunStreamOptions {
  /** 事件回调（按到达顺序） */
  onEvent: (envelope: RunEventEnvelope) => void
  /** 流终止（终态事件到达或不可恢复错误） */
  onDone?: (reason: string) => void
  /** 可恢复断线（重连中） */
  onReconnecting?: (attempt: number) => void
  /** 起始序号（续传）；缺省从头 */
  afterSeq?: number
  signal?: AbortSignal
  maxRetries?: number
}

const TERMINAL_EVENTS = new Set([
  'run.completed', 'run.failed', 'run.cancelled', 'run.suspended',
])

function isBoundary(envelope: RunEventEnvelope): boolean {
  if (TERMINAL_EVENTS.has(envelope.event)) return true
  if (envelope.event === 'run.status_changed') {
    const s = envelope.data?.status
    return s === 'waiting_approval' || s === 'needs_reconciliation'
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

      attempt = 0 // 连接成功重置退避
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let terminated = false

      while (!terminated) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''

        for (const frame of frames) {
          const dataLine = frame.split('\n').find((l) => l.startsWith('data: '))
          if (!dataLine) continue
          try {
            const envelope = JSON.parse(dataLine.slice(6)) as RunEventEnvelope
            if (typeof envelope.sequence === 'number' && envelope.sequence > lastSeq) {
              lastSeq = envelope.sequence
            }
            options.onEvent(envelope)
            if (isBoundary(envelope)) {
              terminated = true
              options.onDone?.(envelope.event)
              return
            }
          } catch (e) {
            // §8.4 7：解析失败记录诊断信息，不中断流
            console.warn('[run-events] JSON 解析失败，已跳过:', (e as Error).message)
          }
        }
      }

      // 流被服务端正常关闭但未见终止事件（如 Run 仍在执行中轮询边界）：
      // 轻量确认状态后决定是否继续
      const info = await runApi.get(runId)
      if (isTerminalStatus(info.status)) {
        options.onDone?.(info.status)
        return
      }
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
  return ['completed', 'failed', 'cancelled', 'limit_exceeded',
    'waiting_approval', 'needs_reconciliation'].includes(status)
}

export default request2
