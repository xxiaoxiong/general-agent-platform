import assert from 'node:assert/strict'
import test from 'node:test'
import { applyRunEvent, finalizeRunMessage } from '../src/features/run/blocks.ts'

function replay(message, event, data) {
  applyRunEvent(message, {
    event, data, run_id: 'work-run', trace_id: 'work-run',
    sequence: (message.lastSeq ?? 0) + 1, timestamp: '2026-11-27T10:00:00Z',
  })
}

const steps = [
  {
    id: 'research', title: '资料检索', executor_profile: 'research',
    depends_on: [], status: 'pending', artifact_count: 0,
  },
  {
    id: 'document', title: '生成 Word 报告', executor_profile: 'document_docx',
    depends_on: ['research'], status: 'pending', artifact_count: 0,
  },
]

test('工作计划事件沿同一张卡片更新，步骤依赖与验收状态可回放', () => {
  const message = { blocks: [] }
  replay(message, 'work.plan.created', {
    plan_id: 'p1', revision: 1, status: 'ready', mode: 'auto',
    objective: '调研并生成 Word 报告', steps,
  })
  replay(message, 'work.step.started', {
    plan_id: 'p1', revision: 1, step_id: 'research', title: '资料检索',
    executor_profile: 'research', depends_on: [], status: 'running',
  })
  replay(message, 'work.step.completed', {
    plan_id: 'p1', revision: 1, step_id: 'research', title: '资料检索',
    executor_profile: 'research', status: 'completed', summary: '已核对 3 个来源',
    artifact_refs: [],
  })
  replay(message, 'work.step.started', {
    plan_id: 'p1', revision: 1, step_id: 'document', title: '生成 Word 报告',
    executor_profile: 'document_docx', depends_on: ['research'], status: 'running',
  })
  replay(message, 'work.step.completed', {
    plan_id: 'p1', revision: 1, step_id: 'document', title: '生成 Word 报告',
    executor_profile: 'document_docx', status: 'completed',
    artifact_refs: [{ path: 'document:1:1', name: 'report.docx' }],
  })
  replay(message, 'work.plan.completed', {
    plan_id: 'p1', revision: 1, status: 'completed', mode: 'auto',
    objective: '调研并生成 Word 报告', step_count: 2, completed_count: 2,
    steps, verification: { status: 'passed', checks: [{ id: 'artifacts_present', passed: true, detail: '' }] },
  })
  finalizeRunMessage(message, 'run.completed')

  assert.equal(message.blocks.filter((block) => block.kind === 'work_plan').length, 1)
  const plan = message.blocks[0].plan
  assert.equal(plan.status, 'completed')
  assert.equal(plan.completedCount, 2)
  assert.deepEqual(plan.steps.map((step) => step.status), ['completed', 'completed'])
  assert.equal(plan.steps[0].summary, '已核对 3 个来源')
  assert.deepEqual(plan.steps[1].dependsOn, ['research'])
  assert.equal(plan.steps[1].artifactCount, 1)
  assert.equal(plan.verification.status, 'passed')
})

test('DeepAgents Todo plan.updated 与 work.plan.* 互不覆盖', () => {
  const message = { blocks: [] }
  replay(message, 'plan.updated', { todos: [{ content: '读取附件', status: 'completed' }] })
  replay(message, 'work.plan.created', {
    plan_id: 'p2', revision: 1, status: 'ready', mode: 'auto',
    objective: '复合目标', steps,
  })
  replay(message, 'work.step.failed', {
    plan_id: 'p2', revision: 1, step_id: 'research', title: '资料检索',
    status: 'failed', message: '检索工具不可用', error_code: 'step_execution_error', retryable: false,
  })
  replay(message, 'plan.updated', { todos: [{ content: '重试检索', status: 'in_progress' }] })

  // DeepAgents Todo 与 WorkPlan 各自成块：后到的 plan.updated 只能在执行位置新增
  // Todo 块，不能覆盖工作计划卡片的步骤状态。
  assert.deepEqual(message.blocks.map((block) => block.kind), ['plan', 'work_plan', 'plan'])
  const workPlan = message.blocks.find((block) => block.kind === 'work_plan')
  assert.equal(workPlan.plan.steps[0].status, 'failed')
  assert.equal(workPlan.plan.steps[0].summary, '检索工具不可用')
  assert.equal(message.blocks[0].todos[0].content, '读取附件')
  assert.equal(message.blocks[2].todos[0].content, '重试检索')
})

test('计划降级或运行失败时，计划卡如实收口，不残留执行中', () => {
  const message = { blocks: [] }
  replay(message, 'work.plan.degraded', { reason: '规划模型超时', mode: 'shadow' })
  assert.equal(message.blocks[0].kind, 'work_plan')
  assert.equal(message.blocks[0].plan.status, 'degraded')
  assert.match(message.blocks[0].plan.degradedReason, /超时/)

  const failed = { blocks: [] }
  replay(failed, 'work.plan.created', {
    plan_id: 'p3', revision: 1, status: 'running', mode: 'auto',
    objective: '复合目标', steps,
  })
  replay(failed, 'work.step.started', {
    plan_id: 'p3', revision: 1, step_id: 'research', title: '资料检索', status: 'running',
  })
  finalizeRunMessage(failed, 'run.failed')
  const plan = failed.blocks[0].plan
  assert.equal(plan.status, 'failed')
  assert.equal(plan.steps[0].status, 'failed')
  assert.equal(plan.steps[1].status, 'pending')
})
