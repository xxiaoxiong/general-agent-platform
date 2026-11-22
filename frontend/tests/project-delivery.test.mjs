import assert from 'node:assert/strict'
import test from 'node:test'
import { applyRunEvent, finalizeRunMessage } from '../src/features/run/blocks.ts'
import { describeProjectScope, readProjectDelivery } from '../src/features/run/projectDelivery.ts'

const passed = {
  title: '现场工单台', filename: 'work-orders.zip', download_url: '/api/v2/artifacts/MTp0ZWFtLXdvcms',
  size: 7231, file_count: 12, state: 'passed',
  checks: [
    { id: 'api', label: '接口契约', status: 'passed', detail: '列出的 4 个端点均有实现' },
    { id: 'runtime', label: '运行检查', status: 'skipped', detail: '当前环境未启动项目' },
  ],
  verification_scope: 'static',
}

function replay(message, event, data) {
  applyRunEvent(message, { event, data, run_id: 'team-project', trace_id: 'team-project',
    sequence: (message.lastSeq ?? 0) + 1, timestamp: '2026-11-21T12:00:00Z' })
}

test('项目过程与交付回放共享一个卡片，保存真实产物和验收范围', () => {
  const message = { blocks: [] }
  replay(message, 'project.progress', { phase: 'building', detail: '实现前端和服务端' })
  replay(message, 'project.progress', {
    phase: 'repairing', title: '现场工单台', detail: '修正列表查询参数', completed_files: 10, total_files: 12,
  })
  assert.equal(message.blocks[0].progress.phase, 'repairing')
  assert.equal(message.blocks[0].progress.title, '现场工单台')
  assert.deepEqual(message.blocks[0].progress.files, { written: 10, total: 12 })
  assert.equal(message.liveStatus, '正在修复验收问题')

  replay(message, 'project.delivery', passed)
  replay(message, 'project.delivery', passed)
  replay(message, 'project.progress', { phase: 'packaging' })
  replay(message, 'run.completed', {})
  finalizeRunMessage(message, 'run.completed')
  assert.equal(message.blocks.length, 1)
  assert.equal(message.blocks[0].kind, 'project')
  assert.deepEqual(message.blocks[0].delivery, passed)
  assert.equal(describeProjectScope(message.blocks[0].delivery.verification_scope), '静态检查，未启动项目')
})

test('失败终态仍提供草稿，即使异常事件声称检查通过', () => {
  const message = { blocks: [] }
  const checks = [{ id: 'entrypoint', label: '启动入口', status: 'failed', detail: '前端未实现请求' }]
  replay(message, 'project.delivery', { ...passed, checks })
  replay(message, 'run.failed', { error: '项目验收未通过' })
  finalizeRunMessage(message, 'run.failed')
  assert.equal(message.runStatus, 'failed')
  assert.equal(message.blocks[0].delivery.state, 'failed')
  assert.deepEqual(message.blocks[0].delivery.checks, checks)
})

test('交付卡按实际运行的浏览器操作场景显示通过数量', () => {
  const checks = [
    { id: 'runtime.browser', label: '页面加载', status: 'passed' },
    { id: 'runtime.browser.flow.1', label: '创建后刷新', status: 'passed' },
    { id: 'runtime.browser.flow.2', label: '表单错误提示', status: 'failed' },
  ]
  assert.equal(describeProjectScope('sandbox', checks), '浏览器操作场景 1 / 2 通过 · 隔离运行')
  assert.equal(describeProjectScope('sandbox', checks.slice(0, 1)), '隔离环境中的运行检查')
})

test('项目交付拒绝外部、非产物、路径穿越或不可识别的文件元数据', () => {
  for (const download_url of ['//other.test/api/v2/artifacts/abc', '/api/v2/artifacts/../run',
    '/api/v2/artifacts/x?other=1', '/api/v2/runs/x/project']) {
    assert.equal(readProjectDelivery({ ...passed, download_url }), undefined)
  }
  assert.equal(readProjectDelivery({ ...passed, filename: '../something.zip' }), undefined)
  assert.equal(readProjectDelivery({ ...passed, file_count: 0 }), undefined)
  assert.equal(readProjectDelivery({ ...passed, checks: [passed.checks[0], passed.checks[0]] }), undefined)
})
