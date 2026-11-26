import assert from 'node:assert/strict'
import test from 'node:test'
import { detectRichKind, fenceLanguage } from '../src/features/run/rich-content/registry.ts'
import { renderMessageDocument, renderMessageMarkdown } from '../src/features/run/messageMarkdown.ts'
import { validateChartSpec } from '../src/features/run/rich-content/validateChartSpec.ts'
import { buildChartOption } from '../src/features/run/rich-content/chartOption.ts'

const MERMAID = 'flowchart TD\n  A[开始] --> B{校验}\n  B -->|是| C[完成]'
const CHART = JSON.stringify({
  type: 'bar',
  title: '月度收入',
  labels: ['1月', '2月', '3月'],
  series: [{ name: '收入', data: [120, 150, 135] }],
  unit: '万元',
})

test('围栏语言识别：只命中明确的富内容别名', () => {
  assert.equal(fenceLanguage('Mermaid title=x'), 'mermaid')
  assert.equal(detectRichKind('mermaid'), 'mermaid')
  assert.equal(detectRichKind('mmd'), 'mermaid')
  assert.equal(detectRichKind('MermaidJS'), 'mermaid')
  assert.equal(detectRichKind('chart'), 'chart')
  assert.equal(detectRichKind('javascript'), null)
  assert.equal(detectRichKind(''), null)
  assert.equal(detectRichKind(undefined), null)
})

test('非流式渲染：富内容生成占位节点，源码不进入 HTML', () => {
  const doc = renderMessageDocument('前文\n\n```mermaid\n' + MERMAID + '\n```\n\n后文')
  assert.equal(doc.embeds.length, 1)
  assert.equal(doc.embeds[0].kind, 'mermaid')
  assert.equal(doc.embeds[0].source.trim(), MERMAID)
  assert.match(doc.html, /data-embed-index="0"/)
  assert.match(doc.html, /data-rich-kind="mermaid"/)
  assert.match(doc.html, /<p>前文<\/p>/)
  assert.match(doc.html, /<p>后文<\/p>/)
  assert.ok(!doc.html.includes('flowchart TD'), '源码不能写入 HTML 属性')
})

test('流式渲染：富内容显示生成中占位，不挂载、不展示半成品源码', () => {
  const doc = renderMessageDocument('```chart\n{"type":"bar"', { streaming: true })
  assert.equal(doc.embeds.length, 0)
  assert.match(doc.html, /rich-pending/)
  assert.match(doc.html, /图表生成中/)
  assert.ok(!doc.html.includes('"type":"bar"'))
})

test('列表与引用块中的围栏同样识别为富内容', () => {
  const doc = renderMessageDocument('1. 步骤\n\n   ```mermaid\n   flowchart LR\n     A --> B\n   ```')
  assert.equal(doc.embeds.length, 1)
  assert.match(doc.html, /data-embed-index="0"/)
  const quote = renderMessageDocument('> ```chart\n> ' + CHART + '\n> ```')
  assert.equal(quote.embeds.length, 1)
  assert.equal(quote.embeds[0].kind, 'chart')
})

test('普通代码块与纯文本 Markdown 行为不变', () => {
  const doc = renderMessageDocument('```python\nprint("hi")\n```')
  assert.equal(doc.embeds.length, 0)
  assert.match(doc.html, /class="code-block"/)
  assert.match(doc.html, /class="copy-code"/)
  assert.match(renderMessageMarkdown('| a | b |\n|---|---|\n| 1 | 2 |'), /table-scroll/)
})

test('allowRich=false 时 mermaid 围栏保持普通代码块（回归兼容）', () => {
  const html = renderMessageMarkdown('```mermaid\n' + MERMAID + '\n```')
  assert.match(html, /class="code-block"/)
  assert.ok(!html.includes('rich-embed'))
})

test('大表格限高滚动且不丢行，小表格不加类', () => {
  const rows = Array.from({ length: 50 }, (_, i) => `| ${i} | 行${i} |`).join('\n')
  const large = renderMessageDocument(`| 序号 | 内容 |\n|---|---|\n${rows}`)
  assert.match(large.html, /table-scroll table-scroll--large/)
  assert.equal((large.html.match(/<tr>/g) || []).length, 51)
  const small = renderMessageDocument('| a | b |\n|---|---|\n| 1 | 2 |')
  assert.ok(!small.html.includes('table-scroll--large'))
})

test('ChartSpec 校验：合法协议通过并保留白名单字段', () => {
  const result = validateChartSpec(CHART)
  assert.equal(result.ok, true)
  if (result.ok) {
    assert.equal(result.spec.type, 'bar')
    assert.equal(result.spec.title, '月度收入')
    assert.equal(result.spec.unit, '万元')
    assert.deepEqual(result.spec.series[0].data, [120, 150, 135])
  }
})

test('ChartSpec 校验：拒绝非法 JSON、未知类型与 ECharts Option 字段', () => {
  assert.equal(validateChartSpec('').ok, false)
  assert.equal(validateChartSpec('not-json').ok, false)
  assert.equal(validateChartSpec(JSON.stringify({ type: 'radar', labels: ['a'], series: [{ name: 's', data: [1] }] })).ok, false)
  const echartsOption = validateChartSpec(JSON.stringify({
    type: 'bar', labels: ['a'], series: [{ name: 's', data: [1] }],
    xAxis: { type: 'category' },
    tooltip: { formatter: () => 'x' },
  }))
  assert.equal(echartsOption.ok, false)
  assert.match(echartsOption.message, /不支持的字段/)
})

test('ChartSpec 校验：长度不匹配、非法数值、饼图多系列与超限均拒绝', () => {
  assert.equal(validateChartSpec(JSON.stringify({ type: 'bar', labels: ['a', 'b'], series: [{ name: 's', data: [1] }] })).ok, false)
  assert.equal(validateChartSpec(JSON.stringify({ type: 'bar', labels: ['a'], series: [{ name: 's', data: ['1'] }] })).ok, false)
  assert.equal(validateChartSpec(JSON.stringify({ type: 'pie', labels: ['a'], series: [{ name: 's1', data: [1] }, { name: 's2', data: [2] }] })).ok, false)
  const tooMany = validateChartSpec(JSON.stringify({
    type: 'line', labels: Array.from({ length: 201 }, (_, i) => String(i)),
    series: [{ name: 's', data: Array.from({ length: 201 }, () => 1) }],
  }))
  assert.equal(tooMany.ok, false)
  const longTitle = validateChartSpec(JSON.stringify({
    type: 'bar', title: 'x'.repeat(121), labels: ['a'], series: [{ name: 's', data: [1] }],
  }))
  assert.equal(longTitle.ok, false)
  const oversized = validateChartSpec(JSON.stringify({
    type: 'bar', labels: ['a'], series: [{ name: 's', data: [1] }], note: 'x'.repeat(20000),
  }))
  assert.equal(oversized.ok, false)
  assert.match(oversized.message, /字符上限/)
})

test('ChartSpec 校验：空数据允许通过，由前端显示暂无数据', () => {
  const empty = validateChartSpec(JSON.stringify({ type: 'bar', labels: [], series: [] }))
  assert.equal(empty.ok, true)
  if (empty.ok) assert.deepEqual(empty.spec.labels, [])
  assert.equal(validateChartSpec(JSON.stringify({ type: 'pie', labels: [], series: [] })).ok, true)
  // 有标签但数据点为空仍是不匹配，必须拒绝
  assert.equal(validateChartSpec(JSON.stringify({
    type: 'bar', labels: ['a'], series: [{ name: 's', data: [] }],
  })).ok, false)
})

test('ChartSpec 校验：负数、零与小数是合法数值', () => {
  const result = validateChartSpec(JSON.stringify({
    type: 'line', labels: ['A', 'B', 'C'], series: [{ name: 's', data: [-1.5, 0, 2.25] }],
  }))
  assert.equal(result.ok, true)
  if (result.ok) assert.deepEqual(result.spec.series[0].data, [-1.5, 0, 2.25])
})

test('Option 映射：堆叠柱、面积、折线与饼图按协议生成', () => {
  const base = { labels: ['A', 'B'], series: [{ name: 'S1', data: [1, 2] }, { name: 'S2', data: [3, 4] }] }
  const stacked = buildChartOption({ type: 'stacked_bar', ...base }, false)
  const stackedSeries = stacked.series
  assert.ok(Array.isArray(stackedSeries))
  assert.equal(stackedSeries[0].stack, 'total')
  assert.equal(stackedSeries[0].type, 'bar')

  const area = buildChartOption({ type: 'area', ...base }, true)
  assert.equal(area.series[0].type, 'line')
  assert.deepEqual(area.series[0].areaStyle, { opacity: 0.18 })

  const line = buildChartOption({ type: 'line', ...base }, false)
  assert.equal(line.series[0].areaStyle, undefined)

  const pie = buildChartOption({ type: 'pie', unit: '万元', labels: ['A', 'B'], series: [{ name: '占比', data: [30, 70] }] }, false)
  assert.equal(pie.xAxis, undefined)
  assert.equal(pie.series[0].data[0].name, 'A')
  assert.equal(pie.series[0].data[1].value, 70)
  assert.equal(pie.tooltip.renderMode, 'richText')
})

test('Option 映射：tooltip 单位由前端函数拼接，不接受模型 formatter', () => {
  const option = buildChartOption({
    type: 'bar', labels: ['A'], series: [{ name: 'S', data: [5] }], unit: '万元',
  }, false)
  assert.equal(typeof option.tooltip.valueFormatter, 'function')
  assert.equal(option.tooltip.valueFormatter(5), '5万元')
  // 单位由卡片工具栏展示，坐标轴不再旋转塞入单位名称
  assert.equal(option.yAxis.name, undefined)
})
