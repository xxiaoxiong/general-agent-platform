import assert from 'node:assert/strict'
import test from 'node:test'
import { applyRunEvent } from '../src/features/run/blocks.ts'

function replay(message, event, data) {
  applyRunEvent(message, {
    event, data, run_id: 'history-run', trace_id: 'history-run',
    sequence: (message.lastSeq ?? 0) + 1, timestamp: '2026-11-27T10:00:00Z',
  })
}

const source = {
  source_id: 'history_src_1', conversation_id: 10, message_ids: ['u1', 'a1'],
  conversation_title: '架构讨论', created_at: '2026-11-20T10:00:00',
  snippet: '之前确定由 DeepAgents 负责执行。', relevance_score: 0.9, scope: 'user',
}

test('历史来源事件生成来源卡片，重复来源按 source_id 去重', () => {
  const message = { blocks: [] }
  replay(message, 'history.sources', { sources: [source], queries: ['架构'] })
  assert.equal(message.blocks.length, 1)
  assert.equal(message.blocks[0].kind, 'history_sources')
  assert.equal(message.blocks[0].sources.length, 1)
  assert.deepEqual(message.blocks[0].queries, ['架构'])

  replay(message, 'history.sources', {
    sources: [source, { ...source, source_id: 'history_src_2', conversation_id: 11 }],
  })
  assert.equal(message.blocks.length, 1)
  assert.equal(message.blocks[0].sources.length, 2)
})

test('空来源或非法来源不生成空区域', () => {
  const message = { blocks: [] }
  replay(message, 'history.sources', { sources: [] })
  replay(message, 'history.sources', {
    sources: [{ source_id: '', conversation_id: 'bad' }, null],
  })
  assert.equal(message.blocks.length, 0)
})
