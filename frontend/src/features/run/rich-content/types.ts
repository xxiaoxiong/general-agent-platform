/**
 * 富内容渲染类型（任务书 §4/§5）：
 * - Mermaid：流程图、时序图等，使用 ```mermaid 代码围栏；
 * - Chart：受限 ChartSpec JSON，使用 ```chart 代码围栏，不直接暴露 ECharts Option。
 */

export type RichKind = 'mermaid' | 'chart'

/** 一条消息中待挂载的富内容块（源码不进入 HTML，仅保存在内存中） */
export interface RichEmbed {
  index: number
  kind: RichKind
  source: string
}

export type ChartType = 'bar' | 'line' | 'area' | 'pie' | 'stacked_bar'

export interface ChartSeries {
  name: string
  data: number[]
}

/** 模型可输出的受限图表协议（白名单字段，未知字段一律拒绝） */
export interface ChartSpec {
  type: ChartType
  title?: string
  labels: string[]
  series: ChartSeries[]
  unit?: string
}

export interface ChartValidationError {
  ok: false
  message: string
}

export interface ChartValidationSuccess {
  ok: true
  spec: ChartSpec
}

export type ChartValidationResult = ChartValidationSuccess | ChartValidationError

/** 协议上限：限制模型输出复杂度，避免阻塞主线程与内存膨胀 */
export const CHART_LIMITS = {
  maxSeries: 8,
  maxPoints: 200,
  maxLabelLength: 64,
  maxTitleLength: 120,
  maxSeriesNameLength: 40,
  maxUnitLength: 16,
  maxSourceLength: 20_000,
} as const

export const CHART_TYPES: readonly ChartType[] = ['bar', 'line', 'area', 'pie', 'stacked_bar']
