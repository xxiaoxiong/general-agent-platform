import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { edgeEndpoints, inputFields, moveNode, nodePosition, replaceGraph } from '../src/views/workflows/workflowGraph.ts'

test('旧连线和新连线与引擎使用相同的端点优先级', () => {
  assert.deepEqual(edgeEndpoints({ from: 'start', to: 'end' }), { source: 'start', target: 'end' })
  assert.deepEqual(edgeEndpoints({ source: 'start', target: 'end' }), { source: 'start', target: 'end' })
  assert.deepEqual(
    edgeEndpoints({ from: 'start', to: 'yes', source: 'other', target: 'no' }),
    { source: 'start', target: 'yes' },
  )
})

test('移动旧节点保留配置和扩展字段并同步已有坐标字段', () => {
  const original = { id: 'a', type: 'start', x: 10, y: 20, config: { custom: true }, annotation: '草稿' }
  const position = { x: -24, y: 60 }
  assert.deepEqual(nodePosition(original), { x: 10, y: 20 })
  assert.deepEqual(moveNode(original, position), { ...original, x: -24, y: 60, position })
  assert.equal(original.x, 10)
})

test('保存时沿用定义扩展字段和条件分支的顺序', () => {
  const nodes = [{ id: 'condition', type: 'condition' }]
  const edges = [{ id: 'legacy', from: 'condition', to: 'yes', custom: true }, { source: 'condition', target: 'no' }]
  const definition = replaceGraph({ nodes: [], edges: [], flags: { archived: false } }, nodes, edges)
  assert.deepEqual(definition, { nodes, edges, flags: { archived: false } })
  assert.equal(definition.edges[0].from, 'condition')
  assert.equal(definition.edges[0].target, undefined)
})

test('运行参数优先读取开始节点定义，其次按出现顺序推断变量', () => {
  const nodes = [
    { id: 'start', type: 'start' },
    { id: 'chat', type: 'ai_chat', config: { prompt: '{{question}} / {{topic}} / {{question}}' } },
  ]
  assert.deepEqual(inputFields(nodes).map(field => field.key), ['question', 'topic'])
  nodes[0].config = { input_fields: [{ key: 'message', label: '消息', type: 'textarea' }] }
  assert.deepEqual(inputFields(nodes), nodes[0].config.input_fields)
})
