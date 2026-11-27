/**
 * 对话流展示辅助（纯函数 + 类型）：块折叠、工具分组摘要、运行状态映射等。
 * 无组件状态依赖，供 WorkspaceHome 与各 composable 复用。
 */
import { Circle, CircleCheck, CircleDot } from '@lucide/vue'
import {
  hasVisibleText, isDocumentReviewInput, pendingDocInput,
  type DocumentEditReviewBlock, type MsgBlock, type RunStreamMessage, type ToolBlock,
} from '@/features/run/blocks'
import type { DocumentTaskSelection } from '@/features/documents/taskModes'
import type { MessageAttachment } from './attachmentHistory'

export interface ChatMessage extends RunStreamMessage {
  role: 'user' | 'assistant'
  content: string
  /** 持久化消息 ID（历史来源卡片定位用） */
  messageId?: string
  runId?: string
  task?: DocumentTaskSelection
  files?: MessageAttachment[]
}

/** 折叠后的连续工具步骤组（完成后折叠防刷屏） */
export interface ToolGroupItem {
  kind: 'group'
  items: ToolBlock[]
}
export type DisplayItem = MsgBlock | ToolGroupItem

export interface ActiveRun {
  abort: AbortController
  stopped: boolean
  msg: ChatMessage
}

/** 右侧临时文件预览区块状态 */
export interface PreviewState {
  name: string
  path: string
  loading: boolean
  error: string
  data: import('@/api/conversations').PreviewContent | null
}

export type ReviewBlock = DocumentEditReviewBlock

export const statuses: Record<string, { label: string; tone: string }> = {
  pending: { label: '排队中', tone: 'info' },
  running: { label: '执行中', tone: 'info' },
  waiting_approval: { label: '等待审批', tone: 'warning' },
  waiting_input: { label: '等待补充', tone: 'warning' },
  waiting_document_review: { label: '等待审阅', tone: 'warning' },
  needs_reconciliation: { label: '等待处理', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已停止', tone: 'neutral' },
  suspended: { label: '已挂起', tone: 'warning' },
  limit_exceeded: { label: '已暂停', tone: 'warning' },
}

export function runState(message: ChatMessage) {
  const request = pendingDocInput(message)?.request
  if (request) return isDocumentReviewInput(request) ? statuses.waiting_document_review : statuses.waiting_input
  if (message.pendingActions?.length) return statuses.waiting_approval
  if (message.streaming && message.runStatus !== 'pending') return statuses.running
  if (message.runStatus === 'limit_exceeded') return statuses.limit_exceeded
  if (message.error && message.blocks.some(block => block.kind === 'verification' && block.state === 'incomplete')) {
    return { label: '验收未通过', tone: 'warning' }
  }
  if (message.error) return statuses.failed
  return message.runId ? statuses[message.runStatus || ''] : undefined
}

export function answerText(message: ChatMessage) {
  return message.blocks.flatMap((block) => block.kind === 'text' ? [block.text] : []).join('\n\n')
}

/** 消息来源引用区：从正文 markdown 链接提取去重后的外链来源（客观展示，不改写正文） */
export function messageSources(message: ChatMessage): Array<{ title: string; url: string }> {
  const out: Array<{ title: string; url: string }> = []
  const seen = new Set<string>()
  const linkRe = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g
  for (const block of message.blocks) {
    if (block.kind !== 'text') continue
    let match: RegExpExecArray | null
    while ((match = linkRe.exec(block.text))) {
      const url = match[2]
      if (seen.has(url)) continue
      seen.add(url)
      out.push({ title: match[1].trim() || url, url })
    }
  }
  return out
}

/** 任务耗时（对齐 Trae「任务耗时 7m 4s」） */
export function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  return rs ? `${m}m ${rs}s` : `${m}m`
}

/** 任务耗时文本：运行中实时累加，终态固化；无 run 起点则空 */
export function taskTimeText(m: ChatMessage, clock: number): string {
  if (m.runDurationMs && m.runDurationMs > 0) return formatDuration(m.runDurationMs)
  if (m.streaming && m.runStartedAt && m.runStartedAt > 0) {
    return formatDuration(Math.max(0, clock - m.runStartedAt))
  }
  return ''
}

/** 停滞秒数（长时间无新事件时提示"仍在执行"） */
export function staleSeconds(m: ChatMessage, clock: number): number {
  if (!m.streaming || !m.lastEventAt) return 0
  return Math.max(0, Math.floor((clock - m.lastEventAt) / 1000))
}

export function groupFailCount(g: ToolGroupItem): number {
  return g.items.filter((i) => i.state === 'failed').length
}

/**
 * 工具分组摘要（对齐 Trae）：按类别计数，如「已读取 3 个文件，执行 1 条命令」，
 * 比抽象的「N 个工具调用」更直观反映执行过程。
 */
export function groupSummary(g: ToolGroupItem): string {
  const read = ['read_file']
  const search = ['ls', 'glob', 'grep']
  const exec = ['execute', 'python_exec']
  const write = ['write_file', 'edit_file']
  const net = ['web_search', 'fetch_webpage', 'http_get', 'http_request']
  const del = ['delete_file', 'delete']
  const count = (names: string[]) => g.items.filter((i) => names.includes(i.name)).length
  const parts: string[] = []
  const nRead = count(read)
  const nSearch = count(search)
  const nExec = count(exec)
  const nWrite = count(write)
  const nNet = count(net)
  const nDel = count(del)
  if (nRead) parts.push(`已读取 ${nRead} 个文件`)
  if (nSearch) parts.push(`搜索 ${nSearch} 次`)
  if (nExec) parts.push(`执行 ${nExec} 条命令`)
  if (nWrite) parts.push(`已编辑 ${nWrite} 个文件`)
  if (nNet) parts.push(`网络请求 ${nNet} 次`)
  if (nDel) parts.push(`删除 ${nDel} 个文件`)
  const known = nRead + nSearch + nExec + nWrite + nNet + nDel
  if (known < g.items.length) parts.push(`${g.items.length - known} 个其他操作`)
  return parts.join('，') || `${g.items.length} 个工具调用`
}

/** 计划清单可见条目（无内容条目不渲染，防白板） */
export function planItems(todos: any[]): any[] {
  return (todos || []).filter((t) => hasVisibleText(String(t?.content ?? '')))
}

export function displayBlocks(m: ChatMessage): DisplayItem[] {
  // 空白文本块（模型偶发的纯空白/零宽增量）与空计划块不渲染，避免"白板区块"
  const blocks = m.blocks.filter((b) => {
    if (b.kind === 'text') return hasVisibleText(b.text.replace(/<!--.*?-->/gs, ''))
    if (b.kind === 'plan') return planItems(b.todos).length > 0
    return true
  })
  if (m.streaming) return blocks // 执行中逐步实时展示，不折叠
  const out: DisplayItem[] = []
  let i = 0
  while (i < blocks.length) {
    const b = blocks[i]
    if (b.kind === 'tool') {
      let j = i
      while (j < blocks.length && blocks[j].kind === 'tool') j++
      const group = blocks.slice(i, j) as ToolBlock[]
      if (group.length > 3) {
        out.push({ kind: 'group', items: group })
      } else {
        out.push(...group)
      }
      i = j
    } else {
      out.push(b)
      i++
    }
  }
  return out
}

export function planIcon(status: string) {
  if (status === 'completed') return CircleCheck
  if (status === 'in_progress') return CircleDot
  return Circle
}

export function questionPreview(text: string): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  return t.length > 80 ? `${t.slice(0, 80)}…` : t
}

/** 会话在历史列表中的状态标签 */
export function conversationStatus(
  c: { id: number; active_run_id: string | null },
  deps: {
    runningConvIds: number[]
    convMessages: Map<number, ChatMessage[]>
  },
): string {
  if (deps.runningConvIds.includes(c.id)) return '执行中'
  const cached = deps.convMessages.get(c.id)
  const request = cached?.map(pendingDocInput).find(Boolean)?.request
  if (request) return isDocumentReviewInput(request) ? '等待审阅' : '等待补充'
  if (cached?.some((message) => message.pendingActions?.length)) return '等待审批'
  if (cached?.some((message) => message.streaming)) return '恢复进度中'
  return c.active_run_id && !cached ? '有活跃任务' : ''
}
