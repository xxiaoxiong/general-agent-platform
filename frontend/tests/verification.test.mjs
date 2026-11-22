import assert from 'node:assert/strict'
import test from 'node:test'
import { applyRunEvent, finalizeRunMessage } from '../src/features/run/blocks.ts'

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
      checks: [{ id: 'citations_present', passed: false, detail: '缺少来源' }] } } })
  finalizeRunMessage(chat, 'run.failed')
  assert.equal(chat.runStatus, 'limit_exceeded')
  assert.equal(chat.blocks[0].state, 'incomplete')
  assert.equal(chat.blocks[1].text, '已保存的报告草稿。')
})
