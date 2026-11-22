/**
 * API v2：产品工作台契约（任务书 §7）。
 *
 * 与 v1 并存：前端工作台（默认助手/Builder/对话/Run Timeline）走 v2；
 * Run 事件流为 §7.1 新协议（tool.started / assistant.delta / run.completed ...），
 * SSE 标准 id:/event:/data: 帧，Last-Event-ID 断线续传（§8.4）。
 */
import { createRequest } from '@/utils/request'
import { isPausedRunStatus, shouldReplayPastPause } from '@/features/run/runReplay'
import { PROJECT_DOWNLOAD_PATH } from '@/features/run/projectDelivery'
import type { DocumentMode, InputRequest } from './documents'

const request2 = createRequest('/api/v2')
const inlineRequest = createRequest('/api/v2', { inlineErrors: true })

export async function downloadRunArtifact(path: string, filename: string, signal?: AbortSignal, requireZip = false) {
  // 交付事件中的地址只用于本平台产物路由，访问令牌不发往动态外部地址。
  const match = PROJECT_DOWNLOAD_PATH.exec(path)
  if (!match) throw new Error('产物下载地址无效')
  const blob = await inlineRequest.get<unknown, Blob>(`/artifacts/${match[1]}`, { responseType: 'blob', signal })
  if (requireZip) {
    const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
    if (signature[0] !== 0x50 || signature[1] !== 0x4b || signature[2] !== 3 || signature[3] !== 4) {
      throw new Error('服务器返回的内容无法识别为 ZIP 文件')
    }
  }
  if (signal?.aborted) return
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const downloadProjectZip = (path: string, filename: string, signal: AbortSignal) =>
  downloadRunArtifact(path, filename, signal, true)

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
  is_pinned: boolean
  pinned_at: string | null
  message_count: number
  active_run_id: string | null
  created_at: string | null
  updated_at: string | null
}

export type ConversationMetadata = Partial<Pick<Conversation, 'title' | 'is_pinned'>>

export interface RunInfo {
  run_id: string
  status: string
  agent_id: number
  session_id: number | null
  error: string | null
  cancel_requested: boolean
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

/** 通用工作区文件预览元数据（右侧临时预览区块） */
export interface FilePreview {
  name: string
  path: string
  /** 渲染分支：image / pdf / markdown / code / text / table / office / binary */
  kind: string
  size: number
  mime: string
  /** 代码高亮语言提示 */
  language?: string
  /** 文本 / 代码 / Markdown / Office 抽取文本 */
  text?: string
  /** 表格二维数组（csv / xlsx） */
  rows?: string[][]
  truncated?: boolean
}

/** 预览内容 = 元数据 + 图片/PDF 的本地 objectURL（前端解析后附加） */
export interface PreviewContent extends FilePreview {
  objectUrl?: string
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

// ── Conversations ─────────────────────────────────────────────────────────────

export const conversationApi = {
  list: (params?: any) =>
    request2.get<any, { items: Conversation[]; total: number }>('/conversations', { params }),
  get: (id: number) => inlineRequest.get<unknown, Conversation>(`/conversations/${id}`),
  create: (data: { assistant_id?: number; title?: string } = {}) =>
    request2.post<any, Conversation>('/conversations', data),
  update: (id: number, data: ConversationMetadata) =>
    inlineRequest.patch<unknown, Conversation>(`/conversations/${id}`, data),
  messages: (id: number) => request2.get<any, { items: any[] }>(`/conversations/${id}/messages`),
  delete: (id: number) => request2.delete(`/conversations/${id}`),
  /** 上传会话附件（写入会话工作区 uploads/，二进制文档同步抽取文本副本） */
  uploadFile: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request2.post<any, { name: string; path: string; text_path: string | null; size: number }>(
      `/conversations/${id}/files`, form,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
  },
  deleteFile: (id: number, filename: string) =>
    request2.delete(`/conversations/${id}/files/${encodeURIComponent(filename)}`),
  /** 下载已上传附件（图片预览用；返回 Blob） */
  fileBlob: (id: number, filename: string) =>
    request2.get<any, Blob>(
      `/conversations/${id}/files/${encodeURIComponent(filename)}`, { responseType: 'blob' },
    ),
  /** 通用工作区文件预览元数据（path 可为 uploads/xx 或 Agent 产物相对路径） */
  filePreview: (id: number, path: string) =>
    inlineRequest.get<unknown, FilePreview>(`/conversations/${id}/file-preview`, { params: { path, layout_only: true } }),
  filePreviewPdf: (id: number, path: string, signal?: AbortSignal) =>
    inlineRequest.get<unknown, Blob>(`/conversations/${id}/file-preview-pdf`, {
      params: { path }, responseType: 'blob', signal,
    }),
  /** 通用工作区文件字节（图片 / PDF 预览、二进制下载） */
  fileBytes: (id: number, path: string) =>
    request2.get<any, Blob>(`/conversations/${id}/file-bytes`, {
      params: { path }, responseType: 'blob',
    }),
}

// ── Runs ──────────────────────────────────────────────────────────────────────

export const runApi = {
  create: (data: {
    message: string
    conversation_id?: number
    assistant_id?: number
    use_memory?: boolean
    /** 权限模块：manual=高危工具人工审批；auto=自动放行 */
    approval_mode?: 'manual' | 'auto'
    /** 手动选模型（仅本次 Run 生效；缺省 = 助手默认模型） */
    model_config_id?: number
    /** 当前会话手动启用团队协作时随本次 Run 提交 */
    team_mode?: boolean
    task_mode?: DocumentMode
    document_profile_id?: number
    document_id?: number
    task_options?: Record<string, unknown>
    /** 会话附件（上传接口返回的项） */
    attachments?: Array<{ name: string; path: string; text_path?: string | null }>
  }) =>
    request2.post<any, { run_id: string; conversation_id: number; status: string; events_url: string }>(
      '/runs', data,
    ),
  get: (runId: string) => request2.get<any, RunInfo>(`/runs/${runId}`),
  cancel: (runId: string, reason = '') =>
    request2.post<any, { run_id: string; status: string; message: string }>(`/runs/${runId}/cancel`, { reason }),
  resume: (runId: string, decisions: Array<{ type: string; message?: string; action_id?: string | null }>) =>
    request2.post<any, { run_id: string; status: string; message: string }>(`/runs/${runId}/resume`, { decisions }),
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

export default request2
