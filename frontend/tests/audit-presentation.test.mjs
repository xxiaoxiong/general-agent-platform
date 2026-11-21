import test from 'node:test'
import assert from 'node:assert/strict'
import { auditAction, auditActor, auditFields, auditStatus, formatAuditValue } from '../src/views/system/auditPresentation.ts'

const log = { id: 1, action: 'update_conversation', module: 'agent', status: 'success', created_at: '2026-11-21T02:00:00Z' }

test('把复杂对象的键序差异视为相同，并保留审计记录标注的隐藏字段', () => {
  const detail = { ...log,
    before_data: { title: '旧名称', nested: { enabled: true, versions: [1, 2] }, api_key: '（内容已隐藏）' },
    after_data: { title: '新名称', nested: { versions: [1, 2], enabled: true }, api_key: '（内容已隐藏）' },
    changed_fields: ['title', 'api_key'],
  }
  assert.deepEqual(auditFields(detail, true).map((item) => item.field), ['title', 'api_key'])
  assert.equal(auditFields(detail, false).find((item) => item.field === 'nested').changed, false)
  assert.equal(auditFields(detail, true)[1].to, '（内容已隐藏）')
})

test('区分新增或删除的一侧、显式空值与零值', () => {
  const created = auditFields({ ...log, before_data: null, after_data: { limit: 0, mode: null } }, true)
  assert.deepEqual(created.map((entry) => [entry.field, formatAuditValue(entry.from), formatAuditValue(entry.to)]), [
    ['limit', '未记录', '0'], ['mode', '未记录', 'null'],
  ])
  const deleted = auditFields({ ...log, before_data: { limit: 0 }, after_data: null }, true)
  assert.deepEqual(deleted, [{ field: 'limit', from: 0, to: undefined, changed: true }])
  assert.deepEqual(auditFields(log, true), [])
})

test('数组顺序变化和嵌套值变化会比对，未知状态保留原值', () => {
  const detail = { ...log, before_data: { nodes: [1, 2] }, after_data: { nodes: [2, 1] } }
  assert.equal(auditFields(detail, true)[0].field, 'nodes')
  assert.equal(formatAuditValue({ enabled: false }), '{\n  "enabled": false\n}')
  assert.equal(auditActor({ ...log, event_type: 'request' }), '未登录')
  assert.deepEqual(auditStatus('denied'), { text: '拒绝', tone: 'warning' })
  assert.equal(auditStatus('future').text, 'future')
  assert.equal(auditAction('constructor'), 'constructor')
  assert.equal(auditAction('update', 'agent_sessions'), '更新会话')
  assert.equal(auditAction('create_document_versions'), '创建文档版本')
})
