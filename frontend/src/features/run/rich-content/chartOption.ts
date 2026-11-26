import type { EChartsCoreOption } from 'echarts/core'
import type { ChartSpec } from './types.ts'

const PALETTE = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16']

/**
 * 将受限 ChartSpec 映射为内部 ECharts Option。
 * 安全：模型不能提供 Option / 函数 / formatter；格式化函数全部由前端生成，
 * 且 tooltip 使用 richText 模式，避免 HTML 注入。
 */
export function buildChartOption(spec: ChartSpec, dark: boolean): EChartsCoreOption {
  const text = dark ? '#cbd5e1' : '#334155'
  const muted = dark ? '#94a3b8' : '#64748b'
  const line = dark ? 'rgba(148, 163, 184, .22)' : 'rgba(100, 116, 139, .18)'
  const tooltipBg = dark ? '#1e293b' : '#ffffff'

  const shared = {
    backgroundColor: 'transparent',
    color: PALETTE,
    animationDuration: 320,
    textStyle: { color: text, fontFamily: 'inherit' },
    tooltip: {
      trigger: spec.type === 'pie' ? 'item' : 'axis',
      renderMode: 'richText',
      backgroundColor: tooltipBg,
      borderColor: line,
      textStyle: { color: text, fontSize: 12 },
    },
    legend: {
      show: spec.type === 'pie' || spec.series.length > 1,
      bottom: 0,
      type: 'scroll',
      icon: 'roundRect',
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: muted, fontSize: 11 },
    },
  }

  if (spec.type === 'pie') {
    const current = spec.series[0]
    return {
      ...shared,
      tooltip: {
        ...shared.tooltip,
        formatter: (params: { name?: string; value?: number; percent?: number }) =>
          `${params.name ?? ''}：${params.value ?? 0}${spec.unit || ''}（${params.percent ?? 0}%）`,
      },
      series: [{
        name: current.name,
        type: 'pie',
        radius: ['38%', '68%'],
        center: ['50%', '50%'],
        data: spec.labels.map((label, index) => ({ name: label, value: current.data[index] })),
        itemStyle: { borderColor: dark ? '#0f172a' : '#ffffff', borderWidth: 2 },
        label: { color: muted, fontSize: 11 },
        emphasis: { scale: true, scaleSize: 4 },
      }],
    }
  }

  const axis = {
    axisLine: { lineStyle: { color: line } },
    axisTick: { show: false },
    axisLabel: { color: muted, fontSize: 11 },
    splitLine: { lineStyle: { color: line } },
  }

  return {
    ...shared,
    grid: { left: 12, right: 20, top: 20, bottom: 34, containLabel: true },
    xAxis: { type: 'category', data: spec.labels, boundaryGap: spec.type !== 'line' && spec.type !== 'area', ...axis, splitLine: { show: false } },
    yAxis: {
      type: 'value',
      ...axis,
    },
    tooltip: {
      ...shared.tooltip,
      valueFormatter: (value: unknown) => `${value}${spec.unit || ''}`,
    },
    series: spec.series.map(item => {
      if (spec.type === 'line' || spec.type === 'area') {
        return {
          name: item.name,
          type: 'line',
          data: item.data,
          smooth: true,
          showSymbol: spec.labels.length <= 30,
          symbolSize: 6,
          lineStyle: { width: 2 },
          areaStyle: spec.type === 'area' ? { opacity: 0.18 } : undefined,
          emphasis: { focus: 'series' },
        }
      }
      return {
        name: item.name,
        type: 'bar',
        data: item.data,
        stack: spec.type === 'stacked_bar' ? 'total' : undefined,
        barMaxWidth: 42,
        itemStyle: { borderRadius: spec.type === 'stacked_bar' ? 0 : [4, 4, 0, 0] },
        emphasis: { focus: 'series' },
      }
    }),
  }
}
