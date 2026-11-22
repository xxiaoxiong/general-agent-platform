import assert from 'node:assert/strict'
import test from 'node:test'
import { applyRunEvent, finalizeRunMessage } from '../src/features/run/blocks.ts'

const evidence = { id: 'evidence', role: '资料分析员', title: '提取依据', brief: '从给出的资料提取依据', depends_on: [] }
const design = { id: 'design', role: '方案设计师', title: '设计实施方案', brief: '结合依据组织分步骤实施方案', depends_on: ['evidence'] }

function replay(message, event, data) {
  applyRunEvent(message, {
    event, data, run_id: 'team-run', trace_id: 'team-run',
    sequence: (message.lastSeq ?? 0) + 1, timestamp: '2026-11-21T12:00:00Z',
  })
}

test('团队进度沿原执行块更新，从回放保留分工、依赖和角色成果', () => {
  const message = { blocks: [] }
  replay(message, 'team.updated', { phase: 'planning', lead: '团队负责人', tasks: [] })
  replay(message, 'team.updated', {
    phase: 'working', lead: '团队负责人', tasks: [{ ...evidence, status: 'running' }, { ...design, status: 'pending' }],
  })
  replay(message, 'team.updated', {
    phase: 'completed', lead: '团队负责人',
    tasks: [{ ...evidence, status: 'completed', result: '证据已整理' }, { ...design, status: 'completed', result: '分三期实施' }],
  })
  replay(message, 'assistant.delta', { text: '综合后的答复' })
  replay(message, 'run.completed', { final_text: '综合后的答复' })
  finalizeRunMessage(message, 'run.completed')

  assert.deepEqual(message.blocks.map((block) => block.kind), ['team', 'text'])
  const { phase, lead, tasks } = message.blocks[0].progress
  assert.equal(phase, 'completed')
  assert.equal(lead, '团队负责人')
  assert.deepEqual(tasks[1].depends_on, ['evidence'])
  assert.deepEqual(tasks.map((task) => task.result), ['证据已整理', '分三期实施'])
  assert.equal(message.blocks[1].text, '综合后的答复')
})

test('取消或失败后团队卡保留已完成的成果，收口活跃角色状态', () => {
  for (const [reason, phase, unfinished] of [['run.cancelled', 'cancelled', 'cancelled'], ['run.failed', 'failed', 'skipped']]) {
    const message = { blocks: [] }
    replay(message, 'team.updated', {
      phase: 'working', tasks: [
        { ...evidence, status: 'completed', result: '可回查的证据' },
        { ...design, status: 'running' },
        { ...design, id: 'review', status: 'pending', depends_on: ['design'] },
      ],
    })
    finalizeRunMessage(message, reason)
    const team = message.blocks[0].progress
    assert.equal(team.phase, phase)
    assert.deepEqual(team.tasks.map(task => task.status), ['completed', phase, unfinished])
    assert.equal(team.tasks[0].result, '可回查的证据')
  }
})

test('异常快照不会覆盖已展示的有效团队进度', () => {
  const message = { blocks: [] }
  replay(message, 'team.updated', { phase: 'working', tasks: [{ ...evidence, status: 'running' }] })
  replay(message, 'team.updated', { phase: 'completed', tasks: [{ ...evidence, status: 'unknown' }] })
  assert.equal(message.blocks[0].progress.phase, 'working')
  assert.equal(message.blocks[0].progress.tasks[0].status, 'running')
})

test('独立复核及返工状态复用团队任务板，保留复核发现', () => {
  const message = { blocks: [] }
  replay(message, 'team.updated', { phase: 'verifying', lead: '团队负责人', tasks: [{ ...evidence, status: 'completed' }] })
  assert.equal(message.liveStatus, '团队正在复核成果')
  replay(message, 'team.updated', {
    phase: 'repairing', tasks: [{ ...evidence, status: 'running' }], summary: '补充来源并核实证据。',
  })
  assert.equal(message.blocks.length, 1)
  assert.equal(message.blocks[0].progress.phase, 'repairing')
  assert.equal(message.blocks[0].progress.summary, '补充来源并核实证据。')
})
