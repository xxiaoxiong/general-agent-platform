import {
  CHART_LIMITS, CHART_TYPES,
  type ChartSeries, type ChartSpec, type ChartType, type ChartValidationResult,
} from './types.ts'

const SPEC_KEYS = new Set(['type', 'title', 'labels', 'series', 'unit'])
const SERIES_KEYS = new Set(['name', 'data'])

/** 去掉控制字符，避免不可见字符影响坐标轴与表格展示 */
function cleanText(value: string): string {
  return value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '').trim()
}

function fail(message: string): ChartValidationResult {
  return { ok: false, message }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 校验模型输出的 ChartSpec。
 * 安全边界：只接受白名单字段与有限数值，拒绝函数、HTML、任意 ECharts Option。
 */
export function validateChartSpec(source: string): ChartValidationResult {
  if (!source.trim()) return fail('图表数据为空')
  if (source.length > CHART_LIMITS.maxSourceLength) return fail(`图表数据超过 ${CHART_LIMITS.maxSourceLength} 字符上限`)

  let raw: unknown
  try {
    raw = JSON.parse(source)
  } catch (error) {
    return fail(`JSON 解析失败：${error instanceof Error ? error.message : String(error)}`)
  }
  if (!isPlainObject(raw)) return fail('图表数据必须是 JSON 对象')

  const unknownKeys = Object.keys(raw).filter(key => !SPEC_KEYS.has(key))
  if (unknownKeys.length) return fail(`不支持的字段：${unknownKeys.join('、')}（不允许直接输出 ECharts Option）`)

  if (typeof raw.type !== 'string' || !CHART_TYPES.includes(raw.type as ChartType)) {
    return fail(`type 必须是 ${CHART_TYPES.join(' / ')} 之一`)
  }
  const type = raw.type as ChartType

  if (raw.title !== undefined && typeof raw.title !== 'string') return fail('title 必须是字符串')
  if (raw.unit !== undefined && typeof raw.unit !== 'string') return fail('unit 必须是字符串')
  const title = typeof raw.title === 'string' ? cleanText(raw.title) : ''
  const unit = typeof raw.unit === 'string' ? cleanText(raw.unit) : ''
  if (title.length > CHART_LIMITS.maxTitleLength) return fail(`title 超过 ${CHART_LIMITS.maxTitleLength} 字符上限`)
  if (unit.length > CHART_LIMITS.maxUnitLength) return fail(`unit 超过 ${CHART_LIMITS.maxUnitLength} 字符上限`)

  if (!Array.isArray(raw.labels)) return fail('labels 必须是数组')
  if (raw.labels.length > CHART_LIMITS.maxPoints) return fail(`数据点超过 ${CHART_LIMITS.maxPoints} 个上限`)
  const labels: string[] = []
  for (const label of raw.labels) {
    if (typeof label !== 'string' && typeof label !== 'number') return fail('labels 只能包含字符串或数字')
    const text = cleanText(String(label))
    if (text.length > CHART_LIMITS.maxLabelLength) return fail(`单个标签超过 ${CHART_LIMITS.maxLabelLength} 字符上限`)
    labels.push(text)
  }

  if (!Array.isArray(raw.series)) return fail('series 必须是数组')
  if (raw.series.length > CHART_LIMITS.maxSeries) return fail(`系列数超过 ${CHART_LIMITS.maxSeries} 个上限`)
  // 空数据允许通过，由前端显示「暂无数据」空状态；饼图非空时只允许一个系列
  if (type === 'pie' && raw.series.length > 1) return fail('饼图只支持单系列数据')

  const series: ChartSeries[] = []
  for (const [index, item] of raw.series.entries()) {
    if (!isPlainObject(item)) return fail(`第 ${index + 1} 个系列必须是对象`)
    const extra = Object.keys(item).filter(key => !SERIES_KEYS.has(key))
    if (extra.length) return fail(`第 ${index + 1} 个系列包含不支持的字段：${extra.join('、')}`)
    if (typeof item.name !== 'string' || !cleanText(item.name)) return fail(`第 ${index + 1} 个系列缺少 name`)
    const name = cleanText(item.name)
    if (name.length > CHART_LIMITS.maxSeriesNameLength) return fail(`系列名称超过 ${CHART_LIMITS.maxSeriesNameLength} 字符上限`)
    if (!Array.isArray(item.data)) return fail(`系列「${name}」的 data 必须是数组`)
    if (item.data.length !== labels.length) {
      return fail(`系列「${name}」的数据点（${item.data.length}）与 labels（${labels.length}）数量不一致`)
    }
    const data: number[] = []
    for (const value of item.data) {
      if (typeof value !== 'number' || !Number.isFinite(value)) {
        return fail(`系列「${name}」包含非有限数值，只接受数字`)
      }
      data.push(value)
    }
    series.push({ name, data })
  }

  const spec: ChartSpec = { type, labels, series }
  if (title) spec.title = title
  if (unit) spec.unit = unit
  return { ok: true, spec }
}
