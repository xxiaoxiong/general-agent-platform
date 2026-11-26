<template>
  <div class="rich-card chart-card">
    <header class="rich-toolbar">
      <span class="rich-label">
        {{ spec?.title || '数据图表' }}
        <span v-if="spec?.unit" class="rich-unit">{{ spec.unit }}</span>
      </span>
      <div class="rich-actions">
        <button v-for="tab in tabs" :key="tab.key" type="button" class="rich-btn"
          :class="{ 'is-active': view === tab.key }" @click="view = tab.key">{{ tab.label }}</button>
        <button type="button" class="rich-btn" @click="copySource">{{ copyLabel }}</button>
      </div>
    </header>

    <div v-if="isInvalid || state === 'error'" class="rich-state is-error" role="alert">
      <CircleAlert :size="14" /><span>图表渲染失败：{{ displayError }}</span>
    </div>

    <div class="chart-body">
      <div v-show="!isInvalid && !isEmpty && state !== 'error' && view === 'chart'" class="chart-canvas-wrap">
        <div ref="chartEl" class="chart-canvas"></div>
        <div v-if="state === 'loading'" class="chart-loading"><span class="rich-spinner" aria-hidden="true" /><span>图表渲染中…</span></div>
      </div>

      <div v-if="view === 'chart' && isEmpty" class="chart-empty">暂无数据</div>

      <div v-if="view === 'data' && spec" class="chart-data">
        <div v-if="isEmpty" class="chart-empty">暂无数据</div>
        <table v-else>
          <thead>
            <tr>
              <th scope="col">标签</th>
              <th v-for="item in spec.series" :key="item.name" scope="col">
                {{ item.name }}<template v-if="spec.unit">（{{ spec.unit }}）</template>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(label, rowIndex) in spec.labels" :key="rowIndex">
              <td>{{ label }}</td>
              <td v-for="item in spec.series" :key="item.name">{{ item.data[rowIndex] }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="view === 'source' || isInvalid || state === 'error'" class="rich-source">
        <pre><code>{{ code }}</code></pre>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CircleAlert } from '@lucide/vue'
import type { ECharts } from 'echarts/core'
import { buildChartOption } from './chartOption'
import { validateChartSpec } from './validateChartSpec'

const props = defineProps<{ code: string; theme?: 'light' | 'dark' }>()

const tabs = [
  { key: 'chart', label: '图表' },
  { key: 'data', label: '数据' },
  { key: 'source', label: '源码' },
] as const

const view = ref<'chart' | 'data' | 'source'>('chart')
const state = ref<'loading' | 'ready' | 'error'>('loading')
const error = ref('')
const copyLabel = ref('复制源码')
const chartEl = ref<HTMLDivElement | null>(null)

const validation = computed(() => validateChartSpec(props.code))
const spec = computed(() => (validation.value.ok ? validation.value.spec : null))
const isInvalid = computed(() => !validation.value.ok)
const isEmpty = computed(() => Boolean(spec.value && (!spec.value.labels.length || !spec.value.series.length)))
const displayError = computed(() => (validation.value.ok ? error.value : validation.value.message))
const chartTheme = computed(() => (props.theme === 'dark' ? 'dark' : 'light'))

let chart: ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let renderToken = 0
let copyTimer = 0
let mounted = false

async function renderChart() {
  const token = ++renderToken
  error.value = ''
  if (!spec.value) {
    state.value = 'error'
    return
  }
  if (isEmpty.value) {
    disposeChart()
    state.value = 'ready'
    return
  }
  try {
    const { echarts } = await import('./echarts')
    if (token !== renderToken) return
    await nextTick()
    if (!chartEl.value) return
    if (!chart) {
      chart = echarts.init(chartEl.value)
      observeSize()
    }
    chart.setOption(buildChartOption(spec.value, chartTheme.value === 'dark'), true)
    state.value = 'ready'
    chart.resize()
  } catch (err) {
    if (token !== renderToken) return
    error.value = err instanceof Error ? err.message : String(err)
    state.value = 'error'
  }
}

function observeSize() {
  if (resizeObserver || !chartEl.value) return
  resizeObserver = new ResizeObserver(() => {
    const element = chartEl.value
    if (!chart || !element || !element.clientWidth || !element.clientHeight) return
    chart.resize()
  })
  resizeObserver.observe(chartEl.value)
}

async function copySource() {
  try {
    await navigator.clipboard.writeText(props.code)
    copyLabel.value = '已复制'
  } catch {
    copyLabel.value = '复制失败'
  }
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { copyLabel.value = '复制源码' }, 1400)
}

onMounted(async () => {
  mounted = true
  chartEl.value?.setAttribute('data-theme', chartTheme.value)
  await renderChart()
})

watch(() => [props.code, props.theme], async () => {
  if (!mounted) return
  chartEl.value?.setAttribute('data-theme', chartTheme.value)
  await renderChart()
})

watch(view, async value => {
  if (value !== 'chart') return
  await nextTick()
  chart?.resize()
})

function disposeChart() {
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
}

onBeforeUnmount(() => {
  renderToken += 1
  window.clearTimeout(copyTimer)
  disposeChart()
})
</script>

<style scoped>
.rich-card { margin: 12px 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); overflow: hidden; background: var(--ui-bg); }
.rich-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 9px 4px 12px; background: var(--ui-subtle); border-bottom: 1px solid var(--ui-border); font-size: 12px; color: var(--ui-muted); }
.rich-label { font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rich-unit { margin-inline-start: 6px; padding: 1px 6px; border-radius: 999px; background: var(--ui-bg); border: 1px solid var(--ui-border); color: var(--ui-muted); font-size: 11px; font-weight: 500; }
.rich-actions { display: inline-flex; align-items: center; gap: 2px; flex-shrink: 0; }
.rich-btn { background: transparent; color: var(--ui-secondary); border: 0; border-radius: var(--ui-radius-sm); padding: 3px 8px; font-size: 12px; cursor: pointer; }
.rich-btn:hover { background: var(--ui-hover); color: var(--ui-accent); }
.rich-btn.is-active { background: var(--ui-bg); color: var(--ui-accent); box-shadow: inset 0 0 0 1px var(--ui-border); }
.rich-state { display: flex; align-items: center; gap: 8px; padding: 12px 16px; color: var(--ui-muted); font-size: 13px; border-bottom: 1px solid var(--ui-border); }
.rich-state.is-error { color: var(--ui-danger, #dc2626); }
.rich-spinner { width: 12px; height: 12px; border: 2px solid var(--ui-border-strong); border-top-color: var(--ui-accent); border-radius: 50%; animation: rich-spin .8s linear infinite; }
@keyframes rich-spin { to { transform: rotate(360deg); } }
.chart-canvas-wrap { position: relative; }
.chart-canvas { width: 100%; height: 286px; }
.chart-loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--ui-muted); font-size: 13px; background: var(--ui-bg); }
.chart-empty { padding: 38px 16px; text-align: center; color: var(--ui-muted); font-size: 13px; }
.chart-data { max-height: 340px; overflow: auto; }
.chart-data table { width: 100%; border-collapse: collapse; font-size: 13px; }
.chart-data th, .chart-data td { padding: 7px 12px; border-bottom: 1px solid var(--ui-border); text-align: start; white-space: nowrap; }
.chart-data th { position: sticky; top: 0; background: var(--ui-subtle); z-index: 1; }
.chart-data td { font-variant-numeric: tabular-nums; }
.rich-source { max-height: 340px; overflow: auto; }
.rich-source pre { margin: 0; padding: 12px; background: var(--ui-bg); overflow: auto; font: 12px/1.6 var(--ui-font-mono); }
.rich-source code { background: transparent; padding: 0; }
@media (max-width: 768px) {
  .chart-canvas { height: 230px; }
  .rich-toolbar { flex-wrap: wrap; }
}
</style>
