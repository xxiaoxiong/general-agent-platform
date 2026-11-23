import assert from 'node:assert/strict'
import test from 'node:test'
import { applyRunEvent, executionPause, finalizeRunMessage } from '../src/features/run/blocks.ts'

function replay(message, event, data) {
  applyRunEvent(message, { event, data, run_id: 'run', trace_id: 'run',
    sequence: (message.lastSeq || 0) + 1, timestamp: '2026-11-21T12:00:00Z' })
}

test('补齐交付后只展示最终正文并保留步骤和下载', () => {
  const chat = { blocks: [] }
  replay(chat, 'assistant.delta', { text: '先给出初稿。' })
  replay(chat, 'tool.started', { tool_name: 'write_file', tool_call_id: 'file-1' })
  replay(chat, 'tool.completed', { tool_name: 'write_file', tool_call_id: 'file-1' })
  replay(chat, 'run.retrying', { reason: 'verification_fix', round: 1,
    failed_checks: [{ id: 'artifact_produced', passed: false, detail: '缺少 audit.json' }] })
  assert.equal(chat.blocks[2].state, 'reworking')
  const verified = { version: 2, status: 'passed', fix_rounds: 1, checks: [],
    artifacts: [{ name: 'audit.json', size: 20, download_url: '/api/v2/artifacts/MjphaQ' },
      { name: '远端', size: 10, download_url: '//外部站点/a' }] }
  replay(chat, 'verification.completed', verified)
  replay(chat, 'run.completed', { final_text: '经核对，两个文件均已保存。', summary: { verification: verified } })
  assert.deepEqual(chat.blocks.map(block => block.kind), ['tool', 'verification', 'text'])
  assert.equal(chat.blocks[1].artifacts.length, 1)
  assert.equal(chat.blocks[2].text, '经核对，两个文件均已保存。')
})

test('未通过验收保留草稿，预算停止状态在重连时不变', () => {
  const chat = { blocks: [], streaming: true }
  replay(chat, 'assistant.delta', { text: '一份旧稿。' })
  replay(chat, 'run.failed', { status: 'limit_exceeded', error: '执行预算已用完', final_text: '已保存的报告草稿。',
    summary: { verification: { version: 2, status: 'failed', stop_reason: 'budget',
      checks: [{ id: 'artifact_produced', passed: false, detail: '缺少交付文件' }] } } })
  finalizeRunMessage(chat, 'run.failed')
  assert.equal(chat.runStatus, 'limit_exceeded')
  assert.equal(chat.blocks[0].state, 'incomplete')
  assert.equal(chat.blocks[1].text, '已保存的报告草稿。')
  assert.equal(chat.error, undefined)
  assert.equal(executionPause(chat)?.canContinue, true)
})

test('自动止损给出续做说明，真实异常仍走错误提示', () => {
  const chat = { blocks: [] }
  replay(chat, 'assistant.delta', { text: '已写入项目入口。' })
  replay(chat, 'run.failed', { status: 'limit_exceeded', summary: { stop: { reason: 'no_progress', can_continue: true } } })
  finalizeRunMessage(chat, 'run.failed')
  assert.deepEqual(executionPause(chat), { text: '连续尝试后暂无新进展。可从已记录的进度继续。', canContinue: true })
  assert.equal(chat.blocks[0].text, '已写入项目入口。')

  const failure = { blocks: [] }
  replay(failure, 'run.failed', { status: 'failed', error: '模型连接失败' })
  assert.equal(executionPause(failure), undefined)
  assert.equal(failure.error, '模型连接失败')
})

test('历史超限消息展示暂停，待核对的外部动作不提供直接续做', () => {
  const old = { blocks: [] }
  replay(old, 'run.failed', { status: 'limit_exceeded', error: '模型调用次数超过预算(100)' })
  assert.deepEqual(executionPause(old), { text: '本轮已达到执行上限。', canContinue: false })
  const pending = { blocks: [] }
  replay(pending, 'run.failed', { status: 'limit_exceeded', summary: {
    stop: { reason: 'budget', can_continue: false },
    verification: { version: 2, stop_reason: 'external_operation' },
  } })
  assert.deepEqual(executionPause(pending), { text: '本轮已暂停，请先核对外部操作记录。', canContinue: false })
})
