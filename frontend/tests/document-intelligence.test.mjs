import assert from 'node:assert/strict'
import test from 'node:test'
import {
  applyRunEvent, documentReviewInput, finalizeRunMessage, isDocumentReviewInput, pendingDocInput, recordDocumentReview,
} from '../src/features/run/blocks.ts'
import { shouldReplayPastPause } from '../src/features/run/runReplay.ts'
import { collectRunInput, isProfileConfirmation } from '../src/features/documents/inputRequest.ts'
import { createProfileDefinition, nextProfileSection, normalizeProfileSections } from '../src/features/documents/profileDefinition.ts'
import { documentTaskPayload } from '../src/features/documents/taskModes.ts'
import { messageAttachments } from '../src/views/workspace/attachmentHistory.ts'

function replay(msg, event, data = {}) {
  applyRunEvent(msg, {
    event, data, run_id: 'run-1', trace_id: 'run-1',
    sequence: (msg.lastSeq ?? 0) + 1, timestamp: '2026-11-20T12:00:00Z',
  })
}

function request(id, properties, required = Object.keys(properties)) {
  return { request_id: id, prompt: '请补充资料', input_schema: { type: 'object', properties, required } }
}

test('旧审阅事件只含部分锚点时仍按完整的草稿范围提交', () => {
  const msg = { blocks: [] }
  const ids = Array.from({ length: 25 }, (_, index) => `change_${index + 1}`)
  replay(msg, 'document.edit.review_required', {
    ordinal: 3, changes: ids.slice(0, 20).map(id => ({ id, kind: 'text' })),
  })
  replay(msg, 'input.required', request('review', {
    accepted_change_ids: { type: 'array', items: { type: 'string', enum: ids } },
  }))
  const review = msg.blocks.find(block => block.kind === 'document_edit_review')
  assert.equal(review.review.context_url, '/api/v2/runs/run-1/document-review/context')
  assert.equal(documentReviewInput(msg, review).state, 'pending')
  recordDocumentReview(msg, 'review', ['change_25'])
  assert.deepEqual(review.decision.accepted_change_ids, ['change_25'])
  assert.deepEqual(review.decision.rejected_change_ids, ids.slice(0, 24))
})

test('原任务续跑保留提交记录和已完成章节，历史失败不打断回放', () => {
  const msg = { blocks: [] }
  replay(msg, 'task.routed', { task_type: 'document_generate' })
  replay(msg, 'input.required', request('doc-1', { software: { type: 'string' } }))
  replay(msg, 'input.received', { request_id: 'doc-1' })
  replay(msg, 'document.section.failed', { title: '故障诊断', completed: 5, total: 9 })
  replay(msg, 'run.failed', { error: '章节生成失败', document_retryable: true })
  finalizeRunMessage(msg, 'run.failed')
  assert.equal(msg.documentRetryable, true)
  assert.equal(msg.blocks.find(block => block.kind === 'document_stage').detail, '已完成 5/9 个部分 · 故障诊断')
  replay(msg, 'run.retrying', { manual: true })
  replay(msg, 'run.failed', { historical: true, error: '旧失败' })
  assert.equal(msg.error, undefined)
  assert.equal(msg.runStatus, 'running')
  assert.equal(msg.blocks.find(block => block.kind === 'input_request').state, 'submitted')
  replay(msg, 'document.section.started', { title: '故障诊断', completed: 5, total: 9 })
  assert.equal(msg.blocks.find(block => block.kind === 'document_stage').label, '已完成 5/9 个部分')
})

test('任务模式钉住编辑版本，并保持普通对话默认请求兼容', () => {
  assert.equal(JSON.stringify(documentTaskPayload({ mode: 'auto' })), '{}')
  assert.deepEqual(JSON.parse(JSON.stringify(documentTaskPayload({ mode: 'document_edit', documentId: 9, version: 3 }))), {
    task_mode: 'document_edit', document_id: 9, task_options: { document_version_no: 3 },
  })
  assert.deepEqual(JSON.parse(JSON.stringify(documentTaskPayload({ mode: 'document_generate', profileId: 4, documentId: 9 }))), {
    task_mode: 'document_generate', document_profile_id: 4,
  })
})

test('对话刷新后使用附件原文件路径，兼容旧的提取副本注记', () => {
  const inline = '修改这个文档\n\n[附件]正文中的小标题'
  assert.deepEqual(messageAttachments(inline, [{ path: 'uploads/汇报（原件）.docx' }]), {
    text: inline, files: [{ name: '汇报（原件）.docx', path: 'uploads/汇报（原件）.docx' }],
  })
  const legacy = '查看附件\n\n[附件]（文件已存入会话工作区。）\n- /workspace/uploads/汇报（原件）.docx（文本副本：/workspace/uploads/汇报（原件）.docx.extracted.txt）'
  assert.deepEqual(messageAttachments(legacy), {
    text: '查看附件', files: [{ name: '汇报（原件）.docx', path: 'uploads/汇报（原件）.docx' }],
  })
  assert.deepEqual(messageAttachments('查看附件', [{ path: 'uploads/../secret.docx' }]).files, [])
})

test('回放文档产物保留稳定版本，重复事件只渲染一张卡片', () => {
  const msg = { blocks: [] }
  replay(msg, 'task.routed', { task_type: 'document_generate' })
  replay(msg, 'document.validation.completed', { passed: true })
  const artifact = { document_id: 8, document_version_id: 55, version_no: 2, title: '项目总结', filename: '项目总结.docx', size: 1200 }
  replay(msg, 'artifact.created', artifact)
  replay(msg, 'artifact.created', artifact)
  replay(msg, 'run.completed')
  finalizeRunMessage(msg, 'run.completed')
  assert.deepEqual(msg.blocks.map(block => block.kind), ['document_stage', 'document_artifact'])
  assert.deepEqual(msg.blocks[1].artifact, artifact)
  assert.equal(msg.runStatus, 'completed')
})

test('规范草稿须由原任务补充确认，保存后可回放规范版本', () => {
  const msg = { blocks: [] }
  const definition = createProfileDefinition()
  definition.name = '工作总结'
  replay(msg, 'document.profile_draft.created', { definition, uncertainties: ['核对结论格式'] })
  const input = request('confirm-1', { definition: { type: 'object' }, confirm: { type: 'boolean' } })
  replay(msg, 'input.required', input)
  replay(msg, 'run.status_changed', { status: 'waiting_input' })
  finalizeRunMessage(msg, 'run.status_changed')
  assert.equal(msg.runStatus, 'waiting_input')
  assert.equal(isProfileConfirmation(pendingDocInput(msg).request), true)
  const draft = msg.blocks.find(block => block.kind === 'document_profile_draft')
  assert.equal(draft.saved, undefined)
  replay(msg, 'input.received', { request_id: input.request_id })
  replay(msg, 'document.profile_saved', { profile_id: 14, version_no: 1 })
  finalizeRunMessage(msg, 'run.completed')
  assert.equal(pendingDocInput(msg), undefined)
  assert.deepEqual(draft.saved, { id: 14, version: 1 })
})

test('历史补充保留多轮记录，当前待补充与终态不会混淆', () => {
  const msg = { blocks: [] }
  const first = request('request-1', { date: { type: 'string', format: 'date' } })
  const second = request('request-2', { total: { type: 'number' } })
  replay(msg, 'input.required', first)
  replay(msg, 'input.required', first)
  replay(msg, 'input.received', { request_id: first.request_id })
  replay(msg, 'run.status_changed', { status: 'waiting_input', historical: true })
  assert.equal(msg.runStatus, 'running')
  replay(msg, 'input.required', second)
  assert.equal(pendingDocInput(msg).request.request_id, 'request-2')
  finalizeRunMessage(msg, 'run.cancelled')
  assert.deepEqual(msg.blocks.filter(block => block.kind === 'input_request').map(block => block.state), ['submitted', 'closed'])
  assert.equal(shouldReplayPastPause('waiting_input', 8, 'completed', 9), true)
  assert.equal(shouldReplayPastPause('waiting_input', 8, 'waiting_input', 8), false)
})

test('文档改动绑定同一条 Run 的审阅请求，保留逐项决定', () => {
  const msg = { blocks: [] }
  const scope = request('scope', { confirm: { type: 'boolean' } })
  replay(msg, 'input.required', scope)
  const review = {
    ordinal: 3,
    source_name: '项目总结.docx',
    changes: [
      { id: 'change_1', kind: 'text', section_title: '指标', before: '原值 10%', after: '新值 7%' },
      { id: 'change_2', kind: 'image', title: '趋势图', before_image_url: '/api/v2/runs/run-1/images/old' },
    ],
  }
  replay(msg, 'document.edit.review_required', review)
  replay(msg, 'document.edit.review_required', review)
  const block = msg.blocks.find((item) => item.kind === 'document_edit_review')
  assert.equal(msg.blocks.filter((item) => item.kind === 'document_edit_review').length, 1)
  assert.equal(documentReviewInput(msg, block), undefined)
  replay(msg, 'input.received', { request_id: scope.request_id })
  const input = request('review', { accepted_change_ids: { type: 'array', items: { enum: ['change_2', 'change_1'] } } })
  replay(msg, 'input.required', input)
  assert.equal(isDocumentReviewInput(pendingDocInput(msg).request), true)
  assert.equal(documentReviewInput(msg, block).state, 'pending')
  recordDocumentReview(msg, 'review', ['change_1'])
  replay(msg, 'input.received', { request_id: input.request_id })
  assert.equal(documentReviewInput(msg, block).state, 'submitted')
  assert.deepEqual(block.decision, { accepted_change_ids: ['change_1'], rejected_change_ids: ['change_2'] })
})

test('从历史事件恢复全拒绝结果且不虚构产物', () => {
  const msg = { blocks: [] }
  const input = request('review', { accepted_change_ids: { type: 'array', items: { enum: ['change_1'] } } })
  // 状态恢复可以先拿到 pending_input，再补齐审阅正文。
  replay(msg, 'input.required', input)
  replay(msg, 'document.edit.review_required', {
    ordinal: 3, changes: [{ id: 'change_1', kind: 'text', before: '原稿', after: '改稿' }],
  })
  const block = msg.blocks.find((item) => item.kind === 'document_edit_review')
  assert.equal(documentReviewInput(msg, block).request.request_id, 'review')
  replay(msg, 'input.received', { request_id: input.request_id })
  replay(msg, 'document.edit.reviewed', { ordinal: 3, accepted_change_ids: [], rejected_change_ids: ['change_1'] })
  finalizeRunMessage(msg, 'run.completed')
  assert.deepEqual(block.decision, { accepted_change_ids: [], rejected_change_ids: ['change_1'] })
  assert(!msg.blocks.some((item) => item.kind === 'document_artifact' || item.kind === 'text'))
})

test('动态补充将日期、数字、单选、多选转换为对应业务值', () => {
  const input = request('request-1', {
    submitted: { type: 'string', format: 'date' },
    quantity: { type: 'number' },
    priority: { enum: ['low', 'high'] },
    reviewers: { type: 'array', items: { enum: ['内审', '技术'] } },
    notes: { type: 'string' },
  }, ['submitted', 'quantity', 'priority', 'reviewers'])
  assert.deepEqual({ ...collectRunInput(input, { submitted: '2026-11-20', quantity: '0', priority: '1', reviewers: [1], notes: '  ' }) }, {
    submitted: '2026-11-20', quantity: 0, priority: 'high', reviewers: ['技术'],
  })
  assert.throws(() => collectRunInput(input, { submitted: '2026-11-20', quantity: '零' }), /数字/)
})

test('章节移动保留原始定位 ID，新增章节不会撞 ID', () => {
  const [first] = createProfileDefinition().sections
  const second = nextProfileSection([first])
  const normalized = normalizeProfileSections([{ ...second, title: ' 成果 ' }, first])
  assert.deepEqual(normalized.map(({ id, title, order }) => [id, title, order]), [
    ['section_2', '成果', 1], ['section_1', '', 2],
  ])
})

test('普通对话和工具回复保持文本块顺序', () => {
  const msg = { blocks: [] }
  replay(msg, 'task.routed', { task_type: 'general_chat' })
  replay(msg, 'assistant.delta', { text: '你好' })
  replay(msg, 'tool.started', { tool_call_id: 'tool-1', tool_name: 'read_file', input: { path: 'uploads/test.txt' } })
  replay(msg, 'tool.completed', { tool_call_id: 'tool-1' })
  replay(msg, 'run.completed', { final_text: '你好！' })
  assert.deepEqual(msg.blocks.map(block => block.kind), ['text', 'tool', 'text'])
  assert.equal(msg.blocks[2].text, '！')
})
