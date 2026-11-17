/**
 * 对话流块模型（流式执行体验）：
 * 助手消息由「文本段 / 工具步骤 / 计划」按事件到达顺序组成的块流，
 * 工具小步骤实时追加在对话流尾部（穿插在文本之间），而非聚堆在消息顶部。
 */
import type { RunEventEnvelope } from '@/api/v2'

export interface TextBlock {
  kind: 'text'
  text: string
}

export interface PlanBlock {
  kind: 'plan'
  todos: any[]
}

export interface ToolBlock {
  kind: 'tool'
  key: string
  name: string
  target: string
  state: 'running' | 'done' | 'failed'
  input?: any
  output?: any
  error?: string
  startedAt?: number
  durationMs?: number
}

export type MsgBlock = TextBlock | ToolBlock | PlanBlock

/** 携带块流的运行消息（工具步骤实时穿插展示的最小状态） */
export interface RunStreamMessage {
  blocks: MsgBlock[]
  streaming?: boolean
  /** 最近活动类型：打字光标仅在 'text'（正在生成文本）时出现 */
  lastActivity?: 'text' | 'tool' | 'other'
  lastEventAt?: number
  lastSeq?: number
  liveStatus?: string
  error?: string
  runStatus?: string
  pendingActions?: any[]
  finalText?: string
}

/** 从工具入参提取目标摘要（文件路径/搜索模式/URL 等），让步骤行有意义 */
export function extractTarget(name: string, input: any): string {
  if (!input || typeof input !== 'object') return ''
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      const v = input[k]
      if (typeof v === 'string' && v.trim()) return v.trim()
    }
    return ''
  }
  const raw = pick('path', 'file_path', 'pattern', 'query', 'url', 'expression', 'text', 'content')
  if (!raw) return ''
  const oneLine = raw.replace(/\s+/g, ' ')
  return oneLine.length > 48 ? oneLine.slice(0, 48) + '…' : oneLine
}

// 空白 + 零宽/不可见字符（ZWSP/BOM/软连字符等）：模型偶发输出这类字符，
// trim() 判不掉，会渲染成"空气泡/白板块"
const INVISIBLE_RE = new RegExp('[\\s\\u00ad\\u034f\\u180e\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e\\u2060-\\u2064\\u2066-\\u206f\\ufeff]+', 'g')

/** 是否含可见内容（防空气泡/空白块） */
export function hasVisibleText(text: string): boolean {
  return !!text && text.replace(INVISIBLE_RE, '') !== ''
}

function findToolBlock(blocks: MsgBlock[], key: string): ToolBlock | undefined {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b.kind === 'tool' && (!key || b.key === key)) return b
  }
  return undefined
}

function joinedText(msg: RunStreamMessage): string {
  return msg.blocks
    .filter((b): b is TextBlock => b.kind === 'text')
    .map((b) => b.text)
    .join('')
}

/** 权威最终文本补齐：delta 缺尾/缺失时用 final_text 兜底（不重复已有文本） */
function gapFillText(msg: RunStreamMessage, finalText: string) {
  const ft = String(finalText || '')
  if (!ft) return
  const joined = joinedText(msg)
  if (!joined.trim()) {
    msg.blocks.push({ kind: 'text', text: ft })
  } else if (ft.length > joined.length && ft.startsWith(joined)) {
    msg.blocks.push({ kind: 'text', text: ft.slice(joined.length) })
  }
}

/** v2 事件 -> 块流状态（实时与历史回放共用同一归约器） */
export function applyRunEvent(msg: RunStreamMessage, ev: RunEventEnvelope): void {
  msg.lastEventAt = Date.now()
  if (typeof ev.sequence === 'number' && ev.sequence > (msg.lastSeq || 0)) {
    msg.lastSeq = ev.sequence
  }
  const blocks = msg.blocks
  const data = ev.data || {}

  switch (ev.event) {
    case 'run.started':
      msg.runStatus = 'running'
      msg.liveStatus = '任务已开始…'
      msg.lastActivity = 'other'
      break

    case 'plan.updated': {
      const todos = (data.todos as any[]) || []
      // 空计划（无任何可见条目）不落块，避免出现"白板区块"
      if (!todos.some((t) => hasVisibleText(String(t?.content ?? '')))) break
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'plan') last.todos = todos
      else blocks.push({ kind: 'plan', todos })
      msg.liveStatus = '已更新任务计划…'
      msg.lastActivity = 'other'
      break
    }

    case 'tool.started':
      blocks.push({
        kind: 'tool',
        key: String(data.tool_call_id || `t-${ev.sequence}`),
        name: String(data.tool_name || '工具'),
        target: extractTarget(String(data.tool_name || ''), data.input),
        state: 'running',
        input: data.input,
        startedAt: Date.parse(ev.timestamp) || Date.now(),
      })
      msg.liveStatus = `正在执行 ${data.tool_name || '工具'}…`
      msg.lastActivity = 'tool'
      break

    case 'tool.completed':
    case 'tool.failed': {
      const key = String(data.tool_call_id || '')
      const failed = ev.event === 'tool.failed'
      let blk = findToolBlock(blocks, key)
      if (!blk || blk.state !== 'running') {
        if (blk && key) {
          // 同 call id 重放：直接落新行，不覆盖已有结果
          blk = undefined
        }
      }
      if (!blk) {
        // tool.started 缺失（护栏拒绝/内部桥）：补一行，直接呈现终态
        blk = {
          kind: 'tool',
          key: key || `t-${ev.sequence}`,
          name: String(data.tool_name || '工具'),
          target: extractTarget(String(data.tool_name || ''), data.input),
          state: failed ? 'failed' : 'done',
          input: data.input,
          startedAt: Date.parse(ev.timestamp) || undefined,
        }
        blocks.push(blk)
      } else {
        blk.state = failed ? 'failed' : 'done'
      }
      if (failed) blk.error = String(data.error || '')
      else blk.output = data.output
      if (blk.startedAt) {
        const end = Date.parse(ev.timestamp) || Date.now()
        if (end >= blk.startedAt) blk.durationMs = end - blk.startedAt
      }
      msg.liveStatus = failed
        ? `${blk.name} 执行失败，继续执行中…`
        : `已完成 ${blk.name}，继续执行中…`
      msg.lastActivity = 'tool'
      break
    }

    case 'assistant.delta': {
      const text = String(data.text || '')
      if (!text) break
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'text') last.text += text
      // 无可见内容的增量（纯空白/零宽字符）不新开文本块（避免空气泡白板）
      else if (hasVisibleText(text)) blocks.push({ kind: 'text', text })
      msg.lastActivity = 'text'
      msg.liveStatus = '正在生成回答…'
      break
    }

    case 'approval.required':
      msg.pendingActions = (data.actions as any[]) || []
      msg.liveStatus = '等待人工审批…'
      msg.lastActivity = 'other'
      break

    case 'run.status_changed':
      msg.runStatus = String(data.status || msg.runStatus || '')
      if (data.actions !== undefined) msg.pendingActions = (data.actions as any[]) || []
      break

    case 'run.retrying': {
      // 断点重试：撤回上次未完成的半截文本（keep_chars 之后的字符全部丢弃）
      const keep = Number(data.keep_chars)
      if (Number.isFinite(keep) && keep >= 0) {
        let need = Math.max(0, joinedText(msg).length - keep)
        for (let i = blocks.length - 1; i >= 0 && need > 0; i--) {
          const b = blocks[i]
          if (b.kind !== 'text') break
          const take = Math.min(need, b.text.length)
          b.text = b.text.slice(0, b.text.length - take)
          need -= take
          if (!b.text) blocks.splice(i, 1)
        }
      }
      msg.liveStatus = '模型响应中断，正在自动重试…'
      msg.lastActivity = 'other'
      break
    }

    case 'run.completed':
      msg.finalText = String(data.final_text || '')
      gapFillText(msg, msg.finalText)
      msg.runStatus = 'completed'
      break

    case 'run.failed':
      msg.error = String(data.error || '运行失败')
      msg.runStatus = 'failed'
      break

    case 'run.cancelled':
      msg.runStatus = 'cancelled'
      break

    case 'run.suspended':
      msg.runStatus = 'suspended'
      break
  }
}

/**
 * 终结流式消息：状态收尾 + 文本兜底（final_text / 历史 content / 占位说明），
 * 保证绝不留下空气泡或假"执行中"状态。
 */
export function finalizeRunMessage(
  msg: RunStreamMessage,
  reason: string,
  fallbackText = '',
): void {
  msg.streaming = false
  msg.liveStatus = ''
  const map: Record<string, string> = {
    'run.completed': 'completed', 'run.failed': 'failed',
    'run.cancelled': 'cancelled', 'run.suspended': 'suspended',
    'run.status_changed': msg.runStatus || 'completed',
  }
  msg.runStatus = reason.startsWith('reconnect_failed')
    ? (msg.runStatus || 'failed')
    : (map[reason] || reason)
  if (fallbackText) gapFillText(msg, fallbackText)
  if (!joinedText(msg).trim()) {
    // 占位文本只在真正收尾时出现；待审批/挂起还有后续执行，不能宣告"已完成"
    if (msg.runStatus === 'cancelled') msg.blocks.push({ kind: 'text', text: '（已停止生成）' })
    else if (msg.runStatus === 'completed') msg.blocks.push({ kind: 'text', text: '（任务已完成，本次未生成文本回答）' })
  }
  // 终态收口：被中断/未回执的工具行不再永远"执行中"（待审批时保留，后续还会执行）
  if (['completed', 'failed', 'cancelled', 'suspended'].includes(msg.runStatus || '')) {
    for (const b of msg.blocks) {
      if (b.kind === 'tool' && b.state === 'running') {
        b.state = 'failed'
        if (!b.error) b.error = '（未收到执行结果，可能被中断）'
      }
    }
  }
  if (reason.startsWith('reconnect_failed')) {
    msg.error = `连接中断且重连失败：${reason.replace('reconnect_failed: ', '')}。可刷新页面恢复任务进度。`
  }
}
