/**
 * 对话流块模型（流式执行体验）：
 * 助手消息由「文本段 / 工具步骤 / 计划」按事件到达顺序组成的块流，
 * 工具小步骤实时追加在对话流尾部（穿插在文本之间），而非聚堆在消息顶部。
 */
import type { RunEventEnvelope } from '@/api/runs'
import type { DocumentArtifact, DocumentEditReview, DocumentReviewDecision, InputRequest, ProfileDefinition } from '@/api/documents'
import { finishTeamProgress, readTeamProgress, teamPhaseLabels, type TeamProgress } from './teamProgress.ts'
import {
  PROJECT_DOWNLOAD_PATH, projectPhaseLabels, readProjectDelivery, readProjectProgress, type ProjectDelivery, type ProjectProgress,
} from './projectDelivery.ts'

export interface TextBlock {
  kind: 'text'
  text: string
}

export interface PlanBlock {
  kind: 'plan'
  todos: any[]
}

export interface TeamBlock {
  kind: 'team'
  progress: TeamProgress
}

export interface ProjectBlock {
  kind: 'project'
  progress?: ProjectProgress
  delivery?: ProjectDelivery
}

export interface VerificationBlock {
  kind: 'verification'
  state: 'reworking' | 'passed' | 'incomplete'
  rounds: number
  checks: { id: string; passed: boolean; detail: string }[]
  artifacts: { name: string; size: number; url: string }[]
  stopReason: string
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
  /** 文件类工具的完整工作区路径（read/write/edit），点击行内文件名打开右侧预览 */
  filePath?: string
}

export interface DocumentArtifactBlock {
  kind: 'document_artifact'
  artifact: DocumentArtifact
}

export interface ProfileDraftBlock {
  kind: 'document_profile_draft'
  definition: ProfileDefinition
  uncertainties: string[]
  warnings: string[]
  saved?: { id: number; version?: number }
}

export interface InputRequestBlock {
  kind: 'input_request'
  request: InputRequest
  state: 'pending' | 'submitted' | 'closed'
}

export interface DocumentEditReviewBlock {
  kind: 'document_edit_review'
  review: DocumentEditReview
  requestId?: string
  decision?: DocumentReviewDecision
}

export interface DocumentStageBlock {
  kind: 'document_stage'
  label: string
  detail?: string
  tone?: 'warning' | 'success'
}

export type MsgBlock = TextBlock | ToolBlock | PlanBlock | DocumentArtifactBlock | ProfileDraftBlock
  | InputRequestBlock | DocumentStageBlock | DocumentEditReviewBlock | TeamBlock | ProjectBlock | VerificationBlock

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
  executionStop?: { reason: 'budget' | 'no_progress'; canContinue: boolean }
  pendingActions?: any[]
  finalText?: string
  documentRetryable?: boolean
  documentCompleted?: number
  documentTotal?: number
  /** 运行起始/最近事件时间戳（事件 timestamp，供任务耗时计算） */
  runStartedAt?: number
  lastEventTs?: number
  /** 任务耗时（run.started -> 最近事件），终态后固化展示 */
  runDurationMs?: number
}

export function executionPause(message: RunStreamMessage) {
  if (message.runStatus !== 'limit_exceeded') return undefined
  const report = message.blocks.find(block => block.kind === 'verification')
  const requiresReconciliation = report?.stopReason === 'external_operation'
  const savedFileTask = !!report && (report.artifacts.length > 0 || report.checks.some(check => check.id === 'artifact_produced'))
  const canContinue = !requiresReconciliation && (message.executionStop?.canContinue
    ?? (savedFileTask && (report.stopReason === 'budget' || report.stopReason === 'no_progress')))
  const noProgress = (message.executionStop?.reason ?? report?.stopReason) === 'no_progress'
  const reason = noProgress ? '连续尝试后暂无新进展。' : '本轮已达到执行上限。'
  return {
    text: requiresReconciliation ? '本轮已暂停，请先核对外部操作记录。'
      : reason + (canContinue ? '可从已记录的进度继续。' : ''),
    canContinue,
  }
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

/** 文件类工具（read/write/edit）从入参取完整工作区路径，供点击行内文件名预览 */
const FILE_PATH_TOOLS = new Set(['read_file', 'write_file', 'edit_file'])
export function extractFilePath(name: string, input: any): string {
  if (!FILE_PATH_TOOLS.has(name) || !input || typeof input !== 'object') return ''
  const raw = input.path ?? input.file_path ?? input.filePath ?? ''
  return typeof raw === 'string' ? raw.trim() : ''
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

function setVerifiedAnswer(msg: RunStreamMessage, value: string) {
  msg.finalText = value
  if (joinedText(msg) === value) return
  // 纠正稿是整份交付。原先流式展示的过程文字不进入正文，工具、计划和验收记录继续保留。
  msg.blocks = msg.blocks.filter(block => block.kind !== 'text')
  if (hasVisibleText(value)) msg.blocks.push({ kind: 'text', text: value })
}

function setVerification(msg: RunStreamMessage, data: Record<string, any>, state: VerificationBlock['state']) {
  const rawChecks = Array.isArray(data.checks) ? data.checks : []
  const checks = rawChecks.slice(0, 24).filter(check => typeof check?.id === 'string' && typeof check.passed === 'boolean')
    .map(check => ({ id: check.id, passed: check.passed, detail: String(check.detail || '') }))
  const artifacts = Array.isArray(data.artifacts) ? data.artifacts.slice(0, 50)
    .filter(file => typeof file?.name === 'string' && /^[^/\\\u0000-\u001f]{1,220}$/.test(file.name)
      && Number.isSafeInteger(file.size) && file.size > 0 && typeof file.download_url === 'string'
      && PROJECT_DOWNLOAD_PATH.test(file.download_url))
    .map(file => ({ name: file.name, size: file.size, url: file.download_url })) : []
  const next: VerificationBlock = { kind: 'verification', state, checks, artifacts,
    rounds: Number.isSafeInteger(data.fix_rounds) && data.fix_rounds >= 0 ? data.fix_rounds : 0,
    stopReason: typeof data.stop_reason === 'string' ? data.stop_reason : '' }
  const previous = msg.blocks.find(block => block.kind === 'verification')
  if (previous) Object.assign(previous, next)
  else msg.blocks.push(next)
}

const DOCUMENT_TASK_LABELS: Record<string, string> = {
  document_analyze: '文档分析',
  profile_extract: '提炼文档规范',
  document_generate: '生成文档',
  document_edit: '修改文档',
}

export function pendingDocInput(msg: RunStreamMessage): InputRequestBlock | undefined {
  return msg.blocks.find((b): b is InputRequestBlock => b.kind === 'input_request' && b.state === 'pending')
}

export function isDocumentReviewInput(request: InputRequest): boolean {
  return request.input_schema.properties?.accepted_change_ids?.type === 'array'
}

export function documentReviewInput(msg: RunStreamMessage, review: DocumentEditReviewBlock): InputRequestBlock | undefined {
  return msg.blocks.find((block): block is InputRequestBlock => block.kind === 'input_request'
    && block.request.request_id === review.requestId)
}

function attachReviewInput(msg: RunStreamMessage, request: InputRequest) {
  if (!isDocumentReviewInput(request)) return
  const choices = request.input_schema.properties?.accepted_change_ids?.items?.enum
  if (!choices?.length) return
  const review = msg.blocks.find((block): block is DocumentEditReviewBlock =>
    block.kind === 'document_edit_review' && !block.requestId && block.review.changes.length <= choices.length
    && block.review.changes.every((change) => choices.includes(change.id)))
  if (review) review.requestId = request.request_id
}

export function recordDocumentReview(msg: RunStreamMessage, requestId: string, accepted: string[]) {
  const block = msg.blocks.find((item): item is DocumentEditReviewBlock => item.kind === 'document_edit_review'
    && item.requestId === requestId)
  if (!block) return
  const all = documentReviewInput(msg, block)?.request.input_schema.properties?.accepted_change_ids?.items?.enum
    ?.filter((id): id is string => typeof id === 'string')
    ?? block.review.changes.map((change) => change.id)
  block.decision = {
    accepted_change_ids: all.filter((id) => accepted.includes(id)),
    rejected_change_ids: all.filter((id) => !accepted.includes(id)),
  }
}

export function addDocInput(msg: RunStreamMessage, request: InputRequest) {
  if (msg.blocks.some((b) => b.kind === 'input_request' && b.request.request_id === request.request_id)) return
  const existing = pendingDocInput(msg)
  if (existing) existing.state = 'closed'
  msg.blocks.push({ kind: 'input_request', request, state: 'pending' })
  attachReviewInput(msg, request)
  msg.runStatus = 'waiting_input'
  msg.liveStatus = isDocumentReviewInput(request) ? '等待审阅文档改动…' : '等待补充信息…'
  msg.lastActivity = 'other'
}

export function acceptDocInput(msg: RunStreamMessage, requestId: string) {
  const block = msg.blocks.find((b): b is InputRequestBlock => b.kind === 'input_request' && b.request.request_id === requestId)
  if (block) block.state = 'submitted'
  if (!pendingDocInput(msg) && msg.runStatus === 'waiting_input') msg.runStatus = 'running'
}

function setDocumentStage(msg: RunStreamMessage, label: string, detail?: string, tone?: DocumentStageBlock['tone']) {
  const stage = msg.blocks.find((b): b is DocumentStageBlock => b.kind === 'document_stage')
  if (stage) Object.assign(stage, { label, detail, tone })
  else msg.blocks.push({ kind: 'document_stage', label, detail, tone })
  msg.liveStatus = label
  msg.lastActivity = 'other'
}

/** v2 事件 -> 块流状态（实时与历史回放共用同一归约器） */
export function applyRunEvent(msg: RunStreamMessage, ev: RunEventEnvelope): void {
  msg.lastEventAt = Date.now()
  // 事件真实时间戳：供任务耗时（run 起点 -> 最近事件）计算，历史回放也准确
  const evTs = Date.parse(ev.timestamp) || Date.now()
  msg.lastEventTs = evTs
  if (typeof ev.sequence === 'number' && ev.sequence > (msg.lastSeq || 0)) {
    msg.lastSeq = ev.sequence
  }
  const blocks = msg.blocks
  const data = ev.data || {}

  switch (ev.event) {
    case 'run.started':
      msg.runStatus = 'running'
      msg.runStartedAt = evTs
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

    case 'team.updated': {
      const progress = readTeamProgress(data)
      if (!progress) break
      const team = blocks.find((block): block is TeamBlock => block.kind === 'team')
      if (team) team.progress = progress
      else blocks.push({ kind: 'team', progress })
      msg.liveStatus = teamPhaseLabels[progress.phase]
      msg.lastActivity = 'other'
      break
    }

    case 'project.progress': {
      const progress = readProjectProgress(data)
      if (!progress) break
      const project = blocks.find((block): block is ProjectBlock => block.kind === 'project')
      if (project?.delivery) break
      if (project) project.progress = progress
      else blocks.push({ kind: 'project', progress })
      msg.liveStatus = projectPhaseLabels[progress.phase]
      msg.lastActivity = 'other'
      break
    }

    case 'project.delivery': {
      const delivery = readProjectDelivery(data)
      if (!delivery) break
      const project = blocks.find((block): block is ProjectBlock => block.kind === 'project')
      if (project) project.delivery = delivery
      else blocks.push({ kind: 'project', delivery })
      msg.liveStatus = delivery.state === 'passed' ? '项目已打包' : '已保存项目草稿'
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
        filePath: extractFilePath(String(data.tool_name || ''), data.input),
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
          filePath: extractFilePath(String(data.tool_name || ''), data.input),
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

    case 'task.routed': {
      const label = DOCUMENT_TASK_LABELS[String(data.task_type || '')]
      if (label) setDocumentStage(msg, label)
      break
    }

    case 'document.profile_extraction.started':
      setDocumentStage(msg, '正在提炼文档规范')
      break

    case 'document.profile_draft.created':
      if (data.definition?.sections?.length && !blocks.some((b) => b.kind === 'document_profile_draft')) {
        blocks.push({
          kind: 'document_profile_draft', definition: data.definition,
          uncertainties: Array.isArray(data.uncertainties) ? data.uncertainties : [],
          warnings: Array.isArray(data.warnings) ? data.warnings : [],
        })
        setDocumentStage(msg, '规范草稿已生成')
      }
      break

    case 'document.profile_saved': {
      const draft = blocks.find((b): b is ProfileDraftBlock => b.kind === 'document_profile_draft')
      if (draft && Number.isSafeInteger(data.profile_id)) {
        draft.saved = { id: data.profile_id, version: data.version_no }
        if (data.definition?.sections?.length) draft.definition = data.definition
      }
      setDocumentStage(msg, '已保存文档规范', undefined, 'success')
      break
    }

    case 'document.generation.started':
      setDocumentStage(msg, '正在生成文档')
      break

    case 'document.section.started':
    case 'document.section.completed':
    case 'document.section.failed': {
      if (Number.isInteger(data.completed)) msg.documentCompleted = data.completed
      else if (ev.event === 'document.section.completed') msg.documentCompleted = (msg.documentCompleted ?? 0) + 1
      if (Number.isInteger(data.total)) msg.documentTotal = data.total
      const title = data.title || data.section_title || data.section_id
      const progress = msg.documentTotal ? `已完成 ${msg.documentCompleted ?? 0}/${msg.documentTotal} 个部分` : ''
      if (ev.event === 'document.section.failed') {
        setDocumentStage(msg, '文档编写暂停', [progress, title].filter(Boolean).join(' · '), 'warning')
      } else {
        setDocumentStage(msg, progress || '正在编写文档', title)
      }
      break
    }

    case 'document.validation.started':
      setDocumentStage(msg, '正在校验文档')
      break

    case 'document.validation.completed': {
      const valid = data.valid ?? data.passed
      setDocumentStage(msg, valid === false ? '文档校验发现问题' : '文档校验完成', undefined, valid === false ? 'warning' : 'success')
      break
    }

    case 'document.edit.review_required': {
      const ordinal = data.ordinal
      const changes = Array.isArray(data.changes) ? data.changes : []
      if (Number.isSafeInteger(ordinal) && changes.length
        && changes.every((change) => typeof change?.id === 'string' && change.id)
        && new Set(changes.map((change) => change.id)).size === changes.length
        && !blocks.some((block) => block.kind === 'document_edit_review' && block.review.ordinal === ordinal)) {
        blocks.push({ kind: 'document_edit_review', review: {
          ...data as DocumentEditReview, context_url: `/api/v2/runs/${ev.run_id}/document-review/context`,
        } })
        const waiting = pendingDocInput(msg)
        if (waiting) attachReviewInput(msg, waiting.request)
        setDocumentStage(msg, '文档修改稿已准备好')
      }
      break
    }

    case 'document.edit.reviewed': {
      const block = blocks.find((item): item is DocumentEditReviewBlock => item.kind === 'document_edit_review'
        && item.review.ordinal === data.ordinal)
      if (block && Array.isArray(data.accepted_change_ids) && Array.isArray(data.rejected_change_ids)) {
        block.decision = {
          accepted_change_ids: data.accepted_change_ids,
          rejected_change_ids: data.rejected_change_ids,
        }
        setDocumentStage(msg, data.accepted_change_ids.length ? '文档改动已审阅' : '已拒绝全部文档改动')
      }
      break
    }

    case 'input.required':
      if (typeof data.request_id === 'string' && data.input_schema) addDocInput(msg, data as InputRequest)
      break

    case 'input.received':
      acceptDocInput(msg, data.request_id)
      break

    case 'artifact.created':
    case 'document.artifact.created':
      if (Number.isSafeInteger(data.document_id) && Number.isSafeInteger(data.document_version_id)
        && Number.isSafeInteger(data.version_no)
        && !blocks.some((b) => b.kind === 'document_artifact' && b.artifact.document_version_id === data.document_version_id)) {
        blocks.push({ kind: 'document_artifact', artifact: data as DocumentArtifact })
        setDocumentStage(msg, '文档已生成', undefined, 'success')
      }
      break

    case 'run.status_changed':
      if (data.historical) {
        const input = pendingDocInput(msg)
        if (input && input.request.request_id === data.request_id) {
          input.state = 'closed'
          if (msg.runStatus === 'waiting_input') msg.runStatus = 'running'
        }
        break
      }
      msg.runStatus = String(data.status || msg.runStatus || '')
      if (data.actions !== undefined) msg.pendingActions = (data.actions as any[]) || []
      else if (msg.runStatus !== 'waiting_approval') msg.pendingActions = []
      break

    case 'run.retrying': {
      if (data.reason === 'verification_fix') {
        setVerification(msg, { checks: data.failed_checks, fix_rounds: data.round }, 'reworking')
        msg.liveStatus = '正在补齐待交付项…'
        msg.lastActivity = 'other'
        break
      }
      if (data.manual) {
        msg.error = undefined
        msg.documentRetryable = false
        msg.runStatus = 'running'
        msg.streaming = true
        setDocumentStage(msg, '正在从已保存的进度继续')
        break
      }
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

    case 'verification.completed':
      setVerification(msg, data, data.status === 'passed' ? 'passed' : 'incomplete')
      break

    case 'run.completed':
      msg.finalText = String(data.final_text || '')
      if (data.summary?.verification?.version === 2) {
        if (data.summary.verification.fix_rounds || data.summary.verification.artifacts?.length) {
          setVerification(msg, data.summary.verification, 'passed')
        }
        setVerifiedAnswer(msg, msg.finalText)
      } else gapFillText(msg, msg.finalText)
      msg.runStatus = 'completed'
      break

    case 'run.failed':
      if (data.historical) break
      msg.runStatus = data.status === 'limit_exceeded' ? 'limit_exceeded' : 'failed'
      msg.error = msg.runStatus === 'failed' ? String(data.error || '运行失败') : undefined
      const stop = data.summary?.stop
      if (msg.runStatus === 'limit_exceeded' && (stop?.reason === 'budget' || stop?.reason === 'no_progress')) {
        msg.executionStop = { reason: stop.reason, canContinue: stop.can_continue === true }
      }
      if (data.summary?.verification) {
        setVerification(msg, data.summary.verification, 'incomplete')
        if (typeof data.final_text === 'string') setVerifiedAnswer(msg, data.final_text)
      } else if (msg.runStatus === 'limit_exceeded' && typeof data.final_text === 'string') {
        msg.finalText = data.final_text
        gapFillText(msg, data.final_text)
      }
      msg.documentRetryable = data.document_retryable === true
      if (blocks.some((block) => block.kind === 'document_stage') && !blocks.some(
        (block) => block.kind === 'document_stage' && block.label === '文档编写暂停',
      )) setDocumentStage(msg, '文档任务已暂停', msg.documentCompleted ? `已完成 ${msg.documentCompleted} 个部分` : undefined, 'warning')
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
  // 任务耗时固化（run.started -> 最近事件时间戳）
  if (msg.runStartedAt && msg.lastEventTs && msg.lastEventTs >= msg.runStartedAt) {
    msg.runDurationMs = msg.lastEventTs - msg.runStartedAt
  }
  const map: Record<string, string> = {
    'run.completed': 'completed', 'run.failed': msg.runStatus === 'limit_exceeded' ? 'limit_exceeded' : 'failed',
    'run.cancelled': 'cancelled', 'run.suspended': 'suspended',
    'run.status_changed': msg.runStatus || 'completed',
  }
  msg.runStatus = reason.startsWith('reconnect_failed')
    ? (msg.runStatus || 'failed')
    : (map[reason] || reason)
  if (fallbackText) gapFillText(msg, fallbackText)
  const hasDelivery = msg.blocks.some((b) => b.kind === 'document_artifact' || (b.kind === 'project' && b.delivery)
    || (b.kind === 'document_profile_draft' && b.saved) || (b.kind === 'document_edit_review' && b.decision))
  if (!hasDelivery && !joinedText(msg).trim()) {
    // 占位文本只在真正收尾时出现；待审批/挂起还有后续执行，不能宣告"已完成"
    if (msg.runStatus === 'cancelled') msg.blocks.push({ kind: 'text', text: '（已停止生成）' })
    else if (msg.runStatus === 'completed') msg.blocks.push({ kind: 'text', text: '（任务已完成，本次未生成文本回答）' })
  }
  // 终态收口：被中断/未回执的工具行不再永远"执行中"（待审批时保留，后续还会执行）
  if (['completed', 'failed', 'cancelled', 'suspended', 'limit_exceeded'].includes(msg.runStatus || '')) {
    msg.pendingActions = []
    for (const b of msg.blocks) {
      if (b.kind === 'input_request' && b.state === 'pending') b.state = 'closed'
      if (b.kind === 'team') b.progress = finishTeamProgress(b.progress, msg.runStatus)
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
